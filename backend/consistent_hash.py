"""Consistent-hash ring (with virtual nodes) mapping a prefix key to a cache
node. Adding/removing a node remaps only ~K/N keys, unlike `hash % N` which
remaps almost everything. See DESIGN.md §4."""
import bisect
import hashlib
from typing import Dict, List, Optional


def _hash(key: str) -> int:
    """Stable 64-bit hash from md5 (deterministic across processes/restarts)."""
    return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:16], 16)


class ConsistentHashRing:
    def __init__(self, nodes: Optional[List[str]] = None, vnodes: int = 150):
        self.vnodes = vnodes
        self._ring: Dict[int, str] = {}      # ring position -> node id
        self._sorted_keys: List[int] = []    # sorted ring positions (for bisect)
        self._nodes: set[str] = set()
        for n in nodes or []:
            self.add_node(n)

    def _vnode_key(self, node: str, i: int) -> str:
        return f"{node}#{i}"

    def add_node(self, node: str) -> None:
        if node in self._nodes:
            return
        self._nodes.add(node)
        for i in range(self.vnodes):
            pos = _hash(self._vnode_key(node, i))
            self._ring[pos] = node
            bisect.insort(self._sorted_keys, pos)

    def remove_node(self, node: str) -> None:
        if node not in self._nodes:
            return
        self._nodes.discard(node)
        for i in range(self.vnodes):
            pos = _hash(self._vnode_key(node, i))
            if pos in self._ring:
                del self._ring[pos]
                idx = bisect.bisect_left(self._sorted_keys, pos)
                if idx < len(self._sorted_keys) and self._sorted_keys[idx] == pos:
                    self._sorted_keys.pop(idx)

    def get_node(self, key: str) -> Optional[str]:
        """Walk clockwise from the key's hash to the first vnode -> its node."""
        if not self._sorted_keys:
            return None
        h = _hash(key)
        idx = bisect.bisect(self._sorted_keys, h)
        if idx == len(self._sorted_keys):  # wrap around the ring
            idx = 0
        return self._ring[self._sorted_keys[idx]]

    def debug(self, key: str) -> dict:
        """Detail used by GET /cache/debug to prove routing behavior."""
        h = _hash(key)
        idx = bisect.bisect(self._sorted_keys, h)
        wrapped = idx == len(self._sorted_keys)
        ring_pos = self._sorted_keys[0 if wrapped else idx] if self._sorted_keys else None
        return {
            "key": key,
            "key_hash": h,
            "owner_node": self.get_node(key),
            "ring_position": ring_pos,
            "wrapped_around": wrapped,
            "total_vnodes": len(self._sorted_keys),
        }

    def distribution(self, keys: List[str]) -> Dict[str, int]:
        """Count how many of `keys` land on each node (balance evidence)."""
        out: Dict[str, int] = {n: 0 for n in self._nodes}
        for k in keys:
            node = self.get_node(k)
            if node is not None:
                out[node] = out.get(node, 0) + 1
        return out
