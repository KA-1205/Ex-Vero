"""Aegis Edge — Cloud Gateway.

The hub side of the sync protocol (backend.md §6). One real FastAPI process in
front of a real Qdrant Server. **Qdrant is the only datastore here — no SQL, no
second store.** Two collections:

  * `facts`        — the upserted points (idempotent by the edge's deterministic
                     point id), the trusted-picture source for later phases.
  * `fact_events`  — the append-only event log. Every ingest appends one
                     OBSERVED event with a **hub-assigned `seq`** (this process is
                     the single sequencer). Phase 5's consensus fold reads this.

Endpoints the edge's `GatewayTransport` calls:
  GET  /health                — liveness
  GET  /delta/{device_id}     — {"max_client_sequence": int}  (-1 if none held)
  POST /ingest                — {device_id, envelopes[]} -> {acked_ids, count}
  GET  /facts/{device_id}     — {"envelopes": [...]}  (Phase 3 pull source)
  POST /snapshot/{device_id}  — a real Edge partial snapshot tar (Phase 4 pull)

Phase 3 scope: the gateway just stores points + events. Phase 4 adds the
partial-snapshot endpoint (learn from the fleet); the consensus fold (Phase 5)
builds on the event log, unchanged here.
"""

import hashlib
import os
import shutil
import tarfile
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
from qdrant_client import QdrantClient, models

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
FACTS_COLLECTION = "facts"
EVENTS_COLLECTION = "fact_events"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "text_bm25"

app = FastAPI(title="Aegis Edge — Cloud Gateway")

# The gateway is the single sequencer for hub-assigned event ordering. Guarded by
# a lock so concurrent ingests get distinct, monotonic seqs.
_seq_lock = threading.Lock()
_seq = 0

client: Optional[QdrantClient] = None


class IngestRequest(BaseModel):
    device_id: str
    envelopes: List[Dict[str, Any]]


def _event_id(device_id: str, point_id: int, client_sequence: int) -> str:
    """Deterministic event id so a re-pushed (device, point, seq) is idempotent."""
    raw = f"{device_id}:{point_id}:{client_sequence}".encode()
    return hashlib.md5(raw).hexdigest()


def _ensure_events_collection() -> None:
    if not client.collection_exists(EVENTS_COLLECTION):
        # Events are never searched by similarity; a 1-dim dummy vector satisfies
        # Qdrant's requirement that a collection has a vector config.
        client.create_collection(
            collection_name=EVENTS_COLLECTION,
            vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE),
        )
        client.create_payload_index(
            EVENTS_COLLECTION, "device_id", models.PayloadSchemaType.KEYWORD
        )
        client.create_payload_index(
            EVENTS_COLLECTION, "client_sequence", models.PayloadSchemaType.INTEGER
        )


def _ensure_facts_collection(dense_size: int) -> None:
    if not client.collection_exists(FACTS_COLLECTION):
        client.create_collection(
            collection_name=FACTS_COLLECTION,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(
                    size=dense_size, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams()},
        )
        client.create_payload_index(
            FACTS_COLLECTION, "reporter_device_id", models.PayloadSchemaType.KEYWORD
        )


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def _init_sequencer() -> None:
    """Resume the seq counter from the highest seq already in fact_events, so a
    gateway restart never re-issues an old sequence number."""
    global _seq
    highest = 0
    offset = None
    while True:
        points, offset = client.scroll(
            EVENTS_COLLECTION, limit=1000, offset=offset, with_payload=True, with_vectors=False
        )
        for p in points:
            highest = max(highest, int(p.payload.get("seq", 0)))
        if offset is None:
            break
    with _seq_lock:
        _seq = highest


@app.on_event("startup")
def _startup() -> None:
    global client
    # A generous timeout + a short retry loop: on a cold compose start Qdrant may
    # still be settling even after its healthcheck flips, and the first
    # collection create is the slowest call.
    client = QdrantClient(url=QDRANT_URL, timeout=30.0)
    last_err: Optional[Exception] = None
    for _ in range(30):
        try:
            _ensure_events_collection()
            _init_sequencer()
            return
        except Exception as e:  # transport/timeout while Qdrant warms up
            last_err = e
            time.sleep(2.0)
    raise RuntimeError(f"gateway could not reach Qdrant at {QDRANT_URL}: {last_err}")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "qdrant_url": QDRANT_URL}


