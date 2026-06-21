"""Performance report generator (assignment §10).

Measures:
- /suggest latency p50/p95/p99 (client-side) under a Zipf-distributed prefix load
- cache hit rate (server /metrics)
- write reduction: fire many /search, then compare searches_received vs db_writes

Run (server must be up):
  python -m scripts.benchmark --base http://localhost:8000 --reads 5000 --writes 20000
"""
import argparse
import asyncio
import random
import statistics
import string
import time

import httpx

HOT = ["a", "i", "ip", "be", "ho", "wh", "fr", "do", "bu", "ne", "mo", "py", "ja"]


def zipf_prefix() -> str:
    """Mostly short/common prefixes (hot), sometimes a random longer one (tail)."""
    r = random.random()
    if r < 0.8:                       # 80% of traffic -> hot prefixes (Pareto)
        return random.choice(HOT)
    n = random.randint(2, 4)
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


async def run_reads(client, base, reads, mode):
    latencies = []
    for _ in range(reads):
        p = zipf_prefix()
        t0 = time.perf_counter()
        await client.get(f"{base}/suggest", params={"q": p, "mode": mode})
        latencies.append((time.perf_counter() - t0) * 1000)
    return latencies


async def run_writes(client, base, writes):
    # heavy duplication so aggregation is visible (Zipf prefixes as queries)
    for _ in range(writes):
        q = zipf_prefix() + " " + random.choice(["phone", "tutorial", "price", "news", "app"])
        await client.post(f"{base}/search", json={"query": q})


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return round(xs[k], 3)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--reads", type=int, default=5000)
    ap.add_argument("--writes", type=int, default=20000)
    ap.add_argument("--mode", default="hybrid")
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=30) as client:
        print("== WRITE REDUCTION (batching) ==")
        m0 = (await client.get(f"{args.base}/metrics")).json()
        await run_writes(client, args.base, args.writes)
        await asyncio.sleep(2.0)  # let buffer flush
        m1 = (await client.get(f"{args.base}/metrics")).json()
        recv = m1["searches_received"] - m0["searches_received"]
        wrote = m1["db_writes"] - m0["db_writes"]
        batches = m1["db_write_batches"] - m0["db_write_batches"]
        print(f"  searches sent      : {recv:,}")
        print(f"  db rows written    : {wrote:,}")
        print(f"  flush batches      : {batches:,}")
        if wrote:
            print(f"  write reduction    : {recv/wrote:.1f}x fewer rows, "
                  f"{recv/max(1,batches):.0f}x fewer transactions")

        print("\n== READ LATENCY + HIT RATE ==")
        # warm the cache first so steady-state hit rate is measured
        await run_reads(client, args.base, args.reads // 5, args.mode)
        mb = (await client.get(f"{args.base}/metrics")).json()
        chunks = await asyncio.gather(*[
            run_reads(client, args.base, args.reads // args.concurrency, args.mode)
            for _ in range(args.concurrency)
        ])
        lat = [x for c in chunks for x in c]
        ma = (await client.get(f"{args.base}/metrics")).json()
        hits = ma["cache_hits"] - mb["cache_hits"]
        misses = ma["cache_misses"] - mb["cache_misses"]
        hr = hits / (hits + misses) if (hits + misses) else 0
        print(f"  requests           : {len(lat):,}")
        print(f"  client p50 / p95   : {pct(lat,50)} ms / {pct(lat,95)} ms")
        print(f"  client p99         : {pct(lat,99)} ms")
        print(f"  cache hit rate     : {hr*100:.1f}%  ({hits:,} hits / {misses:,} misses)")
        print(f"  server p95         : {ma['suggest_latency_ms']['p95']} ms")
        print(f"  trie size          : {ma['trie_size']:,} queries")


if __name__ == "__main__":
    asyncio.run(main())
