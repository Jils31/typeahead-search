"""Counters + latency samples exposed at /metrics: hit rate, DB read/write
counts, write-reduction factor, p50/p95/p99."""
from collections import deque
from typing import Deque

# ---- counters ----
cache_hits = 0
cache_misses = 0
db_reads = 0            # prefix fallback queries that hit Postgres
db_writes = 0          # rows written via batch flush (the batched UPSERTs)
db_write_batches = 0   # number of flush operations (transactions)
searches_received = 0  # POST /search calls (before aggregation)

# ---- latency samples for /suggest (ms), bounded ring buffer ----
_LAT_CAP = 5000
_suggest_latency_ms: Deque[float] = deque(maxlen=_LAT_CAP)


def record_suggest_latency(ms: float) -> None:
    _suggest_latency_ms.append(ms)


def _percentile(samples, p: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[k], 3)


def snapshot() -> dict:
    total = cache_hits + cache_misses
    hit_rate = round(cache_hits / total, 4) if total else 0.0
    # write reduction = searches received vs rows actually written to the DB
    write_reduction = round(searches_received / db_writes, 2) if db_writes else None
    return {
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "cache_hit_rate": hit_rate,
        "db_reads": db_reads,
        "db_writes": db_writes,
        "db_write_batches": db_write_batches,
        "searches_received": searches_received,
        "write_reduction_factor": write_reduction,
        "suggest_latency_ms": {
            "samples": len(_suggest_latency_ms),
            "p50": _percentile(_suggest_latency_ms, 50),
            "p95": _percentile(_suggest_latency_ms, 95),
            "p99": _percentile(_suggest_latency_ms, 99),
        },
    }
