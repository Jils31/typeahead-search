"""Distributed cache over N Redis nodes, routed by consistent hashing.
Cache-aside reads, write-around writes, jittered-TTL invalidation. The routing
key is the prefix, so both ranking modes for a prefix share one node."""
import json
import random
from typing import Dict, List, Optional, Tuple

import redis.asyncio as aioredis

from . import config, metrics
from .consistent_hash import ConsistentHashRing

_clients: Dict[str, "aioredis.Redis"] = {}
ring: ConsistentHashRing = ConsistentHashRing(vnodes=config.VNODES)


def _redis_key(prefix: str, mode: str) -> str:
    return f"sugg:{mode}:{prefix}"


async def init() -> None:
    global ring
    ring = ConsistentHashRing(vnodes=config.VNODES)
    for node in config.CACHE_NODES:
        host, port = node.split(":")
        _clients[node] = aioredis.Redis(host=host, port=int(port), decode_responses=True)
        ring.add_node(node)
    # fail fast if a node is unreachable
    for node, client in _clients.items():
        await client.ping()


async def close() -> None:
    for client in _clients.values():
        await client.aclose()


def _jittered(ttl: int) -> int:
    delta = ttl * config.TTL_JITTER
    return max(1, int(ttl + random.uniform(-delta, delta)))


async def get_suggestions(prefix: str, mode: str) -> Tuple[Optional[List], str, bool]:
    """Returns (suggestions|None, owner_node, hit)."""
    node = ring.get_node(prefix)
    client = _clients[node]
    raw = await client.get(_redis_key(prefix, mode))
    if raw is None:
        metrics.cache_misses += 1
        return None, node, False
    metrics.cache_hits += 1
    return json.loads(raw), node, True


async def set_suggestions(prefix: str, mode: str, suggestions: List, ttl: int) -> str:
    node = ring.get_node(prefix)
    client = _clients[node]
    await client.set(_redis_key(prefix, mode), json.dumps(suggestions), ex=_jittered(ttl))
    return node


async def get_raw(key: str) -> Optional[str]:
    node = ring.get_node(key)
    return await _clients[node].get(key)


async def set_raw(key: str, value: str, ttl: int) -> str:
    node = ring.get_node(key)
    await _clients[node].set(key, value, ex=_jittered(ttl))
    return node


async def debug(prefix: str, mode: str) -> dict:
    """Powers GET /cache/debug — routing + live hit/miss for the prefix key."""
    info = ring.debug(prefix)
    node = info["owner_node"]
    present = False
    if node is not None:
        present = (await _clients[node].get(_redis_key(prefix, mode))) is not None
    info["mode"] = mode
    info["redis_key"] = _redis_key(prefix, mode)
    info["currently_cached"] = present
    info["hit_or_miss"] = "HIT" if present else "MISS"
    return info
