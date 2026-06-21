"""FastAPI app — wires the read path (cache -> trie) and write path (buffer ->
batch flush -> Postgres + trie + trending).

Endpoints:
  GET  /suggest?q=&mode=        top-10 suggestions (count|hybrid)
  POST /search    {query}        ack "Searched", buffer the count (write-back)
  GET  /cache/debug?prefix=&mode show consistent-hash routing + hit/miss
  GET  /cache/ring               nodes + key-distribution sample
  GET  /trending?n=              global trending (decayed recent_score)
  GET  /metrics                  hit rate, write reduction, p50/p95
"""
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cache, config, metrics, store, trending
from .trie import Trie
from .write_buffer import WriteBuffer

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class AppState:
    trie: Trie = Trie()
    buffer: WriteBuffer | None = None
    refresh_task: asyncio.Task | None = None


state = AppState()


async def _flush_handler(window: dict) -> None:
    """Called on every flush: durable UPSERT + live trie update."""
    await store.batch_upsert(window)        # additive write-back to Postgres
    state.trie.apply_updates(window)        # keep the in-memory index live
    # trending recomputes from the DB (recent_score just updated) -> nothing here


async def _rebuild_trie() -> None:
    rows = await store.load_all()
    new_trie = Trie()
    # build off the event loop so large datasets don't block request handling
    await asyncio.to_thread(new_trie.build, rows)
    state.trie = new_trie


async def _refresh_loop() -> None:
    while True:
        await asyncio.sleep(config.TRIE_REFRESH_SEC)
        try:
            await _rebuild_trie()
        except Exception as e:  # don't let a transient error kill the loop
            print(f"[trie refresh] error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.init_pool()
    await store.init_schema()
    await cache.init()
    await _rebuild_trie()                      # initial build from Postgres
    state.buffer = WriteBuffer(_flush_handler)
    state.buffer.start()
    state.refresh_task = asyncio.create_task(_refresh_loop())
    print(f"[startup] trie loaded with {state.trie.size()} queries; cache nodes={config.CACHE_NODES}")
    yield
    if state.refresh_task:
        state.refresh_task.cancel()
    if state.buffer:
        await state.buffer.stop()             # final drain
    await cache.close()
    await store.close_pool()


app = FastAPI(title="Search Typeahead", lifespan=lifespan)


class SearchBody(BaseModel):
    query: str


@app.get("/suggest")
async def suggest(q: str = Query(default=""), mode: str | None = None):
    t0 = time.perf_counter()
    mode = (mode or config.RANKING_MODE).lower()
    if mode not in ("count", "hybrid"):
        mode = config.RANKING_MODE
    prefix = q.lower().strip()
    if not prefix:
        metrics.record_suggest_latency((time.perf_counter() - t0) * 1000)
        return {"prefix": prefix, "mode": mode, "source": "empty", "suggestions": []}

    cached, node, hit = await cache.get_suggestions(prefix, mode)
    if hit:
        result = cached
        source = "cache"
    else:
        pairs = state.trie.get_suggestions(prefix, config.TOP_K, mode)
        result = [{"query": qq, "count": c} for qq, c in pairs]
        await cache.set_suggestions(prefix, mode, result, ttl=config.TTL_SUGGEST)
        source = "trie"

    metrics.record_suggest_latency((time.perf_counter() - t0) * 1000)
    return {"prefix": prefix, "mode": mode, "source": source, "node": node, "suggestions": result}


@app.post("/search")
async def search(body: SearchBody):
    # ack immediately (synchronous), update count asynchronously (write-back)
    state.buffer.add(body.query)
    return {"message": "Searched"}


@app.get("/cache/debug")
async def cache_debug(prefix: str = Query(...), mode: str | None = None):
    mode = (mode or config.RANKING_MODE).lower()
    return await cache.debug(prefix.lower().strip(), mode)


@app.get("/cache/ring")
async def cache_ring(sample: int = 2000):
    # distribution of a sample of synthetic prefixes across nodes (balance proof)
    import string

    keys = []
    letters = string.ascii_lowercase
    for i in range(sample):
        a = letters[i % 26]
        b = letters[(i // 26) % 26]
        c = letters[(i // 676) % 26]
        keys.append(a + b + c)
    return {
        "nodes": config.CACHE_NODES,
        "vnodes_per_node": config.VNODES,
        "sample_size": sample,
        "distribution": cache.ring.distribution(keys),
    }


@app.get("/trending")
async def get_trending(n: int = 10):
    top = await trending.get_trending(n)
    return {"trending": [{"query": q, "score": round(s, 4)} for q, s in top]}


@app.get("/metrics")
async def get_metrics():
    snap = metrics.snapshot()
    snap["trie_size"] = state.trie.size()
    snap["buffer_pending"] = state.buffer.pending() if state.buffer else 0
    snap["cache_nodes"] = config.CACHE_NODES
    return snap


# ---- frontend ----
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