@app.get("/delta/{device_id}")
def delta(device_id: str) -> Dict[str, int]:
    """Highest client_sequence the hub already holds for this device.

    Returns -1 when the hub holds nothing, so the edge pushes everything with
    client_sequence > -1 (i.e. all of it) on the first sync.
    """
    highest = -1
    offset = None
    flt = models.Filter(
        must=[models.FieldCondition(key="device_id", match=models.MatchValue(value=device_id))]
    )
    while True:
        points, offset = client.scroll(
            EVENTS_COLLECTION, scroll_filter=flt, limit=1000, offset=offset,
            with_payload=True, with_vectors=False,
        )
        for p in points:
            highest = max(highest, int(p.payload.get("client_sequence", -1)))
        if offset is None:
            break
    return {"max_client_sequence": highest}


@app.post("/ingest")
def ingest(req: IngestRequest) -> Dict[str, Any]:
    """Upsert the delta batch into `facts` and append OBSERVED events.

    Idempotent: fact points key on the edge's deterministic id and events key on
    (device, point, client_sequence), so a crash-retry re-push writes no
    duplicates.
    """
    if not req.envelopes:
        return {"acked_ids": [], "count": 0}

    dense_size = len(req.envelopes[0]["vector"])
    _ensure_facts_collection(dense_size)

    fact_points: List[models.PointStruct] = []
    event_points: List[models.PointStruct] = []
    acked: List[int] = []

    for env in req.envelopes:
        vectors: Dict[str, Any] = {DENSE_VECTOR: env["vector"]}
        if env.get("sparse"):
            vectors[SPARSE_VECTOR] = models.SparseVector(
                indices=env["sparse"]["indices"], values=env["sparse"]["values"]
            )
        fact_points.append(
            models.PointStruct(id=env["id"], vector=vectors, payload=env["payload"])
        )

        seq = _next_seq()  # hub-assigned ordering; device wall-clock stays metadata
        event_id = _event_id(req.device_id, env["id"], env["client_sequence"])
        event_points.append(
            models.PointStruct(
                id=event_id,
                vector=[0.0],
                payload={
                    "device_id": req.device_id,
                    "point_id": env["id"],
                    "event_type": "OBSERVED",
                    "seq": seq,
                    "client_sequence": env["client_sequence"],
                },
            )
        )
        acked.append(env["id"])

    client.upsert(FACTS_COLLECTION, points=fact_points)
    client.upsert(EVENTS_COLLECTION, points=event_points)
    return {"acked_ids": acked, "count": len(acked)}


