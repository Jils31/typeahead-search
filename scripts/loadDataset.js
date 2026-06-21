// Dataset ingestion -> Postgres.
// AOL query log: derive counts by aggregation (COUNT per normalized query).
//   node scripts/loadDataset.js --dir files/aol_data --min-count 2 --out files/aol_agg.tsv
//   node scripts/loadDataset.js --agg-file files/aol_agg.tsv --top 1000000 --min-count 3
//   node scripts/loadDataset.js --synthetic 120000        # no download
// For the full 35M-row aggregation, give Node more heap:
//   node --max-old-space-size=4096 scripts/loadDataset.js --dir files/aol_data --out files/aol_agg.tsv
const fs = require('fs');
const zlib = require('zlib');
const readline = require('readline');
const path = require('path');
const store = require('../backend/store');

const arg = (name) => { const i = process.argv.indexOf(name); return i >= 0 ? process.argv[i + 1] : undefined; };
const has = (name) => process.argv.includes(name);

function openStream(p) {
  const s = fs.createReadStream(p);
  return p.endsWith('.gz') ? s.pipe(zlib.createGunzip()) : s;
}

async function aggregateInto(file, counter) {
  const rl = readline.createInterface({ input: openStream(file), crlfDelay: Infinity });
  let header = null, qi = 1, delim = '\t', n = 0;
  for await (const line of rl) {
    if (header === null) {
      delim = (line.split('\t').length - 1) >= (line.split(',').length - 1) ? '\t' : ',';
      const cols = line.split(delim).map((c) => c.trim().toLowerCase());
      qi = cols.indexOf('query');
      if (qi < 0) qi = cols.length > 1 ? 1 : 0;
      header = cols;
      continue;
    }
    const parts = line.split(delim);
    if (parts.length <= qi) continue;
    const q = parts[qi].trim().toLowerCase();
    if (!q || q === '-') continue;
    counter.set(q, (counter.get(q) || 0) + 1);
    n++;
  }
  console.log(`  ${path.basename(file)}: ${n.toLocaleString()} query rows (delim=${JSON.stringify(delim)}, col=${qi})`);
}

async function aggregatePaths(paths, minCount) {
  const counter = new Map();
  for (const p of paths) await aggregateInto(p, counter);
  const rows = [];
  for (const [q, c] of counter) if (c >= minCount) rows.push([q, c]);
  return rows;
}

function writeTsv(rows, file) {
  const out = fs.createWriteStream(file);
  for (const [q, c] of rows) out.write(`${q}\t${c}\n`);
  out.end();
  return new Promise((res) => out.on('finish', res));
}

async function readAgg(file, top, minCount) {
  const rows = [];
  const rl = readline.createInterface({ input: fs.createReadStream(file), crlfDelay: Infinity });
  for await (const line of rl) {
    const i = line.lastIndexOf('\t');
    if (i < 0) continue;
    const q = line.slice(0, i), c = parseInt(line.slice(i + 1), 10);
    if (q && c >= minCount) rows.push([q, c]);
  }
  if (top) { rows.sort((a, b) => b[1] - a[1]); return rows.slice(0, top); }
  return rows;
}

function synthetic(n) {
  const heads = ['how to', 'best', 'buy', 'cheap', 'free', 'download', 'what is', 'iphone', 'samsung',
    'java', 'python', 'amazon', 'google', 'weather', 'news', 'movie', 'song', 'recipe', 'near me', 'online', 'review'];
  const tails = ['tutorial', 'price', '2026', 'review', 'online', 'near me', 'for sale', 'guide', 'vs', 'app',
    'login', 'meaning', 'today', 'free', 'pro max', 'case', 'charger', 'stock', 'results', 'live', 'download', 'lyrics'];
  const mids = ['', 'best ', 'new ', 'cheap ', 'top ', 'the '];
  const out = new Map();
  let rank = 1, i = 0;
  while (out.size < n) {
    const h = heads[i % heads.length];
    const m = mids[Math.floor(i / heads.length) % mids.length];
    const t = tails[Math.floor(i / (heads.length * mids.length)) % tails.length];
    const suffix = i >= 5000 ? ` ${Math.floor(i / 5000)}` : '';
    const q = `${h} ${m}${t}${suffix}`.trim();
    if (!out.has(q)) { out.set(q, Math.max(1, Math.floor(10_000_000 / Math.pow(rank, 1.1)))); rank++; }
    i++;
  }
  return [...out.entries()];
}

async function main() {
  const file = arg('--file'), dir = arg('--dir'), synth = arg('--synthetic'), aggFile = arg('--agg-file');
  const out = arg('--out'), top = arg('--top') ? parseInt(arg('--top'), 10) : null;
  const minCount = arg('--min-count') ? parseInt(arg('--min-count'), 10) : 1;

  if (!file && !dir && !synth && !aggFile) {
    console.error('provide --file, --dir, --agg-file, or --synthetic');
    process.exit(1);
  }

  console.log('aggregating...');
  let rows;
  if (synth) rows = synthetic(parseInt(synth, 10));
  else if (aggFile) rows = await readAgg(aggFile, top, minCount);
  else {
    let paths;
    if (dir) paths = fs.readdirSync(dir).filter((f) => f.endsWith('.txt.gz') || f.endsWith('.txt')).sort().map((f) => path.join(dir, f));
    else paths = [file];
    rows = await aggregatePaths(paths, minCount);
    if (top) { rows.sort((a, b) => b[1] - a[1]); rows = rows.slice(0, top); }
  }
  console.log(`${rows.length.toLocaleString()} distinct queries to load`);

  if (out) { await writeTsv(rows, out); console.log(`wrote ${rows.length.toLocaleString()} rows to ${out}`); return; }

  store.initPool();
  try {
    await store.initSchema();
    if (!has('--no-truncate')) await store.truncate();
    await store.bulkLoad(rows);
    console.log(`done. rows in DB: ${(await store.countRows()).toLocaleString()}`);
  } finally {
    await store.closePool();
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
