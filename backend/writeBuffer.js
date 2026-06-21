// Write-back batch buffer: aggregates search counts in memory and flushes to
// Postgres on size (batchSizeN) or interval (flushIntervalMs). A crash loses at
// most one un-flushed window — acceptable for approximate counts (DESIGN.md).
const config = require('./config');
const metrics = require('./metrics');

class WriteBuffer {
  constructor(flushHandler) {
    this.buf = new Map();
    this.handler = flushHandler;
    this.timer = null;
    this.flushing = false;
  }

  add(query) {
    query = query.toLowerCase().trim();
    if (!query) return;
    metrics.m.searchesReceived++;
    this.buf.set(query, (this.buf.get(query) || 0) + 1);
    if (this.buf.size >= config.batchSizeN) this.flush();
  }

  async flush() {
    if (this.flushing || this.buf.size === 0) return;
    this.flushing = true;
    const window = this.buf;     // swap out the current window...
    this.buf = new Map();        // ...start a fresh one (no await between)
    try {
      await this.handler(window);
    } catch (e) {
      // don't let a transient flush error crash the process (window is lost,
      // same as the documented crash trade-off)
      console.error('[flush] error:', e.message);
    } finally {
      this.flushing = false;
    }
  }

  start() { this.timer = setInterval(() => this.flush(), config.flushIntervalMs); }

  async stop() {
    if (this.timer) clearInterval(this.timer);
    await this.flush();          // final drain
  }

  pending() { return this.buf.size; }
}

module.exports = { WriteBuffer };
