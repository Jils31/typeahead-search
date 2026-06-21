# Performance

Setup: 1 FastAPI node, 1 Postgres, 3 Redis nodes (Docker). Dataset: AOL 2006,
top 1,000,000 queries by count. Reproduce: `python -m scripts.benchmark`.

## Read latency — `GET /suggest`
- Server p95 ≈ **0.5 ms** (cache hit → Redis; miss → in-memory trie).
- DB reads on the suggestion path = **0**: a miss is served by the trie, never
  Postgres.

## Cache hit rate
- ~**82%** over 8,000 mixed reads (6,585 hits / 1,415 misses).
- Rises toward 90%+ with a more skewed (real-traffic) load or longer
  `TTL_SUGGEST`. The benchmark uses a synthetic Zipf prefix mix.

## Write reduction — batching
20,000 search submissions in one run:

| Searches received | Rows written | Flush transactions |
|---|---|---|
| 20,000 | ~4,700 | 11 |

≈ 4.3× fewer rows (duplicate aggregation) and ~1,800× fewer transactions
(batching). Trade-off: a crash loses at most one un-flushed window.

## Consistent hashing — distribution
5,000 sample prefixes across 3 nodes (150 vnodes each): **30.9% / 34.6% / 34.5%**
— within ~±4% of even. Adding/removing a node remaps only ~1/N of keys.
Per-prefix routing via `GET /cache/debug?prefix=<p>`.

## Reproduce
```bash
python -m scripts.benchmark --base http://localhost:8000 --reads 8000 --writes 20000
curl 'http://localhost:8000/cache/ring?sample=5000'
```
