"""Ex-Vero — Cloud Gateway.

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
  GET  /consensus/{key}       — the trust-weighted fold for a corroboration_key
                                (Phase 5)

Phase 3 stores points + events; Phase 4 adds the partial-snapshot endpoint
(learn from the fleet); Phase 5 folds the event log into CONFIRMED / DISPUTED /
LWW verdicts via the pure `consensus_fold` module.
"""

import hashlib
import os
import shutil
import tarfile
import tempfile
import threading
import time
import time
from typing import Any, Dict, List, Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
from qdrant_client import QdrantClient, models

from consensus_fold import DEFAULT_DEVICE_TRUST, DEFAULT_THRESHOLD, derive_device_trust, fold_consensus

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
# Creating a collection on a cold Qdrant Server legitimately takes tens of
# seconds — measured at 20s for the events collection on first boot, before
# optimizers settle. A 2s budget guaranteed failure on exactly the cold start
# this gateway runs under `docker compose up`, which is what tipped it onto its
# in-memory fallback and made every later ingest 500.
QDRANT_TIMEOUT_S = float(os.environ.get("QDRANT_TIMEOUT_S", "60"))
QDRANT_CONNECT_ATTEMPTS = int(os.environ.get("QDRANT_CONNECT_ATTEMPTS", "5"))
# Falling back to an in-memory store means the hub keeps "working" while the
# data goes nowhere, which is worse than being down. It stays available for
# local single-process use and for tests, but now has to be asked for.
ALLOW_MEMORY_FALLBACK = os.environ.get(
    "GATEWAY_ALLOW_MEMORY_FALLBACK", ""
).strip().lower() in ("1", "true", "yes")
FACTS_COLLECTION = "facts"
EVENTS_COLLECTION = "fact_events"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "text_bm25"
# Supermajority needed to call a multi-device fact CONFIRMED (config-overridable).
CONSENSUS_THRESHOLD = float(os.environ.get("CONSENSUS_THRESHOLD", DEFAULT_THRESHOLD))
CONSENSUS_DECAY = float(os.environ.get("CONSENSUS_DECAY", "0.0"))


@asynccontextmanager
async def _lifespan(application: FastAPI):
    _startup()
    yield


app = FastAPI(title="Ex-Vero — Cloud Gateway", lifespan=_lifespan)

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
        # Phase 5: the consensus fold selects a fact's events by corroboration_key.
        client.create_payload_index(
            EVENTS_COLLECTION, "corroboration_key", models.PayloadSchemaType.KEYWORD
        )


