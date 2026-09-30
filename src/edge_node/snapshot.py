"""Partial-snapshot materialization for the pull direction (Phase 4).

The hub side of a pull turns the facts it holds into a **real Qdrant Edge
snapshot** that the device applies with `EdgeShard.update_from_snapshot(path)`
(the signature verified in Phase 0, AGENTS.md §3.1). This is the mechanism that
lets a device learn a fact it never captured: A → hub → snapshot → B.

Why build a real Edge shard here instead of shipping a list of points?
`update_from_snapshot` is the *only* sanctioned way to populate the immutable
shard (AGENTS.md §6.3 / Phase 4 guardrail: "never write the immutable shard
locally except via snapshot restore"). Re-upserting envelopes on the device
would be a local write and would defeat the honesty proof. So the hub packs a
genuine shard snapshot and the device restores it.

A Qdrant Edge snapshot is a tar of the shard directory (`segments/`, `wal/`,
`edge_config.json`). `update_from_snapshot` merges segments whose version is
newer than what the destination already holds — so we write **and optimize** the
staging shard before packing, which bumps the segment version above the empty
destination's and makes the restore actually take (a version-0 segment is
silently skipped, verified while building this phase).

The gateway (`cloud-gateway/`) uses `build_snapshot_tar` to answer the edge's
partial-snapshot request; the Phase-4 unit tests reuse it through the stub
transport so the acceptance test exercises the real restore path with no
network.
"""

import os
import tarfile
import tempfile
from typing import Any, Dict, List, Optional

from qdrant_edge import (
    Bm25,
    Bm25Config,
    Distance,
    EdgeConfig,
    EdgeShard,
    EdgeSparseVectorParams,
    EdgeVectorParams,
    Modifier,
    Point,
    SparseVector,
    UpdateOperation,
)

SPARSE_FIELD = "text_bm25"


def build_edge_config(dense_name: str, dim: int, extra_vectors: Optional[Dict[str, int]] = None) -> EdgeConfig:
    """The immutable-shard config a snapshot must match: named vectors
    plus the BM25 sparse field. Must mirror the config `main.py` builds for the
    device, or `update_from_snapshot` would reject a shape mismatch.
    """
    vectors = {dense_name: EdgeVectorParams(size=dim, distance=Distance.Cosine)}
    if extra_vectors:
        for name, size in extra_vectors.items():
            vectors[name] = EdgeVectorParams(size=size, distance=Distance.Cosine)

    return EdgeConfig(
        vectors=vectors,
        sparse_vectors={SPARSE_FIELD: EdgeSparseVectorParams(modifier=Modifier.Idf)},
    )


def _point_from_envelope(
    env: Dict[str, Any],
    dense_name: str,
    all_vector_dims: Optional[Dict[str, int]] = None,
) -> Point:
    """Reconstruct an Edge point from a hub envelope.

    A BM25 sparse vector cannot be re-derived from the payload without the
    original document tokenization, so the envelope carries it. If the hub is
    missing the sparse leg we recompute it from the text as a fallback rather
    than shipping a point that can never be found by the BM25 leg.
    """
    vector: Dict[str, Any] = {}
    if env.get("vectors") and isinstance(env["vectors"], dict):
        vector.update(env["vectors"])
    elif env.get("vector") is not None:
        vector[dense_name] = env["vector"]

    if all_vector_dims:
        for vname, vsize in all_vector_dims.items():
            if vname not in vector:
                vector[vname] = [0.0] * vsize

    sparse = env.get("sparse")
    if sparse is not None:
        vector[SPARSE_FIELD] = SparseVector(
            indices=sparse["indices"], values=sparse["values"]
        )
    else:
        text = (env.get("payload") or {}).get("value")
        if text:
            vector[SPARSE_FIELD] = Bm25(Bm25Config()).embed_document(text)
    return Point(id=env["id"], vector=vector, payload=env.get("payload"))


def build_snapshot_tar(
    envelopes: List[Dict[str, Any]],
    dense_name: str,
    dim: int,
    work_dir: Optional[str] = None,
    extra_vectors: Optional[Dict[str, int]] = None,
) -> Optional[str]:
    """Pack the given hub facts into a real Edge snapshot tar and return its path.

    Returns ``None`` when there is nothing to ship, so the caller can treat an
    empty hub as a no-op pull. The tar is created under ``work_dir`` (a caller
    controlled temp dir) and the caller owns cleaning it up.
    """
    if not envelopes:
        return None

    if extra_vectors is None:
        try:
            import edge_node.main as _main
            if _main.adapters:
                extra_vectors = {a.name: a.dim for a in _main.adapters}
            elif _main.edge_config and hasattr(_main.edge_config, "vectors"):
                extra_vectors = {k: v.size for k, v in _main.edge_config.vectors.items()}
        except Exception:
            pass

    work_dir = work_dir or tempfile.mkdtemp(prefix="edge_snapshot_")
    shard_dir = os.path.join(work_dir, "hub_shard")
    os.makedirs(shard_dir, exist_ok=True)

    config = build_edge_config(dense_name, dim, extra_vectors)
    shard = EdgeShard.create(shard_dir, config)
    try:
        all_dims = {dense_name: dim}
        if extra_vectors:
            all_dims.update(extra_vectors)
        points = [_point_from_envelope(env, dense_name, all_dims) for env in envelopes]
        shard.update(UpdateOperation.upsert_points(points=points))
        # Optimize bumps the segment version above the destination's empty
        # segment, so update_from_snapshot actually applies it (a version-0
        # segment is silently skipped).
        shard.optimize()
        shard.flush()
    finally:
        shard.close()

    tar_path = os.path.join(work_dir, "snapshot.tar")
    with tarfile.open(tar_path, "w") as tar:
        for name in os.listdir(shard_dir):
            tar.add(os.path.join(shard_dir, name), arcname=name)
    return tar_path
