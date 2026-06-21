// In-memory trie for prefix top-k (O(prefix length) lookup).
// Precomputes a candidate pool only for short prefixes (<= precomputePrefixLen);
// longer prefixes are computed on demand. Counts/recency are read live, so the
// same structure serves both count and hybrid modes.
const config = require('./config');
const ranking = require('./ranking');

const MAX_QUERY_LEN = 100;
const CANDIDATE_POOL = 50;
const DFS_CAP = 3000;

class Node {
  constructor() {
    this.children = new Map();
    this.isWord = false;
    this.pool = null; // precomputed candidate query strings (shallow nodes only)
  }
}

class Trie {
  constructor() {
    this.root = new Node();
    this.words = new Map(); // query -> [count, recentScore]
  }

  _insert(query) {
    let node = this.root;
    for (const ch of query) {
      let nxt = node.children.get(ch);
      if (!nxt) { nxt = new Node(); node.children.set(ch, nxt); }
      node = nxt;
    }
    node.isWord = true;
  }

  // rows = [query, count, recentScore, ageSeconds]
  build(rows) {
    this.root = new Node();
    this.words = new Map();
    for (const [query, count, recent, age] of rows) {
      const q = query.slice(0, MAX_QUERY_LEN);
      if (!q) continue;
      this.words.set(q, [count, ranking.decay(recent, age)]);
      this._insert(q);
    }
    this.refreshPools();
  }

  _navigate(prefix) {
    let node = this.root;
    for (const ch of prefix) {
      node = node.children.get(ch);
      if (!node) return null;
    }
    return node;
  }

  _collect(node, prefix, cap) {
    const out = [];
    const stack = [[node, prefix]];
    while (stack.length && out.length < cap) {
      const [cur, pre] = stack.pop();
      if (cur.isWord && this.words.has(pre)) out.push(pre);
      for (const [ch, child] of cur.children) stack.push([child, pre + ch]);
    }
    return out;
  }

  refreshPools() { this._refresh(this.root, '', 0); }

  _refresh(node, prefix, depth) {
    if (depth <= config.precomputePrefixLen) {
      const words = this._collect(node, prefix, 10000);
      words.sort((a, b) => this.words.get(b)[0] - this.words.get(a)[0]);
      node.pool = words.slice(0, CANDIDATE_POOL);
      for (const [ch, child] of node.children) this._refresh(child, prefix + ch, depth + 1);
    }
  }

  getSuggestions(prefix, k, mode) {
    prefix = prefix.toLowerCase().trim();
    if (!prefix) return [];
    const node = this._navigate(prefix);
    if (!node) return [];
    const candidates = node.pool != null ? node.pool : this._collect(node, prefix, DFS_CAP);
    candidates.sort((a, b) => {
      const A = this.words.get(a), B = this.words.get(b);
      return ranking.scoreFor(mode, B[0], B[1]) - ranking.scoreFor(mode, A[0], A[1]);
    });
    return candidates.slice(0, k).map((q) => ({ query: q, count: this.words.get(q)[0] }));
  }

  // apply a flush window: counts exact-additive, recency rough-bumped
  applyUpdates(windowMap) {
    for (const [qRaw, inc] of windowMap) {
      const q = qRaw.slice(0, MAX_QUERY_LEN);
      if (!q) continue;
      const cur = this.words.get(q);
      if (cur) { cur[0] += inc; cur[1] += inc; }
      else { this.words.set(q, [inc, inc]); this._insert(q); }
    }
  }

  size() { return this.words.size; }
}

module.exports = { Trie };
