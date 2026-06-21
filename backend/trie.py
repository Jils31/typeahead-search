"""In-memory trie for prefix top-k (O(prefix length) lookup).

Precomputes a candidate pool only for short prefixes (<= PRECOMPUTE_PREFIX_LEN,
the few broad/hot ones); longer prefixes are computed on demand. Counts/recency
are read live, so the same structure serves both count and hybrid modes.
See DESIGN.md §13."""
from typing import Dict, List, Optional, Tuple

from . import config, ranking

_MAX_QUERY_LEN = 100          # truncate pathological queries (bounds trie depth)
_CANDIDATE_POOL = 50          # candidates kept per node (>= TOP_K, room for re-rank)
_DFS_CAP = 3000               # max words scanned for a non-precomputed prefix


class _Node:
    __slots__ = ("children", "is_word", "pool")

    def __init__(self):
        self.children: Dict[str, "_Node"] = {}
        self.is_word: bool = False
        self.pool: Optional[List[str]] = None  # precomputed membership (shallow only)


class Trie:
    def __init__(self):
        self.root = _Node()
        # query -> [count, recent_score]  (mutable, live values)
        self.words: Dict[str, List[float]] = {}

    # ---------- build ----------
    def _insert_structure(self, query: str) -> None:
        node = self.root
        for ch in query:
            nxt = node.children.get(ch)
            if nxt is None:
                nxt = _Node()
                node.children[ch] = nxt
            node = nxt
        node.is_word = True

    def build(self, rows: List[Tuple[str, int, float, float]]) -> None:
        """rows = (query, count, recent_score, age_seconds). Decays recent to now."""
        self.root = _Node()
        self.words = {}
        for query, count, recent, age in rows:
            q = query[:_MAX_QUERY_LEN]
            if not q:
                continue
            recent_now = ranking.decay(recent, age)
            self.words[q] = [float(count), recent_now]
            self._insert_structure(q)
        self.refresh_pools()

    # ---------- candidate collection ----------
    def _navigate(self, prefix: str) -> Optional[_Node]:
        node = self.root
        for ch in prefix:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    def _collect(self, node: _Node, prefix: str, cap: int) -> List[str]:
        """DFS-collect up to `cap` complete query strings under `node`."""
        out: List[str] = []
        stack: List[Tuple[_Node, str]] = [(node, prefix)]
        while stack and len(out) < cap:
            cur, pre = stack.pop()
            if cur.is_word and pre in self.words:
                out.append(pre)
            for ch, child in cur.children.items():
                stack.append((child, pre + ch))
        return out

    def refresh_pools(self) -> None:
        """(Re)compute candidate membership for all shallow nodes.

        Walks every prefix up to PRECOMPUTE_PREFIX_LEN, collects its subtree
        words, keeps the top _CANDIDATE_POOL by current count as the membership.
        """
        self._refresh(self.root, "", 0)

    def _refresh(self, node: _Node, prefix: str, depth: int) -> None:
        if depth <= config.PRECOMPUTE_PREFIX_LEN:
            words = self._collect(node, prefix, cap=10_000)
            words.sort(key=lambda q: self.words[q][0], reverse=True)
            node.pool = words[:_CANDIDATE_POOL]
            for ch, child in node.children.items():
                self._refresh(child, prefix + ch, depth + 1)
        # deeper nodes: no precomputed pool (computed on demand)

    # ---------- query ----------
    def get_suggestions(self, prefix: str, k: int, mode: str) -> List[Tuple[str, int]]:
        prefix = prefix.lower().strip()
        if not prefix:
            return []
        node = self._navigate(prefix)
        if node is None:
            return []
        if node.pool is not None:
            candidates = node.pool
        else:
            candidates = self._collect(node, prefix, cap=_DFS_CAP)
        ranked = sorted(
            candidates,
            key=lambda q: ranking.score_for(mode, int(self.words[q][0]), self.words[q][1]),
            reverse=True,
        )
        return [(q, int(self.words[q][0])) for q in ranked[:k]]

    # ---------- live updates (called after each flush) ----------
    def apply_updates(self, window: Dict[str, int]) -> None:
        """Apply a flush window: counts are exact-additive; recent gets a rough
        bump (full decay correction happens on the periodic rebuild)."""
        for q_raw, inc in window.items():
            q = q_raw[:_MAX_QUERY_LEN]
            if not q:
                continue
            if q in self.words:
                self.words[q][0] += inc
                self.words[q][1] += inc
            else:
                self.words[q] = [float(inc), float(inc)]
                self._insert_structure(q)

    def size(self) -> int:
        return len(self.words)
