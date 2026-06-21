// Distributed cache over N Redis nodes, routed by consistent hashing.
// Cache-aside reads, write-around writes, jittered-TTL invalidation.
const Redis = require('ioredis');
const config = require('./config');
const metrics = require('./metrics');
const { ConsistentHashRing } = require('./consistentHash');

const clients = new Map();          // node -> ioredis client
let ring = new ConsistentHashRing(config.vnodes);

const redisKey = (prefix, mode) => `sugg:${mode}:${prefix}`;

async function init() {
  ring = new ConsistentHashRing(config.vnodes);
  for (const node of config.cacheNodes) {
    const [host, port] = node.split(':');
    clients.set(node, new Redis({ host, port: parseInt(port, 10), lazyConnect: false }));
    ring.addNode(node);
  }
  // fail fast if a node is unreachable
  for (const c of clients.values()) await c.ping();
}

async function close() {
  for (const c of clients.values()) c.disconnect();
}

function jittered(ttl) {
  const delta = ttl * config.ttlJitter;
  return Math.max(1, Math.round(ttl + (Math.random() * 2 - 1) * delta));
}

async function getSuggestions(prefix, mode) {
  const node = ring.getNode(prefix);
  const raw = await clients.get(node).get(redisKey(prefix, mode));
  if (raw == null) {
    metrics.m.cacheMisses++;
    return { suggestions: null, node, hit: false };
  }
  metrics.m.cacheHits++;
  return { suggestions: JSON.parse(raw), node, hit: true };
}

async function setSuggestions(prefix, mode, suggestions, ttl) {
  const node = ring.getNode(prefix);
  await clients.get(node).set(redisKey(prefix, mode), JSON.stringify(suggestions), 'EX', jittered(ttl));
  return node;
}

async function getRaw(key) {
  return clients.get(ring.getNode(key)).get(key);
}

async function setRaw(key, value, ttl) {
  const node = ring.getNode(key);
  await clients.get(node).set(key, value, 'EX', jittered(ttl));
  return node;
}

async function debug(prefix, mode) {
  const info = ring.debug(prefix);
  const node = info.owner_node;
  const present = node ? (await clients.get(node).get(redisKey(prefix, mode))) != null : false;
  return { ...info, mode, redis_key: redisKey(prefix, mode), currently_cached: present, hit_or_miss: present ? 'HIT' : 'MISS' };
}

module.exports = { init, close, getSuggestions, setSuggestions, getRaw, setRaw, debug, getRing: () => ring };
