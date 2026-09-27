"""
Consensus module for Step 6 & 7.

Event-sourced trust resolution:
- fact_events is an append-only log of OBSERVED / RETRACTED events.
- The fold is a PURE function of the log: same events -> same trust,
  regardless of insertion order (Invariant 6).
- Retraction beats re-observation via recency-weighted decay (Invariant 7).
- Order by a hub-assigned sequence number, NOT device wall-clocks (Invariant 5).
"""

import math
import hashlib
from typing import List, Dict, Any, Optional
from qdrant_edge import (
    EdgeShard,
    EdgeConfig,
    EdgeVectorParams,
    Distance,
    Point,
    UpdateOperation,
    FieldCondition,
    MatchValue,
    Filter,
    ScrollRequest,
)

OBSERVED = "OBSERVED"
RETRACTED = "RETRACTED"


class EventLog:
    """Append-only event log backed by an Edge shard.

    We store one point per event. The event shard needs at least one vector
    field (EdgeConfig rejects empty vectors+sparse_vectors), so we use a tiny
    1-dim dummy dense vector. Events are never queried by vector similarity,
    only scrolled by payload filter.
    """

    def __init__(self, shard: EdgeShard):
        self.shard = shard
        # Monotonic hub-assigned sequence. In a real deployment this comes from
        # the hub; here the gateway (this process) is the single sequencer.
        self._seq = 0

    @staticmethod
    def build_config() -> EdgeConfig:
        return EdgeConfig(
            vectors={"_dummy": EdgeVectorParams(size=1, distance=Distance.Cosine)},
            max_search_threads=1,
            search_pool_core=0,
        )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _event_id(point_id: int, event_type: str, seq: int) -> int:
        raw = f"{point_id}:{event_type}:{seq}"
        return int.from_bytes(hashlib.md5(raw.encode()).digest()[:8], "little", signed=False)

    def append(self, point_id: int, event_type: str, device_ts: str, seq: Optional[int] = None) -> Dict[str, Any]:
        """Append an event. seq is hub-assigned ordering; device_ts is metadata only."""
        if seq is None:
            seq = self._next_seq()
        else:
            # keep our counter ahead of externally supplied seqs
            self._seq = max(self._seq, seq)
        payload = {
            "point_id": point_id,
            "event_type": event_type,
            "seq": seq,           # authoritative ordering
            "device_ts": device_ts,  # metadata only, never trusted for ordering
        }
        ev = Point(id=self._event_id(point_id, event_type, seq), vector={"_dummy": [0.0]}, payload=payload)
        self.shard.update(UpdateOperation.upsert_points(points=[ev]))
        self.shard.optimize()
        return payload

    def events_for(self, point_id: int) -> List[Dict[str, Any]]:
        cond = FieldCondition(key="point_id", match=MatchValue(value=point_id))
        f = Filter(must=[cond])
        req = ScrollRequest(limit=10000, filter=f, with_payload=True, with_vector=False)
        res = self.shard.scroll(req)
        recs = res[0] if isinstance(res, tuple) else res
        return [r.payload for r in recs]

    def all_events(self) -> List[Dict[str, Any]]:
        req = ScrollRequest(limit=100000, with_payload=True, with_vector=False)
        res = self.shard.scroll(req)
        recs = res[0] if isinstance(res, tuple) else res
        return [r.payload for r in recs]


def fold_trust(events: List[Dict[str, Any]], decay: float = 0.0) -> float:
    """Pure fold over the event log for a single fact.

    Ordered by hub 'seq' (not wall-clock). Returns trust in [0, 1].

    - Each OBSERVED contributes +1, each RETRACTED contributes -1.
    - A recency weight is applied by seq distance from the newest event so a
      RETRACTED that arrives after an OBSERVED dominates (retraction beats
      re-observation). With decay=0 this reduces to a simple signed count that
      still lets the latest event dominate via the final-state override below.
    - The fold is deterministic in the *set* of events: we sort by seq inside,
      so insertion order does not matter (Invariant 6).
    """
    if not events:
        return 0.0

    ordered = sorted(events, key=lambda e: e["seq"])
    newest_seq = ordered[-1]["seq"]

    score = 0.0
    for e in ordered:
        val = 1.0 if e["event_type"] == OBSERVED else -1.0
        # recency weight: newer events (closer to newest_seq) weigh more
        w = math.exp(-decay * (newest_seq - e["seq"]))
        score += w * val

    # Final-state override: a retraction as the newest event forces trust to 0.
    if ordered[-1]["event_type"] == RETRACTED:
        return 0.0

    # Map signed score to [0, 1]. Saturating.
    # A single OBSERVED -> ~0.73; multiple corroborations climb toward 1.0.
    return 1.0 / (1.0 + math.exp(-score))