def _ensure_facts_collection(dense_size: int) -> None:
    if not client.collection_exists(FACTS_COLLECTION):
        vectors_cfg = {
            "text": models.VectorParams(size=384, distance=models.Distance.COSINE),
            "image": models.VectorParams(size=512, distance=models.Distance.COSINE),
        }
        if DENSE_VECTOR not in vectors_cfg:
            vectors_cfg[DENSE_VECTOR] = models.VectorParams(
                size=dense_size, distance=models.Distance.COSINE
            )
        client.create_collection(
            collection_name=FACTS_COLLECTION,
            vectors_config=vectors_cfg,
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


def _startup() -> None:
    global client
    if QDRANT_URL == ":memory:":
        client = QdrantClient(location=":memory:")
        _ensure_events_collection()
        _init_sequencer()
        return

    # Connect to QDRANT_URL, retrying: the first attempt routinely loses the
    # race against a cold Qdrant Server, and a single attempt is not a
    # connection strategy.
    last_err: Optional[Exception] = None
    for attempt in range(1, QDRANT_CONNECT_ATTEMPTS + 1):
        try:
            client = QdrantClient(url=QDRANT_URL, timeout=QDRANT_TIMEOUT_S)
            _ensure_events_collection()
            _init_sequencer()
            return
        except Exception as e:  # noqa: BLE001 - report whatever went wrong
            last_err = e
            print(
                f"Qdrant at {QDRANT_URL} not ready "
                f"(attempt {attempt}/{QDRANT_CONNECT_ATTEMPTS}): {type(e).__name__}: {e}"
            )
            if attempt < QDRANT_CONNECT_ATTEMPTS:
                time.sleep(min(2.0 * attempt, 10.0))

    if not ALLOW_MEMORY_FALLBACK:
        raise RuntimeError(
            f"Qdrant at {QDRANT_URL} unreachable after {QDRANT_CONNECT_ATTEMPTS} "
            f"attempts ({last_err}). Refusing to fall back to an in-memory store, "
            f"because the hub would accept writes and lose them. Set "
            f"GATEWAY_ALLOW_MEMORY_FALLBACK=1 to allow it deliberately."
        )

    # Deliberate local-only mode, opted into above.
    print(f"Falling back to in-memory Qdrant (GATEWAY_ALLOW_MEMORY_FALLBACK set)")
    client = QdrantClient(location=":memory:")
    _ensure_events_collection()
    _init_sequencer()


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

    dense_size = len(req.envelopes[0]["vector"]) if req.envelopes[0].get("vector") else 384
    _ensure_facts_collection(dense_size)

    fact_points: List[models.PointStruct] = []
    event_points: List[models.PointStruct] = []
    acked: List[int] = []

    for env in req.envelopes:
        payload = env.get("payload") or {}
        model_name = payload.get("model", "text")
        vectors: Dict[str, Any] = {}
        if env.get("vector") is not None:
            v = env["vector"]
            # Map into the collection's named vector slot
            if len(v) == 512:
                vectors["image"] = v
            elif len(v) == 384:
                vectors["text"] = v
            else:
                vectors[DENSE_VECTOR] = v

        if env.get("sparse"):
            vectors[SPARSE_VECTOR] = models.SparseVector(
                indices=env["sparse"]["indices"], values=env["sparse"]["values"]
            )
        fact_points.append(
            models.PointStruct(id=env["id"], vector=vectors, payload=payload)
        )

        seq = _next_seq()  # hub-assigned ordering; device wall-clock stays metadata
        event_id = _event_id(req.device_id, env["id"], env["client_sequence"])
        payload = env.get("payload") or {}
        # Phase 5: carry everything the consensus fold needs. `seq` is the only
        # ordering key; `client_timestamp_ns` rides along as metadata only.
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
                    "corroboration_key": payload.get("corroboration_key"),
                    "value": payload.get("value"),
                    "client_timestamp_ns": payload.get("client_timestamp_ns"),
                    "device_trust_at_report": payload.get(
                        "device_trust_at_report", DEFAULT_DEVICE_TRUST
                    ),
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


def _events_for_key(corroboration_key: str) -> List[Dict[str, Any]]:
    """Scroll the append-only event log for one fact's events."""
    events: List[Dict[str, Any]] = []
    if not client.collection_exists(EVENTS_COLLECTION):
        return events
    flt = models.Filter(
        must=[
            models.FieldCondition(
                key="corroboration_key", match=models.MatchValue(value=corroboration_key)
            )
        ]
    )
    offset = None
    while True:
        points, offset = client.scroll(
            EVENTS_COLLECTION, scroll_filter=flt, limit=1000, offset=offset,
            with_payload=True, with_vectors=False,
        )
        events.extend(p.payload for p in points)
        if offset is None:
            break
    return events


@app.get("/consensus/{corroboration_key}")
def consensus(corroboration_key: str) -> Dict[str, Any]:
    """Fold all live events for a fact into one trust-weighted verdict (Phase 5).

    The fold is the pure `consensus_fold.fold_consensus` — ordered strictly by
    hub `seq`, trust derived from the log, retraction terminal — so the hub is
    the single sequencer and the verdict is reproducible.
    """
    events = _events_for_key(corroboration_key)
    result = fold_consensus(events, threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY)
    # Trust is derived here (never stored), so the dashboard can watch it move.
    result["device_trust"] = derive_device_trust(
        events, threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY
    )
    return result


class RetractRequest(BaseModel):
    device_id: str
    point_id: int
    corroboration_key: Optional[str] = None
    client_sequence: int = 0
    client_timestamp_ns: Optional[int] = None


@app.post("/retract")
def retract_event(req: RetractRequest) -> Dict[str, Any]:
    """Append a RETRACTED event to the hub's fact_events log."""
    seq = _next_seq()
    event_id = _event_id(req.device_id, req.point_id, req.client_sequence)
    key = req.corroboration_key
    # If not supplied, try to find corroboration_key from existing events for this point
    if not key and client.collection_exists(EVENTS_COLLECTION):
        flt = models.Filter(
            must=[
                models.FieldCondition(key="device_id", match=models.MatchValue(value=req.device_id)),
                models.FieldCondition(key="point_id", match=models.MatchValue(value=req.point_id)),
            ]
        )
        points, _ = client.scroll(EVENTS_COLLECTION, scroll_filter=flt, limit=1, with_payload=True)
        if points:
            key = points[0].payload.get("corroboration_key")

    event_point = models.PointStruct(
        id=event_id,
        vector=[0.0],
        payload={
            "device_id": req.device_id,
            "point_id": req.point_id,
            "event_type": "RETRACTED",
            "seq": seq,
            "client_sequence": req.client_sequence,
            "corroboration_key": key,
            "value": None,
            "client_timestamp_ns": req.client_timestamp_ns or int(time.time() * 1e9),
            "device_trust_at_report": 0.0,
        },
    )
    client.upsert(EVENTS_COLLECTION, points=[event_point])
    return {"retracted": True, "seq": seq, "point_id": req.point_id}


class InjectConflictRequest(BaseModel):
    corroboration_key: str
    assignments: Dict[str, str]


@app.post("/demo/inject-conflict")
def inject_conflict(req: InjectConflictRequest) -> Dict[str, Any]:
    """Scripted conflict injection across multiple devices for demo/testing."""
    events_to_insert = []
    now_ns = int(time.time() * 1e9)
    for dev_id, val in req.assignments.items():
        seq = _next_seq()
        pid = abs(hash(f"{req.corroboration_key}:{val}:{dev_id}")) % 1000000 + 1
        eid = _event_id(dev_id, pid, seq)
        events_to_insert.append(
            models.PointStruct(
                id=eid,
                vector=[0.0],
                payload={
                    "device_id": dev_id,
                    "point_id": pid,
                    "event_type": "OBSERVED",
                    "seq": seq,
                    "client_sequence": seq,
                    "corroboration_key": req.corroboration_key,
                    "value": val,
                    "client_timestamp_ns": now_ns,
                    "device_trust_at_report": DEFAULT_DEVICE_TRUST,
                },
            )
        )
    if events_to_insert:
        client.upsert(EVENTS_COLLECTION, points=events_to_insert)

    # Return updated consensus for the key
    return {
        "injected": True,
        "corroboration_key": req.corroboration_key,
        "consensus": consensus(req.corroboration_key),
    }


@app.get("/cloud/state")
def cloud_state() -> Dict[str, Any]:
    """Merged trusted fleet state across all corroboration keys (API.md §8).

    Exposes facts formatted for the ApiCloudFact frontend contract:
    {
      "facts": [
        {
          "corroboration_key": str,
          "state": "CONFIRMED" | "DISPUTED",
          "value": str,
          "confidence": float,
          "corroborating_devices": list[str],
          "updated_at": str (ISO 8601)
        }
      ],
      "device_trust": {device_id: score}
    }
    """
    all_events: List[Dict[str, Any]] = []
    if client.collection_exists(EVENTS_COLLECTION):
        offset = None
        while True:
            pts, offset = client.scroll(
                EVENTS_COLLECTION, limit=1000, offset=offset, with_payload=True, with_vectors=False
            )
            for p in pts:
                if p.payload:
                    all_events.append(p.payload)
            if offset is None:
                break

    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for ev in all_events:
        k = ev.get("corroboration_key")
        if k:
            by_key.setdefault(k, []).append(ev)

    facts = []
    from datetime import datetime, timezone
    for k in sorted(by_key):
        evts = by_key[k]
        folded = fold_consensus(evts, threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY)
        if folded.get("status") == "ABSENT" or not folded.get("value"):
            continue

        st = "CONFIRMED" if folded.get("confidence", 0.0) >= CONSENSUS_THRESHOLD else "DISPUTED"

        winner_val = folded.get("value")
        corrob: List[str] = []
        for val_info in folded.get("values", []):
            if val_info.get("value") == winner_val:
                corrob = val_info.get("devices", [])
                break

        latest_evt = max(evts, key=lambda e: e.get("seq", 0))
        ts_ns = latest_evt.get("client_timestamp_ns")
        if isinstance(ts_ns, (int, float)) and ts_ns > 0:
            updated_at = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).isoformat()
        else:
            updated_at = datetime.now(timezone.utc).isoformat()

        facts.append({
            "corroboration_key": k,
            "state": st,
            "value": str(winner_val),
            "confidence": folded.get("confidence", 0.0),
            "corroborating_devices": sorted(corrob),
            "updated_at": updated_at,
        })

    device_trust = derive_device_trust(
        all_events, threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY
    )
    return {"facts": facts, "device_trust": device_trust}

