# Design & Trade-offs — Search Typeahead System

This document explains **every major design decision**, the **alternative** we
rejected, and **why** — grounded in the system-design topics the course covers.
The guiding tension throughout:

> A typeahead system has two opposing workloads on one dataset: **reads**
> (suggestions, fired on every keystroke) and **writes** (count updates on every
> submit). Reads dominate and want static, cached data; writes constantly mutate
> that data. Every decision below is the negotiated truce between the two.

---

## 1. Establishing the need for caching (Pareto + back-of-envelope)

**Assumptions:** 10M DAU × 4 searches/day = **40M searches/day** ≈ **~460 writes/s**
average, ~1,400/s peak (3× factor).

**The read multiplier:** one search = ~5 suggestion reads (typing, debounced) + 1
write. So **reads ≈ 5× writes → ~2,300/s avg, ~7,000/s peak**. Reads dominate
~5–10:1.

**Without a cache:** every keystroke runs a prefix scan + sort on Postgres. At
~7k QPS the DB saturates and p95 climbs into the hundreds of ms — a laggy
typeahead.

**Why caching wins — Zipf/Pareto:** query traffic follows a power law; the top
~20% of prefixes serve ~80%+ of requests. Caching the hot set gives a **>90% hit
rate**, cutting DB read load ~10× and keeping p95 in single-digit ms. The data is
read-heavy, the hot set is small and stable between keystrokes, and slight
staleness is acceptable — the textbook case for caching.

**Decision:** cache the read path.

---

## 2. Local vs Global cache → **Global**

| | Local (in-process per app node) | **Global (shared tier)** |
|---|---|---|
| Speed | fastest (no network) | +0.2–1 ms network hop |
| Duplication | same entry cached on every node | one copy |
| **Invalidation** | must broadcast to N nodes (coherence nightmare) | invalidate once, all nodes see it |
| Hit rate | per-node, partial | global, warm |
| Restart/scale-out | cold | survives |

**Decision: global.** The deciding factor is invalidation — writes invalidate
data, and invalidating one shared cache is clean, while keeping N private caches
coherent requires broadcasting (pub/sub) or polling (cron) plus a shared log
anyway. (A cron/poll approach *works* for eventual consistency but is the
worst-of-both: it needs a shared component *and* keeps local caches. TTL or
pub/sub beat it. We sidestep the whole class by going global.) Our in-memory
**trie** acts as an optional L1; **Redis** is the authoritative global L2.

---

## 3. Single vs Distributed cache → **Distributed**

A single Redis *would* handle this dataset and ~7k QPS (Redis does 50–100k+
ops/s; our hot set fits in RAM). **So throughput/capacity is NOT the reason.**

**The real reasons:**
1. **No single point of failure.** One node down = 100% of reads stampede the DB
   (thundering herd) → cascade outage. With 3 nodes, one failure loses only ~1/3
   of the cache; the rest keeps serving. Distribution **bounds the blast radius.**
2. **Horizontal headroom** as data/traffic grow.
3. It's the real-world pattern (and the learning objective).

**Decision: distributed.** Honest framing: chosen for **resilience + scale, not
raw throughput.**

---

## 4. Routing: why **consistent hashing** (not round-robin, not `hash % N`)

Cache nodes are **stateful** (each holds different keys), so routing is a
**partitioning** problem, not load balancing.

- **Round-robin fails:** it throws away *where* a key lives → constant misses +
  duplication.
- **`hash(key) % N` is deterministic but brittle:** changing N (add/remove node)
  remaps *almost every* key at once → mass miss / stampede.
