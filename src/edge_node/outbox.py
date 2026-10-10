"""Outbox — a filtered *view* over the mutable shard, never a second store.

The pending queue is exactly `scroll(_sync_meta.synced == 0 AND
_sync_meta.syncable == 1)`, priority-ordered so URGENT facts drain before
ROUTINE ones (backend.md §6.1).

Two Phase-0 facts shape this module (AGENTS.md §3.1):
  * A Python-`bool` payload field matches *nothing* in `scroll`/`count`/
    `set_payload_by_filter` — a silent zero that would make the outbox drain
    zero points. So `synced`/`syncable` are stored as Integer 0/1 and
    Integer-indexed.
  * `scroll(order_by=...)` only works on a range-indexed key, so
    `client_sequence` is Integer-indexed too and used for stable ordering.
"""

from typing import List, Tuple, Dict, Any
import hashlib

from qdrant_edge import (
    EdgeShard,
    Point,
    UpdateOperation,
    FieldCondition,
    MatchValue,
    Filter,
    HasIdCondition,
    ScrollRequest,
    OrderBy,
    Direction,
    PayloadSchemaType,
)

# Nested payload keys (dotted) for the sync bookkeeping stored on every point.
SYNCED_KEY = "_sync_meta.synced"            # 0 = pending, 1 = acked by hub
SYNCABLE_KEY = "_sync_meta.syncable"        # 1 = verdict permits sync
SEQUENCE_KEY = "_sync_meta.client_sequence"  # per-device monotonic ordering

# A retraction travels through the SAME outbox as observations (A2 / P0-2), but
# the fact it withdraws is deleted locally the moment it is retracted, so the
# pending retraction cannot be a flag on that (now gone) point. It is a tiny
# vector-less marker point instead — invisible to search (no vectors) and to the
# observation outbox (no `_sync_meta`) — carrying its own persistent
# `client_sequence` so it pushes in order with observations.
RETRACT_SYNCED_KEY = "_retract.synced"      # 0 = pending, 1 = acked by hub
RETRACT_ENTRY_TYPE = "RETRACTED"

# Push order for the priority buckets; lower drains first.
_PRIORITY_ORDER = {"URGENT": 0, "ROUTINE": 1, "HELD": 2}