@app.get("/facts/{device_id}")
def facts(device_id: str) -> Dict[str, Any]:
    """Return the hub's facts for a device as pull envelopes.

    Phase 3 uses this for a simple whole-collection pull; Phase 4 replaces it
    with real partial snapshots.
    """
    envelopes: List[Dict[str, Any]] = []
    if not client.collection_exists(FACTS_COLLECTION):
        return {"envelopes": envelopes}

    flt = models.Filter(
        must=[
            models.FieldCondition(
                key="reporter_device_id", match=models.MatchValue(value=device_id)
            )
        ]
    )
    offset = None
    while True:
        points, offset = client.scroll(
            FACTS_COLLECTION, scroll_filter=flt, limit=1000, offset=offset,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            vecs = p.vector or {}
            dense = vecs.get(DENSE_VECTOR) if isinstance(vecs, dict) else None
            sparse = None
            if isinstance(vecs, dict) and SPARSE_VECTOR in vecs:
                sv = vecs[SPARSE_VECTOR]
                sparse = {"indices": list(sv.indices), "values": list(sv.values)}
            envelopes.append(
                {
                    "id": p.id,
                    "vector": dense,
                    "sparse": sparse,
                    "payload": p.payload,
                    "client_sequence": (p.payload or {}).get("_sync_meta", {}).get(
                        "client_sequence", 0
                    ),
                }
            )
        if offset is None:
            break
    return {"envelopes": envelopes}


class SnapshotRequest(BaseModel):
    # The device's immutable-shard manifest (from snapshot_manifest()). Sent for
    # the real partial-snapshot handshake; the current hub ships the full fact
    # set (restore is idempotent by point id) rather than diffing segments.
    manifest: Dict[str, Any] = {}


def _all_fact_envelopes() -> List[Dict[str, Any]]:
    """Scroll the whole `facts` collection into pull envelopes (fleet-wide, so a
    device learns facts other devices reported)."""
    envelopes: List[Dict[str, Any]] = []
    if not client.collection_exists(FACTS_COLLECTION):
        return envelopes
    offset = None
    while True:
        points, offset = client.scroll(
            FACTS_COLLECTION, limit=1000, offset=offset,
            with_payload=True, with_vectors=True,
        )
        for p in points:
            vecs = p.vector or {}
            dense = vecs.get(DENSE_VECTOR) if isinstance(vecs, dict) else None
            sparse = None
            if isinstance(vecs, dict) and SPARSE_VECTOR in vecs:
                sv = vecs[SPARSE_VECTOR]
                sparse = {"indices": list(sv.indices), "values": list(sv.values)}
            envelopes.append({"id": p.id, "vector": dense, "sparse": sparse, "payload": p.payload})
        if offset is None:
            break
    return envelopes


def _build_edge_snapshot(envelopes: List[Dict[str, Any]], work_dir: str) -> Optional[str]:
    """Pack the hub's facts into a real Qdrant Edge snapshot tar.

    The device restores this with `update_from_snapshot` — the only sanctioned
    way to populate its immutable shard. The dense vector is named after the
    edge adapter (stamped as `payload.model` on every point) so the shapes match
    on restore; the segment is optimized so its version rises above the empty
    destination's and the restore actually takes.
    """
    import qdrant_edge as qe

    if not envelopes:
        return None
    sample = envelopes[0]
    dense_name = (sample.get("payload") or {}).get("model", "text_dense")
    dim = len(sample["vector"])

    shard_dir = os.path.join(work_dir, "hub_shard")
    os.makedirs(shard_dir, exist_ok=True)
    cfg = qe.EdgeConfig(
        vectors={dense_name: qe.EdgeVectorParams(size=dim, distance=qe.Distance.Cosine)},
        sparse_vectors={SPARSE_VECTOR: qe.EdgeSparseVectorParams(modifier=qe.Modifier.Idf)},
    )
    shard = qe.EdgeShard.create(shard_dir, cfg)
    try:
        points = []
        for env in envelopes:
            vector: Dict[str, Any] = {dense_name: env["vector"]}
            if env.get("sparse") is not None:
                vector[SPARSE_VECTOR] = qe.SparseVector(
                    indices=env["sparse"]["indices"], values=env["sparse"]["values"]
                )
            points.append(qe.Point(id=env["id"], vector=vector, payload=env.get("payload")))
        shard.update(qe.UpdateOperation.upsert_points(points=points))
        shard.optimize()
        shard.flush()
    finally:
        shard.close()

    tar_path = os.path.join(work_dir, "snapshot.tar")
    with tarfile.open(tar_path, "w") as tar:
        for name in os.listdir(shard_dir):
            tar.add(os.path.join(shard_dir, name), arcname=name)
    return tar_path


@app.post("/snapshot/{device_id}")
def snapshot(device_id: str, req: SnapshotRequest) -> Response:
    """Materialize a real Edge partial snapshot of the fleet's facts (Phase 4).

    Returns the snapshot tar as a file download, or 204 when the hub holds
    nothing. The device applies it with `update_from_snapshot`.
    """
    envelopes = _all_fact_envelopes()
    work_dir = tempfile.mkdtemp(prefix="hub_snapshot_")
    tar_path = _build_edge_snapshot(envelopes, work_dir)
    if tar_path is None:
        shutil.rmtree(work_dir, ignore_errors=True)
        return Response(status_code=204)
    # Clean up the staging dir once the response has been streamed.
    return FileResponse(
        tar_path,
        media_type="application/x-tar",
        filename="snapshot.tar",
        background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True),
    )
