# Performance Report

**Setup:** single app node (FastAPI), 1 Postgres, 3 Redis cache nodes (Docker).
**Dataset:** AOL 2006 query log — 35.4M rows aggregated to 4.1M distinct queries;
top **1,000,000 by count** loaded. Reproduce with `python -m scripts.benchmark`.

## 1. Read latency — `GET /suggest`

| Metric | Value |
|---|---|
| Server p95 | **~0.5 ms** |
| Server p50 | < 1 ms |
| Path | cache hit → in-memory Redis; miss → in-memory trie |

Suggestions never block on Postgres: a cache miss is served by the in-memory
trie (O(prefix length)), so **DB reads on the suggestion path = 0**.

## 2. Cache hit rate & DB read/write counts

| Metric | Value |
|---|---|
| Cache hit rate | **~82%** (6,585 hits / 1,415 misses over 8,000 reads) |
| DB reads (suggestion path) | **0** (trie absorbs all misses) |
| DB writes | batched only (see §3) |

Hit rate rises toward 90%+ with a more skewed (real-traffic) load or a longer
`TTL_SUGGEST`. The benchmark uses a synthetic Zipf prefix mix; real query traffic
is more concentrated.

## 3. Write reduction — batching (write-back)

20,000 search submissions in one run:

| Stage | Count |
|---|---|
| Searches received | 20,000 |
| Rows written to Postgres | 4,688 |
| Flush transactions | 11 |

→ **~4.3× fewer rows** (duplicate aggregation) and **~1,800× fewer transactions**
(batching). Trade-off: an app crash loses at most one un-flushed window (≤ batch
size / flush interval); acceptable for approximate, self-healing counts.

## 4. Consistent hashing — key distribution

5,000 sample prefixes routed across 3 nodes (150 virtual nodes each):

| Node | Share |
|---|---|
| localhost:6390 | 30.9% |
| localhost:6391 | 34.6% |
| localhost:6392 | 34.5% |

Balanced within ~±4% of even (33.3%). Virtual nodes keep distribution smooth;
adding/removing a node remaps only ~1/N of keys (vs ~all keys with `hash % N`).
Inspect routing per prefix via `GET /cache/debug?prefix=<p>`.

## 5. Why caching works here (Pareto)

AOL query popularity is Zipf-distributed: a small set of prefixes serve most
traffic, so a cache holding the hot set yields a high hit rate while the DB only
handles batched writes. This is the basis for the cache-first read path.

## Reproduce
```bash
python -m scripts.benchmark --base http://localhost:8000 --reads 8000 --writes 20000
curl 'http://localhost:8000/cache/ring?sample=5000'
```
