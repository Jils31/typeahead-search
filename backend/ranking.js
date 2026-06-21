// Ranking: basic (all-time count) vs hybrid (count + time-decayed recency).
// recent_score halves every decayHalflifeSec of inactivity, so a short-lived
// spike fades instead of ranking forever.
const config = require('./config');

const LAMBDA = Math.log(2) / config.decayHalflifeSec;

function decay(recentScore, dtSeconds) {
  if (dtSeconds <= 0) return recentScore;
  return recentScore * Math.exp(-LAMBDA * dtSeconds);
}

function hybridScore(count, recentScore) {
  return config.wPop * Math.log1p(count) + config.wRec * recentScore;
}

function scoreFor(mode, count, recentScore) {
  return mode === 'count' ? count : hybridScore(count, recentScore);
}

module.exports = { LAMBDA, decay, hybridScore, scoreFor };
