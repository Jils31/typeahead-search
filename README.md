# Search Typeahead System

A search-autocomplete backend + UI that suggests popular queries as you type,
records submitted searches, and serves suggestions with low latency via a
**distributed cache (consistent hashing)**, a **trie top-k index**,
**write-back batching**, and **recency-aware trending**.

> Design rationale and trade-offs for every decision are in **[DESIGN.md](DESIGN.md)**.

## Architecture

```
 Browser UI (debounce, dropdown, keyboard nav, trending, modes)
        │
        ▼   (round-robin LB in front of stateless app nodes)
┌──────────────── FastAPI app node ─────────────────┐
│  in-memory TRIE (top-k index)   write BUFFER       │
│  consistent-hash RING ─► routes prefix → cache node│
└───────┬───────────────────────────────┬───────────┘
        │ read: cache-aside             │ write: write-back batch flush
   ┌────┼────┬────┐                     ▼
   ▼    ▼    ▼                    ┌──────────────┐
 Redis0 Redis1 Redis2  (cache)    │ PostgreSQL    │ (durable counts)
        │ miss → trie              └──────────────┘
        ▼
   compute top-k → fill cache (TTL) → return
```

- **Read** (`GET /suggest`): consistent-hash the prefix → Redis node → **hit** returns top-10; **miss** computes from the trie, fills the cache with a (jittered) TTL.
- **Write** (`POST /search`): returns `Searched` immediately; the count is buffered in memory and flushed to Postgres in **aggregated batches** (write-back).

## Prerequisites
- Python 3.11+ (tested on 3.14)
- Docker + Docker Compose

## Setup

```bash
# 1. Infrastructure: Postgres + 3 Redis cache nodes
docker compose up -d

# 2. Config + Python env
cp .env.example .env
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

# 3. Load a dataset (see options below)
./.venv/bin/python -m scripts.load_dataset --synthetic 120000     # quick start, no download

# 4. Run the server
./.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 5. Open the UI
open http://127.0.0.1:8000
```

> **Note on ports:** Postgres is mapped to host port **5433** (to avoid clashing
> with a native Postgres on 5432). Redis nodes are on **6390/6391/6392**.

## Dataset

**Primary dataset: AOL search query log (2006)** — ~36M real user search queries.
Counts are derived by **aggregation** (`COUNT(*)` per normalized query) — exactly
the assignment's "derive counts if absent". Query popularity is naturally
Zipf/Pareto, which is what makes caching work (see DESIGN.md §1).

### Reproduce the exact load used here (no Kaggle login needed)
```bash
# 1. download (~440 MB) from the Internet Archive mirror
curl -L -o files/aol.zip \
  "https://archive.org/download/AOL_search_data_leak_2006/AOL_search_data_leak_2006.zip"

# 2. extract the 10 gzipped data files
unzip -o -j files/aol.zip "AOL-user-ct-collection/*.txt.gz" -d files/aol_data

# 3. aggregate all 10 files -> a reusable TSV (35.4M rows -> 4.1M distinct queries)
./.venv/bin/python -m scripts.load_dataset --dir files/aol_data --min-count 2 \
  --out files/aol_agg.tsv

# 4. load the top 1,000,000 queries by count into Postgres
./.venv/bin/python -m scripts.load_dataset --agg-file files/aol_agg.tsv \
  --top 1000000 --min-count 3
```

The loader auto-detects delimiter + `Query` column, handles `.gz`, lowercases/
trims, drops blanks and `-`, aggregates, and bulk-loads via `COPY`. We load the
**top 1M by count** (4.1M distinct is far more than needed and makes the in-memory
trie heavy); raise `--top` for more.

> Also on [Kaggle (AOL 500K)](https://www.kaggle.com/datasets/dineshydv/aol-user-session-collection-500k) if you prefer.
> **Privacy note:** the 2006 AOL release had a user-privacy controversy. We use
> only the **query text + derived counts** and drop all user IDs.

**No-download option:** `--synthetic 120000` generates Zipf-distributed queries so
the system runs immediately and still exceeds the 100k minimum.

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/suggest?q=<prefix>&mode=count\|hybrid` | Top-10 prefix matches. `count` = all-time, `hybrid` = recency-aware. Returns `source` (cache/trie) + owning `node`. |
| POST | `/search` `{"query": "..."}` | Returns `{"message":"Searched"}`; buffers the count (write-back). |
| GET | `/cache/debug?prefix=<p>&mode=` | Consistent-hash routing for the prefix: owner node, hash, ring position, HIT/MISS. |
| GET | `/cache/ring?sample=N` | Key distribution of N sample prefixes across nodes (balance evidence). |
| GET | `/trending?n=10` | Global trending by decayed recent score. |
| GET | `/metrics` | Cache hit rate, DB read/write counts, write-reduction factor, p50/p95/p99. |

### Examples
```bash
curl 'http://127.0.0.1:8000/suggest?q=ip&mode=hybrid'
curl -X POST 'http://127.0.0.1:8000/search' -H 'Content-Type: application/json' -d '{"query":"iphone 15"}'
curl 'http://127.0.0.1:8000/cache/debug?prefix=ip'
curl 'http://127.0.0.1:8000/trending?n=10'
```

## Performance report

```bash
./.venv/bin/python -m scripts.benchmark --reads 6000 --writes 20000
```
Reports `/suggest` p50/p95/p99, cache hit rate, and write reduction
(searches sent vs DB rows written vs transactions). Sample run on the **1M-query
AOL dataset**:

```
WRITE REDUCTION : 20,000 searches → 4,620 rows in 11 batches  (1818× fewer transactions)
READ LATENCY    : server p95 ≈ 0.5 ms   (cache hits sub-millisecond)
CACHE HIT RATE  : ~83%   (→ 90%+ with a more skewed load / longer TTL)
TRIE SIZE       : ~1,000,000 queries
```

## Configuration

All knobs live in `.env` (loaded by `backend/config.py`): cache TTLs, batch
size/interval, virtual nodes, ranking weights, decay half-life, eviction policy.
Each maps to a decision in DESIGN.md.

## Project structure
```
backend/
  main.py            FastAPI app + endpoints + lifespan wiring
  config.py          all tunable knobs (.env)
  consistent_hash.py hash ring + virtual nodes
  cache.py           multi-Redis client, routing, TTL, write-around
  trie.py            in-memory top-k index (precompute short, lazy long)
  store.py           Postgres: additive UPSERT, prefix fallback, decay-in-SQL
  write_buffer.py    write-back batch buffer (flush on size or time)
  ranking.py         count vs hybrid score, exponential decay
  trending.py        decayed recent_score top-N
  metrics.py         hit rate, read/write counts, latency percentiles
scripts/
  load_dataset.py    AOL ingestion (or --synthetic)
  benchmark.py       performance report
frontend/            vanilla HTML/JS UI
docker-compose.yml   Postgres + 3 Redis nodes
```

## Rubric mapping
- **Basic (60%)** — dataset ingestion, UI, `/suggest`, `/search`, count updates, distributed cache + consistent hashing (`/cache/debug`).
- **Trending (20%)** — `ranking.py` + `trending.py`, `hybrid` mode, decay (DESIGN.md §10).
- **Batch writes (20%)** — `write_buffer.py`, write-reduction evidence, crash trade-off (DESIGN.md §8).
```
