"""
Semantic conflict detection for Step 8 (backend.md §4.5).

corroboration_key only catches a conflict when two devices pick the SAME key.
Real devices describe the same hazard with different keys and wording, so the
exact-key fold misses genuine disagreements silently. This module supplements
it with a semantic path that reuses the hybrid query as the detector:

1. On ingest of an OBSERVED capture, hybrid-query the local store for reports
   above a similarity threshold.
2. A hard zone/entity guard prevents "Zone C gas leak" from merging with
   "Zone D gas leak".
3. Candidates that clear the threshold but carry a DIFFERENT corroboration_key
   are surfaced as POSSIBLE_CONFLICT. Never auto-merge: flag for review.
"""

from typing import List, Dict, Any, Optional
from qdrant_edge import (
    EdgeShard,
    Query,
    QueryRequest as EdgeQueryRequest,
    Prefetch,
    Fusion,
)

POSSIBLE_CONFLICT = "POSSIBLE_CONFLICT"

# In-memory conflict register: device_id -> list of conflict records.
_conflicts: Dict[str, List[Dict[str, Any]]] = {}


def _same_zone(a: Optional[str], b: Optional[str]) -> bool:
    """Hard guard: only compare reports about the same zone.

    If either side has no zone we do NOT treat them as the same zone, to avoid
    merging under-specified reports (conservative)."""
    if a is None or b is None:
        return False
    return str(a).strip().lower() == str(b).strip().lower()


def detect_conflicts(
    shard: EdgeShard,
    dense_vector: List[float],
    sparse_vector,
    dense_name: str,
    new_point_id: int,
    new_payload: Dict[str, Any],
    similarity_threshold: float,
    zone_field: str = "zone",
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Hybrid-query the shard for semantically-similar prior reports and return
    POSSIBLE_CONFLICT candidates.

    A candidate is flagged when ALL hold:
      - it is not the new point itself
      - same zone (hard guard)
      - similarity/fused score >= threshold
      - a DIFFERENT corroboration_key (i.e. the exact-key path would miss it)
    """
    dense_prefetch = Prefetch(limit=limit, query=Query.Nearest(query=dense_vector, using=dense_name))
    sparse_prefetch = Prefetch(limit=limit, query=Query.Nearest(query=sparse_vector, using="text_bm25"))
    req = EdgeQueryRequest(
        limit=limit,
        prefetches=[dense_prefetch, sparse_prefetch],
        query=Fusion.Rrf(k=60),
        with_payload=True,
        with_vector=False,
    )
    try:
        results = shard.query(req)
    except Exception:
        return []

    new_key = new_payload.get("corroboration_key")
    new_zone = new_payload.get(zone_field)

    candidates: List[Dict[str, Any]] = []
    for res in results:
        if res.id == new_point_id:
            continue
        payload = res.payload or {}
        if not _same_zone(new_zone, payload.get(zone_field)):
            continue
        if res.score < similarity_threshold:
            continue
        other_key = payload.get("corroboration_key")
        # A genuine miss of the exact-key scheme: different key, same real fact.
        if other_key == new_key:
            continue
        candidates.append({
            "status": POSSIBLE_CONFLICT,
            "new_point_id": new_point_id,
            "existing_point_id": res.id,
            "score": float(res.score),
            "zone": new_zone,
            "new_value": new_payload.get("value"),
            "existing_value": payload.get("value"),
            "new_key": new_key,
            "existing_key": other_key,
        })
    return candidates


def register_conflicts(device_id: str, conflicts: List[Dict[str, Any]]):
    if not conflicts:
        return
    _conflicts.setdefault(device_id, []).extend(conflicts)


def get_conflicts(device_id: str) -> List[Dict[str, Any]]:
    return list(_conflicts.get(device_id, []))


def clear_conflicts(device_id: Optional[str] = None):
    if device_id is None:
        _conflicts.clear()
    else:
        _conflicts.pop(device_id, None)
