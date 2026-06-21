"""PostgreSQL primary store (durable source of truth for counts).

Additive UPSERT (`count = count + EXCLUDED.count`) so concurrent flushes add
instead of clobbering; recent_score is decayed in SQL on each flush."""
import math
from typing import Dict, List, Tuple

from psycopg_pool import AsyncConnectionPool

from . import config, metrics

_LAMBDA = math.log(2) / config.DECAY_HALFLIFE_SEC

_pool: AsyncConnectionPool | None = None


async def init_pool() -> None:
    global _pool
    _pool = AsyncConnectionPool(conninfo=config.PG_DSN, min_size=1, max_size=10, open=False)
    await _pool.open()


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


async def init_schema() -> None:
    async with _pool.connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queries (
                query         TEXT PRIMARY KEY,
                count         BIGINT NOT NULL DEFAULT 0,
                recent_score  DOUBLE PRECISION NOT NULL DEFAULT 0,
                last_searched TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # text_pattern_ops -> makes LIKE 'prefix%' use the index as a range scan
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_query_prefix ON queries (query text_pattern_ops);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_score ON queries (recent_score DESC);"
        )


async def truncate() -> None:
    async with _pool.connection() as conn:
        await conn.execute("TRUNCATE queries;")


async def count_rows() -> int:
    async with _pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM queries;")
        row = await cur.fetchone()
        return row[0] if row else 0


async def bulk_load(rows: List[Tuple[str, int]]) -> None:
    """Initial dataset ingestion: (query, count). Uses COPY for speed."""
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            async with cur.copy(
                "COPY queries (query, count) FROM STDIN"
            ) as copy:
                for q, c in rows:
                    await copy.write_row((q, c))


async def batch_upsert(window: Dict[str, int]) -> int:
    """Apply one flush window of aggregated increments.

    `window` maps query -> number of searches in this window. Each search also
    contributes +1 to recent_score (so recent increment == count increment).
    Returns the number of rows written (for the write-reduction metric).
    """
    if not window:
        return 0
    items = list(window.items())
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO queries (query, count, recent_score, last_searched)
                VALUES (%(q)s, %(inc)s, %(inc)s, now())
                ON CONFLICT (query) DO UPDATE SET
                    count = queries.count + EXCLUDED.count,
                    recent_score = queries.recent_score
                        * exp(-%(lam)s * EXTRACT(EPOCH FROM (now() - queries.last_searched)))
                        + EXCLUDED.recent_score,
                    last_searched = now();
                """,
                [{"q": q, "inc": inc, "lam": _LAMBDA} for q, inc in items],
            )
    metrics.db_writes += len(items)
    metrics.db_write_batches += 1
    return len(items)


async def load_all() -> List[Tuple[str, int, float, float]]:
    """Load every row for trie build: (query, count, recent_score, age_seconds).

    age_seconds = how long since last_searched, so callers can decay to 'now'.
    """
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "SELECT query, count, recent_score, EXTRACT(EPOCH FROM (now() - last_searched)) FROM queries;"
        )
        rows = await cur.fetchall()
    return [(r[0], int(r[1]), float(r[2]), float(r[3] or 0.0)) for r in rows]


async def trending_candidates(limit: int) -> List[Tuple[str, float, float]]:
    """Top rows by stored recent_score: (query, recent_score, age_seconds).

    Caller decays each to 'now' so queries that went quiet drop off — this is
    why trending must decay at read time, not only on write (trending Q3)."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT query, recent_score, EXTRACT(EPOCH FROM (now() - last_searched))
            FROM queries ORDER BY recent_score DESC LIMIT %s;
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return [(r[0], float(r[1]), float(r[2] or 0.0)) for r in rows]
