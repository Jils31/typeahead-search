// PostgreSQL primary store (durable source of truth for counts).
// Additive UPSERT so concurrent flushes add instead of clobbering; recent_score
// is decayed in SQL on each flush.
const { Pool } = require('pg');
const config = require('./config');
const metrics = require('./metrics');
const { LAMBDA } = require('./ranking');

let pool;

function initPool() {
  pool = new Pool({ ...config.pg, max: 10 });
}
async function closePool() { if (pool) await pool.end(); }

async function initSchema() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS queries (
      query         TEXT PRIMARY KEY,
      count         BIGINT NOT NULL DEFAULT 0,
      recent_score  DOUBLE PRECISION NOT NULL DEFAULT 0,
      last_searched TIMESTAMPTZ NOT NULL DEFAULT now()
    );`);
  await pool.query(`CREATE INDEX IF NOT EXISTS idx_query_prefix ON queries (query text_pattern_ops);`);
  await pool.query(`CREATE INDEX IF NOT EXISTS idx_recent_score ON queries (recent_score DESC);`);
}

async function truncate() { await pool.query('TRUNCATE queries;'); }

async function countRows() {
  const r = await pool.query('SELECT count(*)::int AS n FROM queries;');
  return r.rows[0].n;
}

// initial dataset ingestion: rows = [[query, count], ...]
async function bulkLoad(rows) {
  const CHUNK = 1000;
  for (let i = 0; i < rows.length; i += CHUNK) {
    const slice = rows.slice(i, i + CHUNK);
    const vals = [];
    const params = [];
    slice.forEach(([q, c], j) => {
      vals.push(`($${2 * j + 1}, $${2 * j + 2})`);
      params.push(q, c);
    });
    await pool.query(
      `INSERT INTO queries (query, count) VALUES ${vals.join(',')} ON CONFLICT (query) DO NOTHING;`,
      params
    );
  }
}

// apply one flush window: Map<query, increment>. Each search also +1 to recency.
async function batchUpsert(windowMap) {
  const entries = [...windowMap.entries()];
  if (!entries.length) return 0;
  const vals = [];
  const params = [];
  // separate params for count (bigint) and recent_score (double) so Postgres
  // doesn't try to deduce one shared type for both columns
  entries.forEach(([q, inc], j) => {
    vals.push(`($${3 * j + 1}, $${3 * j + 2}, $${3 * j + 3}, now())`);
    params.push(q, inc, inc);
  });
  await pool.query(
    `INSERT INTO queries (query, count, recent_score, last_searched)
     VALUES ${vals.join(',')}
     ON CONFLICT (query) DO UPDATE SET
       count = queries.count + EXCLUDED.count,
       recent_score = queries.recent_score
         * exp(-${LAMBDA} * EXTRACT(EPOCH FROM (now() - queries.last_searched)))
         + EXCLUDED.recent_score,
       last_searched = now();`,
    params
  );
  metrics.m.dbWrites += entries.length;
  metrics.m.dbWriteBatches += 1;
  return entries.length;
}

// load every row for trie build: [query, count, recent_score, age_seconds]
async function loadAll() {
  const r = await pool.query(
    `SELECT query, count, recent_score, EXTRACT(EPOCH FROM (now() - last_searched)) AS age FROM queries;`
  );
  return r.rows.map((x) => [x.query, Number(x.count), Number(x.recent_score), Number(x.age) || 0]);
}

// top rows by stored recent_score: [query, recent_score, age_seconds]
async function trendingCandidates(limit) {
  const r = await pool.query(
    `SELECT query, recent_score, EXTRACT(EPOCH FROM (now() - last_searched)) AS age
     FROM queries ORDER BY recent_score DESC LIMIT $1;`,
    [limit]
  );
  return r.rows.map((x) => [x.query, Number(x.recent_score), Number(x.age) || 0]);
}

module.exports = { initPool, closePool, initSchema, truncate, countRows, bulkLoad, batchUpsert, loadAll, trendingCandidates };