def add_sync_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure `_sync_meta.synced` exists as Integer 0 (pending), never a bool.

    Storing a Python `bool` here is the Phase-0 trap: the outbox filter would
    silently match nothing. Integer 0/1 filters correctly.
    """
    meta = payload.setdefault("_sync_meta", {})
    if "synced" not in meta:
        meta["synced"] = 0
    return payload


def ensure_indexes(shard: EdgeShard) -> None:
    """Create the Integer indexes the outbox filter/order rely on (idempotent).

    `client_timestamp_ns` is indexed too: the Phase-4 pull dedupes the mutable
    shard with a range filter on it, which needs a range (Integer) index.
    """
    for field in (
        "_sync_meta.synced",
        "_sync_meta.syncable",
        "_sync_meta.client_sequence",
        "client_timestamp_ns",
        RETRACT_SYNCED_KEY,
    ):
        try:
            shard.update(
                UpdateOperation.create_field_index(
                    field_name=field, schema=PayloadSchemaType.Integer
                )
            )
        except Exception:
            # Index already exists / benign on a populated shard.
            pass


def get_outbox(shard: EdgeShard, limit: int = 100) -> List[Tuple[int, Point]]:
    """Return pending, syncable points as (point_id, record), URGENT first.

    Ordered by `client_sequence` at the shard (a real range-indexed sort) and
    then bucketed by priority in Python so URGENT drains ahead of ROUTINE.
    """
    f_filter = Filter(
        must=[
            FieldCondition(key=SYNCED_KEY, match=MatchValue(value=0)),
            FieldCondition(key=SYNCABLE_KEY, match=MatchValue(value=1)),
        ]
    )
    req = ScrollRequest(
        limit=limit,
        filter=f_filter,
        with_payload=True,
        with_vector=True,  # push needs the vectors to build the envelope
        order_by=OrderBy(key=SEQUENCE_KEY, direction=Direction.Asc),
    )
    res = shard.scroll(req)
    records = res[0] if isinstance(res, tuple) else res

    def priority_of(rec) -> int:
        meta = (rec.payload or {}).get("_sync_meta", {})
        return _PRIORITY_ORDER.get(meta.get("sync_priority", "ROUTINE"), 1)

    def sequence_of(rec) -> int:
        meta = (rec.payload or {}).get("_sync_meta", {})
        return meta.get("client_sequence", 0)

    # Stable: (priority bucket, client_sequence). URGENT before ROUTINE, and
    # within a bucket the earliest-captured fact goes first.
    ordered = sorted(records, key=lambda r: (priority_of(r), sequence_of(r)))
    return [(r.id, r) for r in ordered]


def get_outbox_points(shard: EdgeShard, limit: int = 100) -> List[Point]:
    return [rec for _, rec in get_outbox(shard, limit)]


def mark_synced(shard: EdgeShard, point_ids: List[int]) -> None:
    """Flip `_sync_meta.synced` to 1 for the acked ids, merging (not replacing)
    the nested `_sync_meta` object so priority/sequence/verdict survive.

    Uses `key="_sync_meta"` so only the `synced` sub-field is written; passing a
    full `{"_sync_meta": {...}}` without a key would clobber the sibling fields.
    """
    if not point_ids:
        return
    op = UpdateOperation.set_payload_by_filter(
        filter=Filter(must=[HasIdCondition(point_ids=set(point_ids))]),
        payload={"synced": 1},
        key="_sync_meta",
    )
    shard.update(op)
    shard.optimize()


def retraction_marker_id(target_point_id: int) -> int:
    """Deterministic, collision-resistant id for a retraction marker so a repeated
    retract of the same point is idempotent (one marker, not many)."""
    raw = f"retract_marker:{target_point_id}".encode()
    return int.from_bytes(hashlib.md5(raw).digest()[:8], "little", signed=False)


def queue_retraction(
    shard: EdgeShard,
    target_point_id: int,
    corroboration_key: str,
    client_sequence: int,
) -> int:
    """Persist a pending retraction as a vector-less marker point in the mutable
    shard (the outbox is a view over this shard, never a second store). Returns
    the marker id. Idempotent on `target_point_id`."""
    marker_id = retraction_marker_id(target_point_id)
    marker = Point(
        id=marker_id,
        vector={},  # no vectors: invisible to dense/BM25 search
        payload={
            "_retract": {
                "entry_type": RETRACT_ENTRY_TYPE,
                "target_point_id": target_point_id,
                "corroboration_key": corroboration_key,
                "client_sequence": client_sequence,
                "synced": 0,
            }
        },
    )
    shard.update(UpdateOperation.upsert_points(points=[marker]))
    shard.optimize()
    return marker_id


def get_pending_retractions(shard: EdgeShard, limit: int = 100) -> List[Point]:
    """Pending (unsynced) retraction markers, ordered by their own
    `client_sequence` so they push in the same order they were made."""
    req = ScrollRequest(
        limit=limit,
        filter=Filter(must=[FieldCondition(key=RETRACT_SYNCED_KEY, match=MatchValue(value=0))]),
        with_payload=True,
        with_vector=False,
    )
    res = shard.scroll(req)
    records = res[0] if isinstance(res, tuple) else res

    def seq_of(rec) -> int:
        return (rec.payload or {}).get("_retract", {}).get("client_sequence", 0)

    return sorted(records, key=seq_of)


def mark_retraction_synced(shard: EdgeShard, marker_ids: List[int]) -> None:
    """Flip a drained retraction marker's `_retract.synced` to 1 (merging the
    nested object), so a re-push never re-sends it."""
    if not marker_ids:
        return
    op = UpdateOperation.set_payload_by_filter(
        filter=Filter(must=[HasIdCondition(point_ids=set(marker_ids))]),
        payload={"synced": 1},
        key="_retract",
    )
    shard.update(op)
    shard.optimize()


def delete_points(shard: EdgeShard, point_ids: List[int]) -> None:
    """Delete points (facts or retraction markers) by id. Used by retract (local
    withdrawal) and by pull cleanup of hub-tombstoned ids."""
    if not point_ids:
        return
    shard.update(
        UpdateOperation.delete_points_by_filter(
            filter=Filter(must=[HasIdCondition(point_ids=set(point_ids))])
        )
    )
    shard.optimize()

