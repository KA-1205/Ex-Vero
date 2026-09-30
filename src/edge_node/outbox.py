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
    """Create the Integer indexes the outbox filter/order rely on (idempotent)."""
    for field in ("_sync_meta.synced", "_sync_meta.syncable", "_sync_meta.client_sequence"):
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
