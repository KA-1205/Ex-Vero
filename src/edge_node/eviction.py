"""Eviction — safe memory cap enforcement for the mutable shard.

Eviction only touches points that are already confirmed synced to the hub
(`_sync_meta.synced == 1`). Pending points (`synced == 0`) are NEVER evicted
— that would be silent data loss (invariant 1, AGENTS.md §5).

The policy is "oldest and least queried" among synced points. Since Qdrant Edge
does not natively track per-point query counts, we use `client_timestamp_ns`
as the primary ordering (oldest first) — a reasonable proxy for least-queried
in a disaster-response context where older facts are less relevant.

After eviction, `optimize()` is called to reclaim space (Qdrant Edge has no
background optimizer — AGENTS.md §4 trap).
"""

from typing import List, Optional
from qdrant_edge import (
    EdgeShard,
    UpdateOperation,
    Filter,
    FieldCondition,
    MatchValue,
    ScrollRequest,
    OrderBy,
    Direction,
    CountRequest,
    HasIdCondition,
)
from .outbox import SYNCED_KEY


def get_synced_count(shard: EdgeShard) -> int:
    """Count points with _sync_meta.synced == 1."""
    f = Filter(must=[FieldCondition(key=SYNCED_KEY, match=MatchValue(value=1))])
    res = shard.count(CountRequest(filter=f))
    return getattr(res, "count", res)


def get_total_count(shard: EdgeShard) -> int:
    """Total points in the shard."""
    res = shard.count(CountRequest())
    return getattr(res, "count", res)


def evict_if_over_cap(
    shard: EdgeShard,
    max_local_points: int,
    min_evict: int = 1,
) -> int:
    """Evict oldest synced points if over the cap.

    Args:
        shard: The mutable shard to evict from.
        max_local_points: Maximum allowed points in the shard.
        min_evict: Minimum number of points to evict when over cap (default 1).

    Returns:
        Number of points evicted (0 if not over cap or nothing to evict).
    """
    total = get_total_count(shard)
    if total <= max_local_points:
        return 0

    # How many to evict to get under cap
    to_evict = total - max_local_points
    if to_evict < min_evict:
        to_evict = min_evict

    # Scroll synced points ordered by client_timestamp_ns ASC (oldest first)
    f = Filter(must=[FieldCondition(key=SYNCED_KEY, match=MatchValue(value=1))])
    req = ScrollRequest(
        limit=to_evict,
        filter=f,
        with_payload=True,
        with_vector=False,
        order_by=OrderBy(key="client_timestamp_ns", direction=Direction.Asc),
    )
    res = shard.scroll(req)
    records = res[0] if isinstance(res, tuple) else res

    if not records:
        return 0

    # Extract point IDs to evict
    point_ids = [r.id for r in records]

    # Delete the evicted points
    shard.update(UpdateOperation.delete_points_by_filter(
        filter=Filter(must=[HasIdCondition(point_ids=set(point_ids))])
    ))

    # Reclaim space
    shard.optimize()

    return len(point_ids)


def evict_by_count_and_optimize(
    shard: EdgeShard,
    max_local_points: int,
) -> int:
    """Main entry point: evict oldest synced points if over cap, then optimize.

    This is called after write operations (capture, pull) to enforce the cap.

    Returns:
        Number of points evicted.
    """
    return evict_if_over_cap(shard, max_local_points)