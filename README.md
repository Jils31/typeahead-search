# Search Typeahead System

![architecture](screenshots/architecture.png)

A search-autocomplete service: suggests popular queries as you type, records
submitted searches, and serves suggestions with low latency. Reads are served
from a distributed cache (consistent hashing) backed by an in-memory trie;
writes are batched (write-back) into PostgreSQL.

**Stack:** Node.js (Express) · PostgreSQL · 3 Redis nodes · vanilla JS UI.
Design decisions and trade-offs: [DESIGN.md](DESIGN.md). Numbers: [PERFORMANCE.md](PERFORMANCE.md).

## Run

```bash
docker compose up -d                       # Postgres + 3 Redis
cp .env.example .env
npm install

node scripts/loadDataset.js --synthetic 120000   # quick start, no download
npm start                                         # node backend/server.js (port 8000)
open http://127.0.0.1:8000
```

Postgres is on host port **5433** (5432 is often taken by a native install);
Redis nodes are on 6390/6391/6392.

## Dataset

AOL 2006 query log (real searches; counts derived by aggregation). To load it:

```bash
curl -L -o files/aol.zip "https://archive.org/download/AOL_search_data_leak_2006/AOL_search_data_leak_2006.zip"
unzip -o -j files/aol.zip "AOL-user-ct-collection/*.txt.gz" -d files/aol_data
node --max-old-space-size=4096 scripts/loadDataset.js --dir files/aol_data --min-count 2 --out files/aol_agg.tsv
node scripts/loadDataset.js --agg-file files/aol_agg.tsv --top 1000000 --min-count 3
```

35.4M rows → 4.1M distinct queries; we load the top 1M by count (the full set
makes the in-memory trie unnecessarily large). `--synthetic N` needs no download.

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/suggest?q=<prefix>&mode=count\|hybrid` | Top-10 prefix matches. `count` = all-time, `hybrid` = recency-aware. |
| POST | `/search` `{"query":"..."}` | Returns `{"message":"Searched"}`; buffers the count. |
| GET | `/cache/debug?prefix=<p>` | Which cache node owns the prefix + HIT/MISS. |
| GET | `/cache/ring?sample=N` | Key distribution across nodes. |
| GET | `/trending?n=10` | Trending by decayed recent score. |
| GET | `/metrics` | Hit rate, DB read/write counts, write reduction, p50/p95. |

```bash
node scripts/benchmark.js --reads 8000 --writes 20000   # performance report
```

## Layout

```
backend/   server.js · config.js · consistentHash.js · cache.js · trie.js
           store.js · writeBuffer.js · ranking.js · trending.js · metrics.js
scripts/   loadDataset.js · benchmark.js
frontend/  index.html · app.js
```
