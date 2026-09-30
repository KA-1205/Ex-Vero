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

Phase 3 scope: the gateway just stores points + events. The consensus fold
(Phase 5) and partial snapshots (Phase 4) build on top of this, unchanged here.
"""

import hashlib
import os
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
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
