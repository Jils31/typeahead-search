"""Trending = global top-N by time-decayed recent_score, decayed at read time so
quiet queries fall off. Cached with a short TTL (TTL_TREND)."""
import json
from typing import List, Tuple

from . import cache, config, ranking, store


async def get_trending(n: int) -> List[Tuple[str, float]]:
    key = f"trending:{n}"
    raw = await cache.get_raw(key)
    if raw is not None:
        return [(q, s) for q, s in json.loads(raw)]

    # miss -> recompute from the store, decaying each candidate to 'now'
    candidates = await store.trending_candidates(limit=max(100, n * 5))
    decayed = [(q, ranking.decay(rs, age)) for q, rs, age in candidates]
    decayed.sort(key=lambda x: x[1], reverse=True)
    top = decayed[:n]

    await cache.set_raw(key, json.dumps(top), ttl=config.TTL_TREND)
    return top
