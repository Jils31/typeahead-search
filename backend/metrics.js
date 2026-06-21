// Counters + latency samples exposed at /metrics.
const m = {
  cacheHits: 0,
  cacheMisses: 0,
  dbReads: 0,            // suggestion-path DB reads (0 by design: trie serves misses)
  dbWrites: 0,           // rows written via batch flush
  dbWriteBatches: 0,     // flush transactions
  searchesReceived: 0,   // POST /search calls before aggregation
};

const LAT_CAP = 5000;
const latency = [];

function recordSuggestLatency(ms) {
  latency.push(ms);
  if (latency.length > LAT_CAP) latency.shift();
}

function percentile(p) {
  if (!latency.length) return 0;
  const sorted = [...latency].sort((a, b) => a - b);
  const k = Math.max(0, Math.min(sorted.length - 1, Math.round((p / 100) * (sorted.length - 1))));
  return Math.round(sorted[k] * 1000) / 1000;
}

function snapshot() {
  const total = m.cacheHits + m.cacheMisses;
  return {
    cache_hits: m.cacheHits,
    cache_misses: m.cacheMisses,
    cache_hit_rate: total ? Math.round((m.cacheHits / total) * 10000) / 10000 : 0,
    db_reads: m.dbReads,
    db_writes: m.dbWrites,
    db_write_batches: m.dbWriteBatches,
    searches_received: m.searchesReceived,
    write_reduction_factor: m.dbWrites ? Math.round((m.searchesReceived / m.dbWrites) * 100) / 100 : null,
    suggest_latency_ms: {
      samples: latency.length,
      p50: percentile(50),
      p95: percentile(95),
      p99: percentile(99),
    },
  };
}

module.exports = { m, recordSuggestLatency, snapshot };
