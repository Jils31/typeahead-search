// Performance report: /suggest latency, cache hit rate, write reduction.
//   node scripts/benchmark.js --reads 8000 --writes 20000
const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 ? process.argv[i + 1] : d; };

const BASE = arg('--base', 'http://localhost:8000');
const READS = parseInt(arg('--reads', '5000'), 10);
const WRITES = parseInt(arg('--writes', '20000'), 10);
const MODE = arg('--mode', 'hybrid');
const CONC = parseInt(arg('--concurrency', '20'), 10);

const HOT = ['a', 'i', 'ip', 'be', 'ho', 'wh', 'fr', 'do', 'bu', 'ne', 'mo', 'py', 'ja'];
const LETTERS = 'abcdefghijklmnopqrstuvwxyz';
const rand = (arr) => arr[Math.floor(Math.random() * arr.length)];

function zipfPrefix() {
  if (Math.random() < 0.8) return rand(HOT);               // 80% -> hot prefixes (Pareto)
  let s = ''; const len = 2 + Math.floor(Math.random() * 3);
  for (let i = 0; i < len; i++) s += rand(LETTERS.split(''));
  return s;
}

async function runReads(count) {
  const lat = [];
  for (let i = 0; i < count; i++) {
    const t0 = performance.now();
    await fetch(`${BASE}/suggest?q=${encodeURIComponent(zipfPrefix())}&mode=${MODE}`);
    lat.push(performance.now() - t0);
  }
  return lat;
}

async function runWrites(count) {
  const words = ['phone', 'tutorial', 'price', 'news', 'app'];
  for (let i = 0; i < count; i++) {
    await fetch(`${BASE}/search`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: `${zipfPrefix()} ${rand(words)}` }),
    });
  }
}

const pct = (xs, p) => { const s = [...xs].sort((a, b) => a - b); const k = Math.max(0, Math.min(s.length - 1, Math.round(p / 100 * (s.length - 1)))); return s.length ? Math.round(s[k] * 1000) / 1000 : 0; };
const metrics = async () => (await fetch(`${BASE}/metrics`)).json();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  console.log('== WRITE REDUCTION (batching) ==');
  const m0 = await metrics();
  await runWrites(WRITES);
  await sleep(2000);
  const m1 = await metrics();
  const recv = m1.searches_received - m0.searches_received;
  const wrote = m1.db_writes - m0.db_writes;
  const batches = m1.db_write_batches - m0.db_write_batches;
  console.log(`  searches sent   : ${recv.toLocaleString()}`);
  console.log(`  db rows written : ${wrote.toLocaleString()}`);
  console.log(`  flush batches   : ${batches.toLocaleString()}`);
  if (wrote) console.log(`  write reduction : ${(recv / wrote).toFixed(1)}x fewer rows, ${Math.round(recv / Math.max(1, batches))}x fewer transactions`);

  console.log('\n== READ LATENCY + HIT RATE ==');
  await runReads(Math.floor(READS / 5));            // warm the cache
  const mb = await metrics();
  const chunks = await Promise.all(Array.from({ length: CONC }, () => runReads(Math.floor(READS / CONC))));
  const lat = chunks.flat();
  const ma = await metrics();
  const hits = ma.cache_hits - mb.cache_hits;
  const misses = ma.cache_misses - mb.cache_misses;
  const hr = (hits + misses) ? hits / (hits + misses) : 0;
  console.log(`  requests        : ${lat.length.toLocaleString()}`);
  console.log(`  client p50/p95  : ${pct(lat, 50)} ms / ${pct(lat, 95)} ms`);
  console.log(`  client p99      : ${pct(lat, 99)} ms`);
  console.log(`  cache hit rate  : ${(hr * 100).toFixed(1)}%  (${hits.toLocaleString()} hits / ${misses.toLocaleString()} misses)`);
  console.log(`  server p95      : ${ma.suggest_latency_ms.p95} ms`);
  console.log(`  trie size       : ${ma.trie_size.toLocaleString()}`);
}

main().catch((e) => { console.error(e); process.exit(1); });
