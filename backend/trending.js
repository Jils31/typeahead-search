// Trending = global top-N by time-decayed recent_score, decayed at read time so
// quiet queries fall off. Cached with a short TTL.
const cache = require('./cache');
const config = require('./config');
const ranking = require('./ranking');
const store = require('./store');

async function getTrending(n) {
  const key = `trending:${n}`;
  const raw = await cache.getRaw(key);
  if (raw != null) return JSON.parse(raw);

  const candidates = await store.trendingCandidates(Math.max(100, n * 5));
  const decayed = candidates
    .map(([q, rs, age]) => [q, ranking.decay(rs, age)])
    .sort((a, b) => b[1] - a[1])
    .slice(0, n);

  await cache.setRaw(key, JSON.stringify(decayed), config.ttlTrend);
  return decayed;
}

module.exports = { getTrending };
