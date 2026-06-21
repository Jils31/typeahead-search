// Central config. Every tunable maps to a decision in DESIGN.md.
const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

const int = (k, d) => parseInt(process.env[k] ?? d, 10);
const flt = (k, d) => parseFloat(process.env[k] ?? d);

module.exports = {
  pg: {
    host: process.env.PG_HOST || 'localhost',
    port: int('PG_PORT', 5433),
    user: process.env.PG_USER || 'typeahead',
    password: process.env.PG_PASSWORD || 'typeahead',
    database: process.env.PG_DB || 'typeahead',
  },

  // distributed cache nodes (host:port), routed by consistent hashing
  cacheNodes: (process.env.CACHE_NODES || 'localhost:6390,localhost:6391,localhost:6392')
    .split(',').map((s) => s.trim()).filter(Boolean),
  vnodes: int('VNODES', 150),

  // invalidation: TTL is the primary mechanism (jittered)
  ttlSuggest: int('TTL_SUGGEST', 45),
  ttlTrend: int('TTL_TREND', 8),
  ttlJitter: flt('TTL_JITTER', 0.2),

  // write-back batching
  batchSizeN: int('BATCH_SIZE_N', 500),
  flushIntervalMs: flt('FLUSH_INTERVAL_T', 1.0) * 1000,

  // trie / suggestions
  topK: int('TOP_K', 10),
  precomputePrefixLen: int('PRECOMPUTE_PREFIX_LEN', 3),
  trieRefreshSec: int('TRIE_REFRESH_SEC', 120),

  // ranking
  rankingMode: process.env.RANKING_MODE || 'hybrid',
  wPop: flt('W_POP', 1.0),
  wRec: flt('W_REC', 2.0),
  decayHalflifeSec: flt('DECAY_HALFLIFE_SEC', 3600),
};
