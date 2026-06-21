# Design Decisions & Trade-offs

The system has two competing workloads on one dataset: **reads** (suggestions,
fired on every keystroke) and **writes** (count updates on every submit). Reads
dominate ~5–10:1. Most decisions follow from optimizing reads while keeping
writes cheap, and from the fact that suggestion data tolerates staleness.

## Why cache (Pareto)
~10M DAU × 4 searches/day ≈ 460 writes/s avg; ~5 suggestion reads per search →
~2,300 reads/s avg, ~7k/s peak. Query popularity is Zipf: a small set of
prefixes serve most traffic, so caching the hot set yields a high hit rate and
keeps reads off the DB. Without it, every keystroke is a prefix scan + sort on
Postgres and p95 degrades under load.

## Cache: global, distributed
- **Local vs global → global.** A shared cache means one copy and one place to
  invalidate. Per-node local caches duplicate data and need cross-node
  invalidation (broadcast/poll) to stay coherent.
- **Single vs distributed → distributed (3 Redis).** A single node would handle
  this dataset and QPS fine; we distribute for **fault tolerance** (one node
  down loses 1/3 of the cache, not all of it → no full stampede to the DB) and
  headroom, not raw throughput.

## Routing: consistent hashing
Cache nodes hold different keys, so routing must be deterministic per key
(round-robin can't — it loses *where* a key is). `hash % N` is deterministic but
remaps almost all keys when N changes. A hash ring with virtual nodes remaps
only ~K/N keys on add/remove. Trade-off: more code than `% N`, but stable under
membership changes. App tier itself is stateless → plain round-robin LB.

## Eviction: LRU
Hot prefixes are re-hit constantly, so LRU keeps them and evicts the rare long
tail. LFU fits stable Zipf popularity slightly better but needs an aging factor
(else an old viral query squats forever on its count) — not worth the
complexity, especially since short TTLs mean eviction rarely fires.

## Invalidation: write-around + short TTL
- A count change can stale many prefix entries across nodes. **TTL** (≈45s
  suggestions, ≈8s trending, jittered to avoid synchronized expiry) bounds
  staleness with zero tracking.
- **Write-around:** searches update the store, not the cache; the cache refills
  lazily on the next read miss. We avoid **write-through** because the cache
  value is a computed top-10 — recomputing it on every write (most of which
  don't change the ranking) is wasted work.
- Targeted invalidation was deliberately skipped (complexity not justified;
  short TTL covers freshness).

## Consistency: eventual (PA/EL)
Suggestion data is approximate popularity — staleness is harmless, latency and
availability are not. So during a partition we serve stale rather than error
(AP), and in normal operation we serve from cache rather than re-check the DB
(EL). The `Searched` response is a synchronous *acknowledgement*; the count
update behind it is asynchronous.

## Writes: write-back batching
`POST /search` increments an in-memory map and returns immediately. The buffer
aggregates duplicates and flushes on size **or** interval into one additive
UPSERT (`count = count + EXCLUDED.count`, so concurrent flushes add instead of
clobber). This collapses ~20k searches into a few thousand rows in ~10
transactions. **Trade-off:** a crash loses at most one un-flushed window —
acceptable for approximate, self-healing counts; durability would need a WAL or
a durable queue (Kafka/Redis Streams).

## Store: PostgreSQL
Workload looks write-heavy → usually argues for a write-optimized LSM/NoSQL
store. But batching already cut DB writes ~60×, removing that pressure, so we
use Postgres: B-tree index serves `LIKE 'pre%'` range scans, ACID for the count
truth, read replicas for scaling. We'd move to an LSM store (Cassandra) only if
writes were un-batchable and massive, or we needed sharding + quorum. (LSM:
writes go to a memtable + WAL, flush to immutable SSTables, compaction merges —
our batch buffer is the same idea at the app layer. Redis, the cache, is the one
NoSQL store we use.)

## Replication & quorum
Writes → master; cache-miss reads → async replicas (read scaling + failover) at
the cost of bounded replication lag (acceptable). Quorum (`R+W>N` for strong
consistency) is a leaderless-store concern; with Postgres we don't tune it, but
a Cassandra variant would use low R/W to match the PA/EL choice.

## Serving: trie with lazy top-k
Suggestions come from an in-memory trie (O(prefix length) lookup). We precompute
a candidate pool only for short prefixes (≤3 chars — few but broad/hot);
longer prefixes are computed on demand. We do **not** precompute every prefix
(tens of millions of nodes). Counts/recency are read live, so the same structure
serves both ranking modes.

## Ranking: count vs hybrid (recency)
- **count** (basic): sort by all-time `count`.
- **hybrid**: `w_pop·log(count) + w_rec·recent_score`. `recent_score` increments
  per search and **decays exponentially** (1h half-life), so a short-lived spike
  fades instead of ranking forever (same aging idea as LFU). Decay is applied at
  read time for trending so quiet queries drop off. Freshness comes from a short
  TTL, not extra invalidation.

## Summary

| Topic | Decision | Rejected alternative |
|---|---|---|
| Caching | cache reads | no cache → DB-bound p95 |
| Locality | global | local → duplication + coherence |
| Topology | distributed (3) | single → SPOF/stampede |
| Routing | consistent hashing | round-robin (no locality), `%N` (mass remap) |
| Eviction | LRU | LFU (needs aging) |
| Invalidation | write-around + short TTL | write-through (costly), targeted (complex) |
| Writes | write-back batching | sync per-write (DB-bound) |
| Store | PostgreSQL | NoSQL/LSM (unneeded after batching) |
| Consistency | eventual / PA-EL | strong (latency cost) |
| Serving | trie + lazy top-k | full precompute (waste), DB-only (slow) |
| Ranking | count + decayed hybrid | raw recent counter (over-ranks spikes) |

## Known limits
- A single scorching-hot prefix still lands on one node (hot-key problem) —
  would need hot-key replication or an L1.
- Crash loses ≤1 flush window of counts.
- Trie recency is approximate between periodic rebuilds.
