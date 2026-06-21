// FastAPI -> Express. Read path: cache -> trie. Write path: buffer -> batch
// flush -> Postgres + trie. Endpoints: /suggest /search /cache/debug /cache/ring
// /trending /metrics.
const path = require('path');
const express = require('express');

const config = require('./config');
const metrics = require('./metrics');
const store = require('./store');
const cache = require('./cache');
const trending = require('./trending');
const { Trie } = require('./trie');
const { WriteBuffer } = require('./writeBuffer');

const FRONTEND_DIR = path.join(__dirname, '..', 'frontend');

const state = { trie: new Trie(), buffer: null };

// on every flush: durable additive UPSERT + live trie update
async function flushHandler(window) {
  await store.batchUpsert(window);
  state.trie.applyUpdates(window);
}

async function rebuildTrie() {
  const rows = await store.loadAll();
  const t = new Trie();
  t.build(rows);            // synchronous; runs at startup / on refresh
  state.trie = t;
}

const app = express();
app.use(express.json());

app.get('/suggest', async (req, res) => {
  const t0 = process.hrtime.bigint();
  let mode = (req.query.mode || config.rankingMode).toLowerCase();
  if (mode !== 'count' && mode !== 'hybrid') mode = config.rankingMode;
  const prefix = (req.query.q || '').toLowerCase().trim();

  if (!prefix) {
    metrics.recordSuggestLatency(Number(process.hrtime.bigint() - t0) / 1e6);
    return res.json({ prefix, mode, source: 'empty', suggestions: [] });
  }

  const { suggestions: cached, node, hit } = await cache.getSuggestions(prefix, mode);
  let result, source;
  if (hit) {
    result = cached; source = 'cache';
  } else {
    result = state.trie.getSuggestions(prefix, config.topK, mode);
    await cache.setSuggestions(prefix, mode, result, config.ttlSuggest);
    source = 'trie';
  }
  metrics.recordSuggestLatency(Number(process.hrtime.bigint() - t0) / 1e6);
  res.json({ prefix, mode, source, node, suggestions: result });
});

app.post('/search', (req, res) => {
  // synchronous ack; count update is async (write-back)
  state.buffer.add(String((req.body && req.body.query) || ''));
  res.json({ message: 'Searched' });
});

app.get('/cache/debug', async (req, res) => {
  const mode = (req.query.mode || config.rankingMode).toLowerCase();
  res.json(await cache.debug(String(req.query.prefix || '').toLowerCase().trim(), mode));
});

app.get('/cache/ring', (req, res) => {
  const sample = parseInt(req.query.sample || '2000', 10);
  const letters = 'abcdefghijklmnopqrstuvwxyz';
  const keys = [];
  for (let i = 0; i < sample; i++) {
    keys.push(letters[i % 26] + letters[(Math.floor(i / 26)) % 26] + letters[(Math.floor(i / 676)) % 26]);
  }
  res.json({
    nodes: config.cacheNodes,
    vnodes_per_node: config.vnodes,
    sample_size: sample,
    distribution: cache.getRing().distribution(keys),
  });
});

app.get('/trending', async (req, res) => {
  const n = parseInt(req.query.n || '10', 10);
  const top = await trending.getTrending(n);
  res.json({ trending: top.map(([query, score]) => ({ query, score: Math.round(score * 10000) / 10000 })) });
});

app.get('/metrics', (req, res) => {
  res.json({ ...metrics.snapshot(), trie_size: state.trie.size(), buffer_pending: state.buffer.pending(), cache_nodes: config.cacheNodes });
});

app.use('/static', express.static(FRONTEND_DIR));
app.get('/', (req, res) => res.sendFile(path.join(FRONTEND_DIR, 'index.html')));

async function main() {
  store.initPool();
  await store.initSchema();
  await cache.init();
  await rebuildTrie();
  state.buffer = new WriteBuffer(flushHandler);
  state.buffer.start();
  setInterval(() => rebuildTrie().catch((e) => console.error('[trie refresh]', e.message)), config.trieRefreshSec * 1000);

  app.listen(8000, '127.0.0.1', () =>
    console.log(`[startup] trie loaded with ${state.trie.size()} queries; cache nodes=${config.cacheNodes}`));
}

main().catch((e) => { console.error('startup failed:', e); process.exit(1); });
