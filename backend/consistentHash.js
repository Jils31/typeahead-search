// Consistent-hash ring (with virtual nodes) mapping a prefix key to a cache
// node. Adding/removing a node remaps only ~K/N keys, unlike `hash % N`.
const crypto = require('crypto');

// 64-bit hash from md5; BigInt so positions don't lose precision.
function hash(key) {
  const hex = crypto.createHash('md5').update(key).digest('hex').slice(0, 16);
  return BigInt('0x' + hex);
}

class ConsistentHashRing {
  constructor(vnodes = 150) {
    this.vnodes = vnodes;
    this.ring = new Map();      // pos(string) -> node
    this.sorted = [];           // sorted array of BigInt positions
    this.nodes = new Set();
  }

  addNode(node) {
    if (this.nodes.has(node)) return;
    this.nodes.add(node);
    for (let i = 0; i < this.vnodes; i++) {
      const pos = hash(`${node}#${i}`);
      this.ring.set(pos.toString(), node);
      this.sorted.push(pos);
    }
    this.sorted.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  }

  removeNode(node) {
    if (!this.nodes.has(node)) return;
    this.nodes.delete(node);
    for (let i = 0; i < this.vnodes; i++) {
      const pos = hash(`${node}#${i}`);
      this.ring.delete(pos.toString());
    }
    this.sorted = this.sorted.filter((p) => this.ring.has(p.toString()));
  }

  // first vnode clockwise from the key's hash (wraps around)
  _index(h) {
    let lo = 0, hi = this.sorted.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.sorted[mid] <= h) lo = mid + 1;
      else hi = mid;
    }
    return lo === this.sorted.length ? 0 : lo;
  }

  getNode(key) {
    if (!this.sorted.length) return null;
    return this.ring.get(this.sorted[this._index(hash(key))].toString());
  }

  debug(key) {
    const h = hash(key);
    const idx = this._index(h);
    const pos = this.sorted.length ? this.sorted[idx] : null;
    return {
      key,
      key_hash: h.toString(),
      owner_node: this.getNode(key),
      ring_position: pos ? pos.toString() : null,
      total_vnodes: this.sorted.length,
    };
  }

  distribution(keys) {
    const out = {};
    for (const n of this.nodes) out[n] = 0;
    for (const k of keys) {
      const n = this.getNode(k);
      if (n != null) out[n] = (out[n] || 0) + 1;
    }
    return out;
  }
}

module.exports = { ConsistentHashRing, hash };
