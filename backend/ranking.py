"""Ranking: basic (all-time count) vs hybrid (count blended with a time-decayed
recency score). recent_score halves every DECAY_HALFLIFE_SEC of inactivity, so
a short-lived spike fades instead of ranking forever."""
import math

from . import config

# lambda such that score halves every DECAY_HALFLIFE_SEC of inactivity:
#   exp(-lambda * halflife) = 0.5  ->  lambda = ln(2) / halflife
_LAMBDA = math.log(2) / config.DECAY_HALFLIFE_SEC


def decay(recent_score: float, dt_seconds: float) -> float:
    """Apply exponential time-decay to a recent_score over dt seconds."""
    if dt_seconds <= 0:
        return recent_score
    return recent_score * math.exp(-_LAMBDA * dt_seconds)


def bump(recent_score: float, dt_seconds: float, increment: float = 1.0) -> float:
    """Decay the old score to 'now', then add this window's increment.

    recent_score_new = recent_score_old * e^(-lambda*dt) + increment
    """
    return decay(recent_score, dt_seconds) + increment


def hybrid_score(count: int, recent_score: float) -> float:
    """Blend all-time popularity (log-scaled for the power-law) with recency."""
    return config.W_POP * math.log1p(count) + config.W_REC * recent_score


def score_for(mode: str, count: int, recent_score: float) -> float:
    """Single ranking key used by the trie/top-k for the chosen mode."""
    if mode == "count":
        return float(count)
    return hybrid_score(count, recent_score)
