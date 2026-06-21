"""Central config — every design knob lives here so tradeoffs are tunable.

Loaded from the project-root .env. Each value maps to a decision documented in
DESIGN.md (TTL = invalidation, batch N/T = write-back, vnodes = consistent
hashing balance, weights/half-life = trending).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# .env sits at the project root (one level above backend/)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, default))


# ---- Primary store ----
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = _int("PG_PORT", 5432)
PG_USER = os.getenv("PG_USER", "typeahead")
PG_PASSWORD = os.getenv("PG_PASSWORD", "typeahead")
PG_DB = os.getenv("PG_DB", "typeahead")

PG_DSN = f"host={PG_HOST} port={PG_PORT} user={PG_USER} password={PG_PASSWORD} dbname={PG_DB}"

# ---- Distributed cache ----
CACHE_NODES = [n.strip() for n in os.getenv("CACHE_NODES", "localhost:6390,localhost:6391,localhost:6392").split(",") if n.strip()]
VNODES = _int("VNODES", 150)

# ---- Cache invalidation (TTL) ----
TTL_SUGGEST = _int("TTL_SUGGEST", 45)
TTL_TREND = _int("TTL_TREND", 8)
TTL_JITTER = _float("TTL_JITTER", 0.2)

# ---- Batch writes (write-back) ----
BATCH_SIZE_N = _int("BATCH_SIZE_N", 500)
FLUSH_INTERVAL_T = _float("FLUSH_INTERVAL_T", 1.0)

# ---- Suggestions / trie ----
TOP_K = _int("TOP_K", 10)
PRECOMPUTE_PREFIX_LEN = _int("PRECOMPUTE_PREFIX_LEN", 3)
TRIE_REFRESH_SEC = _int("TRIE_REFRESH_SEC", 30)  # periodic pool refresh + decay correction

# ---- Ranking ----
RANKING_MODE = os.getenv("RANKING_MODE", "hybrid")
W_POP = _float("W_POP", 1.0)
W_REC = _float("W_REC", 2.0)
DECAY_HALFLIFE_SEC = _float("DECAY_HALFLIFE_SEC", 3600.0)
