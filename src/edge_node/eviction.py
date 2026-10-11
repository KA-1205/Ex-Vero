"""Eviction — safe memory cap enforcement for the mutable shard.

Only data whose loss is safe is ever eligible:

  * points already confirmed synced to the hub (`_sync_meta.synced == 1`), and
  * local-only points (`_sync_meta.syncable == 0`, e.g. HELD verdicts) that were
    never going to reach the hub.

Pending points (`synced == 0 AND syncable == 1`) are NEVER evicted — that would
be silent data loss (invariant 1, AGENTS.md §5).

Ordering is by `_sync_meta.client_sequence` ascending: a per-device local
monotonic counter. Wall-clock (`client_timestamp_ns`) is deliberately NOT used
as the sort key — it is not monotonic under clock skew, so it cannot give a
deterministic, reproducible eviction order (C1 / P2-2).

If, after evicting every eligible point, the shard is still over the cap (only
pending-syncable data remains), the cap cannot be honoured without data loss, so
`memory_status` reports a visible "memory_full" status instead of silently
dropping protected data.

Thumbnails are removed for evicted vision points through the `on_evict` callback:
the caller owns the thumbnail directory path, so this module neither guesses it
nor hardcodes it (C1).

After eviction, `optimize()` is called to reclaim space (Qdrant Edge has no
background optimizer — AGENTS.md §4 trap).
"""

from typing import Callable, Dict, Optional

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
from .outbox import SYNCED_KEY, SYNCABLE_KEY, SEQUENCE_KEY

# Visible memory status labels (C1). Not tunables — enum-like values compared by
# callers/tests, so they get names rather than bare literals.
MEMORY_STATUS_OK = "ok"
MEMORY_STATUS_FULL = "memory_full"


def _eligible_filter() -> Filter:
    """Filter matching exactly the safely-evictable points.

    A `should` (OR) of `synced == 1` and `syncable == 0`: a point that is either
    already at the hub, or local-only. A pending syncable point matches neither
    and is therefore never selected.
    """
    return Filter(
        should=[
            FieldCondition(key=SYNCED_KEY, match=MatchValue(value=1)),
            FieldCondition(key=SYNCABLE_KEY, match=MatchValue(value=0)),
        ]
    )


def get_synced_count(shard: EdgeShard) -> int:
    """Count points with _sync_meta.synced == 1."""
    f = Filter(must=[FieldCondition(key=SYNCED_KEY, match=MatchValue(value=1))])
    res = shard.count(CountRequest(filter=f))
    return getattr(res, "count", res)


def get_total_count(shard: EdgeShard) -> int:
    """Total points in the shard."""
    res = shard.count(CountRequest())
    return getattr(res, "count", res)


def memory_status(shard: EdgeShard, max_local_points: int) -> Dict[str, object]:
    """Visible cap status for a shard (C1).

    `over_cap` is True when the shard still exceeds the cap after the eviction
    that always precedes this call — i.e. only non-evictable (pending syncable)
    data remains, so the cap is full of data that must not be dropped.
    """
    used = get_total_count(shard)
    over_cap = used > max_local_points
    return {
        "used": used,
        "cap": max_local_points,
        "over_cap": over_cap,
        "status": MEMORY_STATUS_FULL if over_cap else MEMORY_STATUS_OK,
    }


def evict_if_over_cap(
    shard: EdgeShard,
    max_local_points: int,
    min_evict: int = 1,
    on_evict: Optional[Callable[[Dict[str, object]], None]] = None,
) -> int:
    """Evict eligible points (oldest local sequence first) if over the cap.

    Args:
        shard: The mutable shard to evict from.
        max_local_points: Maximum allowed points in the shard.
        min_evict: Minimum number of points to evict when over cap (default 1).
        on_evict: Optional callback invoked with each evicted point's payload
            before deletion (used by the caller to remove a thumbnail file).

    Returns:
        Number of points evicted (0 if not over cap or nothing eligible).
    """
    total = get_total_count(shard)
    if total <= max_local_points:
        return 0

    # How many to evict to get under cap
    to_evict = total - max_local_points
    if to_evict < min_evict:
        to_evict = min_evict

    # Scroll eligible points ordered by the local monotonic counter ASC.
    req = ScrollRequest(
        limit=to_evict,
        filter=_eligible_filter(),
        with_payload=True,  # on_evict needs the payload (e.g. thumbnail_url)
        with_vector=False,
        order_by=OrderBy(key=SEQUENCE_KEY, direction=Direction.Asc),
    )
    res = shard.scroll(req)
    records = res[0] if isinstance(res, tuple) else res

    if not records:
        # Everything left is protected (pending syncable): cap cannot be met.
        return 0

    if on_evict is not None:
        for rec in records:
            try:
                on_evict(rec.payload or {})
            except Exception as exc:  # noqa: BLE001 — cleanup must not block eviction
                print(f"eviction on_evict callback failed: {exc}")

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
    on_evict: Optional[Callable[[Dict[str, object]], None]] = None,
) -> int:
    """Main entry point: evict oldest eligible points if over cap, then optimize.

    This is called after write operations (capture, pull) to enforce the cap.

    Returns:
        Number of points evicted.
    """
    return evict_if_over_cap(shard, max_local_points, on_evict=on_evict)
