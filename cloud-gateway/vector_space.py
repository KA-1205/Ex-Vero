"""Single source of truth for the fleet's named vector space (P0-1).

The hub (`facts` collection, ingest slotting, snapshot build) and the shared
edge snapshot builder (`src/edge_node/snapshot.py`) must agree on *exactly* which
named dense vectors exist and how large each one is. If they disagree, two
things break silently:

  * `/facts` reads a point back from a slot it was never written to and hands the
    snapshot builder a ``None`` vector (``TypeError`` on ``len``);
  * `update_from_snapshot` rejects a shard whose named-vector config differs from
    the device's immutable shard.

So the slot -> dim map lives here, once. It mirrors the model adapter registry
the edge resolves from YAML (``config/*.yaml``): slot name == adapter name, dim ==
the adapter's model dim. The edge builds its immutable shard with these exact
named vectors, so the hub snapshot must match.
"""

from typing import Any, Dict, Optional

# Named dense vector slots: slot name -> dimension. Slot name equals the edge
# adapter name stamped on every point as ``payload["model"]`` (``text`` =
# BAAI/bge-small-en-v1.5, ``image`` = Qdrant/clip-ViT-B-32). Dims come from the
# model, not from a request body.
DENSE_SLOTS: Dict[str, int] = {
    "text": 384,
    "image": 512,
}

# BM25 sparse vector slot (built into Qdrant Edge; one per text point).
SPARSE_SLOT = "text_bm25"

# The slot used as the snapshot's primary dense name. The restore config always
# carries every slot in DENSE_SLOTS regardless of which points exist, so a
# text-only hub still produces a shard the device (which has an image slot) can
# restore.
DEFAULT_DENSE_SLOT = "text"

# Reverse lookup for the fallback path: a dense vector's length identifies its
# slot. The dims are distinct across slots, so length is unambiguous.
_DIM_TO_SLOT: Dict[int, str] = {dim: name for name, dim in DENSE_SLOTS.items()}


def slot_for_dim(dim: int) -> Optional[str]:
    """Resolve a dense slot name from a vector length, or ``None`` if unknown."""
    return _DIM_TO_SLOT.get(dim)


def slot_for_envelope(env: Dict[str, Any]) -> Optional[str]:
    """Resolve the dense slot a push envelope belongs in.

    Prefers the ``model`` stamp (which is the adapter/slot name on every edge
    point), then the vector length. Returns ``None`` when there is no dense
    vector or it cannot be placed, so the caller can decide to fail fast rather
    than mis-slot the point.
    """
    payload = env.get("payload") or {}
    model = payload.get("model")
    if isinstance(model, str) and model in DENSE_SLOTS:
        return model
    vec = env.get("vector")
    if vec is not None:
        return slot_for_dim(len(vec))
    return None


def extra_dense_slots(primary: str = DEFAULT_DENSE_SLOT) -> Dict[str, int]:
    """Every dense slot except ``primary`` — the ``extra_vectors`` the shared
    snapshot builder needs so its EdgeConfig carries all named vectors."""
    return {name: dim for name, dim in DENSE_SLOTS.items() if name != primary}