- **Consistent hashing:** keys and nodes are placed on a ring; a key is owned by
  the next node clockwise. Adding/removing a node remaps only **~K/N keys** (just
  that node's arc). **Virtual nodes** (150/node) smooth the distribution.

Implemented in `consistent_hash.py`; `GET /cache/debug` proves routing; observed
distribution across 3 nodes ≈ 31/35/34% (balanced).

---

## 5. Load balancing (app tier) → **round-robin**

App nodes are **stateless** (cache + DB are external), so any node serves any
request. Round-robin / least-connections is correct here. (Sticky/consistent-hash
LB would raise local-cache hit rate but risks hot spots — unnecessary since our
shared cache is global.)

---

## 6. Cache eviction → **LRU** (LFU discussed)

Eviction = a **capacity** policy (out of space), distinct from invalidation.

- **LRU (chosen):** fits typeahead — hot prefixes are re-hit constantly, so they
  stay at the MRU end and only the rare long tail is evicted. Simple, O(1)-ish.
- **LFU:** marginally better for stable Pareto popularity, **but needs an aging
  factor** — without decay, an old viral query (e.g. "black friday deals" at
  1,000,000 hits) squats forever and starves a genuinely-hot newcomer (50,000
  hits) because raw frequency never forgets. Redis's LFU adds logarithmic counters
  + `lfu-decay-time` to fix this. Extra complexity for marginal gain.

**Key point:** because we use short TTLs, entries usually expire before memory
fills, so **eviction is a safety net, not the primary tool** — making LRU vs LFU
largely academic. Redis config: `maxmemory 256mb`, `allkeys-lru` (note: Redis LRU
is *approximate*/sampled for speed).

---

## 7. Cache invalidation → **write-around + short jittered TTL**

Invalidation = a **correctness** policy (data changed). A count change can poison
*many* prefix entries (`i`, `ip`, `iph`… for `iphone`) across different nodes.

**Mechanisms:**
- **TTL (primary):** every entry expires after ~30–60s (suggestions) / ~8s
  (trending). Bounds staleness with **zero tracking** — poisoned multi-node keys
  self-heal on expiry. **Jittered** to avoid synchronized-expiry stampede.
- **Write-around:** searches update the store (via the buffer), **not** the cache;
  the cache refreshes lazily on the next read miss. We avoid **write-through**
  because our cache value is a *computed top-10* — recomputing it on every write
  (most of which don't even change the ranking) is pure waste.
- **No targeted invalidation:** deliberately omitted (over-engineering for this
  scale). Freshness where it matters (trending) comes from a **shorter TTL**, not
  extra machinery.

**The TTL knob trades** freshness ↔ hit-rate/load; we pick longer TTLs for
suggestions (slow-changing) and a short TTL for trending (fast-changing).

**Stampede mitigation:** jittered TTL (+ single-flight as a future hardening).

---

## 8. Write strategy → **write-back batching** (+ crash trade-off)

Writing every search to Postgres synchronously is wasteful (per-row transaction +
WAL fsync + lock contention on hot rows).

**Buffer → aggregate → flush (`write_buffer.py`):**
- `POST /search` increments an in-memory map (instant ack).
- Duplicates **aggregate** (50× `iphone` → one `+50`).
- Flush on **size N OR time T** (whichever first): size caps memory under spikes,
  time caps staleness when quiet.
- One **additive UPSERT** per flush: `count = count + EXCLUDED.count` — additive
  so concurrent flushes from multiple nodes add up instead of clobbering.

**Two reductions stack:** aggregation collapses duplicates; batching collapses
many writes into one transaction. Measured: **20k searches → ~4,600 rows in 10
transactions (~2000× fewer transactions)**.

**Crash trade-off (required):** the buffer is in RAM, so an app crash loses at
most **one flush window** (≤ N or ≤ T). **Acceptable** because counts are
approximate popularity signals and self-healing (a truly popular query is
re-searched immediately). Durability upgrade path: **WAL** (append-before-ack,
replay on restart) or a **durable queue** (Kafka/Redis Streams) — both discussed,
neither built (over-engineering for this scale).

This is **write-back** applied to counts. (The suggestion cache, separately, uses
**write-around**.)

---

## 9. SQL vs NoSQL → **PostgreSQL** (and why batching makes this the right call)

| Question | Our answer |
|---|---|
| Data shape | one simple entity `query→count` (+recency); no joins |
| Access pattern | prefix range scan + top-k (read), additive UPSERT (write) |
| Consistency | eventual is fine |
| Scale | fits one machine |
| Write pressure at the DB | **already cut ~60× by batching** |

The workload *looks* write-heavy → usually argues for a write-optimized
NoSQL/LSM store. **But batching absorbed the writes at the app layer**, so the
throughput argument for NoSQL is gone. That frees us to use Postgres, whose
**B-tree index** handles `LIKE 'pre%'` as a range scan, gives **ACID** for the
count truth, and offers **read replicas**. We'd switch to an LSM store like
**Cassandra** only if writes were un-batchable + massive, or we needed horizontal
sharding + tunable quorum.

**Data model (`store.py`):**
```sql
CREATE TABLE queries (
  query         TEXT PRIMARY KEY,
  count         BIGINT NOT NULL DEFAULT 0,
  recent_score  DOUBLE PRECISION NOT NULL DEFAULT 0,
  last_searched TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_query_prefix ON queries (query text_pattern_ops);  -- prefix range scan
```

---

## 10. LSM / memtable / SSTable (conceptual + our analogy)

LSM engines optimize **writes** by never updating in place: writes hit a **WAL**
(durability) + an in-memory **memtable**, which flushes to immutable sorted
**SSTables**; **compaction** merges them and drops old versions; **bloom filters**
keep reads fast. Trade-off (RUM): great writes, more read/space amplification +
background compaction. That's why write-heavy NoSQL stores (Cassandra, RocksDB)
are LSM-based.

**We do NOT use an LSM database.** We use Postgres (B-tree). But our **batch
buffer is conceptually a memtable** (writes in RAM, sorted by key), **flush ≈
memtable→SSTable**, **aggregation ≈ compaction**, and the optional WAL ≈ the LSM
WAL. So we get **LSM-style write absorption at the app layer + a read-friendly
B-tree store underneath** — best of both. (Redis, our cache, is technically a
NoSQL KV store — used only for caching.)

---

## 11. CAP & PACELC → **PA / EL**

- **CAP (during a partition):** suggestions choose **Availability** — serve
  slightly-stale rather than error (an erroring typeahead is unacceptable).
- **PACELC (Else, no partition):** choose **Latency** — serve from cache rather
  than re-verify against the DB on every request.

So the suggestion path is **PA/EL**. This single posture is the *root* of
caching, TTL-based eventual consistency, and write-back. **Justification:**
staleness of approximate popularity costs ~nothing; errors/latency hurt UX. A
bank would invert this to **PC/EC**. The "Searched" ack is synchronous
(acknowledgement) but the *effect* is eventual (visibility) — these are different
guarantees.

---

## 12. Master-slave replication & quorum

- **Master-slave:** batched writes → master; cache-miss reads → **async replicas**
  (read scaling + failover). Async replication adds bounded **replication lag** —
  one more staleness term, fine for approximate counts. The master is the single
  writer, but batching keeps its load tiny, so no sharding needed.
- **Quorum (discussion):** in a leaderless store, **R + W > N** guarantees strong
  consistency (read/write sets overlap). We're leader-based (Postgres), so we
  don't tune quorum — but if we used Cassandra we'd pick **low R/W** (eventual,
  fast, available) to match our PA/EL posture.

Sync replication / high quorum ⇒ PC/EC; async replication / low quorum ⇒ PA/EL
(us). These are just **mechanisms implementing the CAP/PACELC choice.**

---

## 13. Suggestion serving: trie + top-k (`trie.py`)

A **trie** gives O(prefix length) lookup, independent of dataset size. We store a
precomputed **top-k candidate pool** at each node so a lookup is "walk + return"
instead of re-ranking the subtree.

**We do NOT precompute all prefixes** (tens of millions of nodes = several GB,
wasteful). Instead:
- **Precompute** candidate pools only for **short prefixes (≤3 chars, ~50k)** —
  few but broad/hot/expensive-to-compute live.
- **Lazily compute** longer prefixes on demand (tiny subtrees) and let the Redis
  cache hold the result. By Pareto, only the hot fraction is ever requested, so
  the millions nobody types cost nothing.
- The pool stores candidate **strings**; counts/recency are read **live** at query
  time, so ranking is always current. The same structure serves both `count` and
  `hybrid` modes (rank at query time). Pools + decay are refreshed periodically
  (`TRIE_REFRESH_SEC`); counts update live on each flush.

Alternative: `LIKE 'pre%' ORDER BY count` straight from Postgres (simpler, used as
fallback) — fine since the cache absorbs ~90% of reads, but slower for broad
prefixes.

---

## 14. Trending / recency-aware ranking (`ranking.py`, `trending.py`)

Basic ranking sorts by all-time `count` — which over-rewards historical
popularity. The enhanced ranking blends popularity with **recency**, answering the
assignment's five questions:

1. **How recent searches are tracked:** a per-query **time-decayed `recent_score`**
   (one number + `last_searched`), preferred over sliding-window buckets
   (less memory, smooth, no window cliff).
2. **How recency affects ranking:** `hybrid = w_pop·log(1+count) + w_rec·recent_score`
   (log-scale tames the power law). Same `/suggest` API, `mode=count|hybrid`.
   Trending panel sorts by `recent_score` alone.
3. **Avoiding permanent over-ranking:** **exponential decay** —
   `score = old·e^(−λ·Δt) + 1`, λ from a 1-hour half-life. A spike fades once
   searches stop (same "aging" idea as LFU). Decay is applied **at read time** for
   trending and **in SQL on each flush** for stored scores, so even queries that
   went quiet decay down.
4. **Cache update on ranking change:** TTL only — trending uses a **short TTL
   (~8s)**, not targeted invalidation (kept simple).
5. **Trade-offs:** decay = low memory/complexity, smooth; short trending TTL trades
   a little staleness for freshness; we avoid targeted invalidation for simplicity.

Demonstrated: a brand-new query (`ipl 2026 final`) submitted 60× rose to **#1
trending** and entered hybrid suggestions, while remaining low in `count` mode.

---

## 15. Master trade-off table

| Topic | Decision | Rejected alternative & why |
|---|---|---|
| Caching | cache reads | no cache → DB-bound p95 |
| Cache locality | global | local → coherence/duplication |
| Cache topology | distributed (3) | single → SPOF/stampede |
| Routing | consistent hashing + vnodes | round-robin (no locality), `%N` (mass remap) |
| App LB | round-robin | sticky → hot spots |
| Eviction | LRU | LFU (needs aging), FIFO/random |
| Invalidation | write-around + short jittered TTL | write-through (costly), targeted (complex) |
| Writes | write-back batching | sync per-write (DB-bound) |
| Store | PostgreSQL (B-tree) | NoSQL/LSM (unneeded after batching) |
| Consistency | PA/EL (eventual) | PC/EC (latency cost; not needed) |
| Replication | master + async replicas | sync (latency); single (no read scale) |
| Serving | trie + lazy top-k | full precompute (GB waste); DB-only (slow broad) |
| Ranking | count + decayed hybrid | raw recent counter (over-ranks spikes) |

---

## 16. Known limitations (honest)
- **Hot-key skew:** a single scorching prefix lives on one node regardless of
  sharding (replicate hot keys / add L1 to fix). Not handled.
- **Buffer crash window:** ≤1 flush of counts can be lost (accepted; WAL/queue is
  the upgrade).
- **Trie recency between rebuilds** is approximate (rough bump); the periodic
  rebuild + decay-at-read for trending correct it. Bounded staleness.
- **At assignment scale** a single Redis would suffice; distribution is for
  resilience/scale/learning, not capacity.
```
