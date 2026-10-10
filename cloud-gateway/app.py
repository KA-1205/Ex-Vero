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
import hmac
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
from qdrant_client import QdrantClient, models

from consensus_fold import DEFAULT_DEVICE_TRUST, DEFAULT_THRESHOLD, derive_device_trust, fold_consensus
import vector_space

# The snapshot builder is shared with the edge (one builder, no drift): the hub
# packs the same real Edge shard the device restores. Importing it at module load
# also pulls in `qdrant_edge`, so a missing Edge engine fails the gateway fast
# rather than at the first /snapshot call. `../src` is on path in tests and in a
# repo-root run; the Dockerfile vendors the module next to app.py for the image.
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC_DIR):
    sys.path.insert(0, _SRC_DIR)
try:
    from edge_node.snapshot import build_snapshot_tar
except ImportError:  # Docker image: snapshot.py is COPY'd next to app.py
    from snapshot import build_snapshot_tar

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
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
# A dedicated meta collection holds the sequencer high-water mark in a single
# point, so a restart resumes the seq counter by reading ONE point instead of
# scanning the whole event log (P0-4).
META_COLLECTION = "gateway_meta"
SEQ_POINT_ID = 0
# The named vector space is defined once in `vector_space` (P0-1). The hub never
# invents a slot from `len(vector)`; it reads the slot map from there.
SPARSE_VECTOR = vector_space.SPARSE_SLOT
# Supermajority needed to call a multi-device fact CONFIRMED (config-overridable).
CONSENSUS_THRESHOLD = float(os.environ.get("CONSENSUS_THRESHOLD", DEFAULT_THRESHOLD))
CONSENSUS_DECAY = float(os.environ.get("CONSENSUS_DECAY", "0.0"))


# --- Device identity & HMAC authentication (B1 / P1-1) ---------------------
# Each write request is signed by the reporting device: an HMAC-SHA256 over
# `"{timestamp}.{raw_body}"` with that device's shared secret. Secrets live in
# config/env (never in the repo): `GATEWAY_DEVICE_SECRETS` is a JSON object of
# {device_id: secret}. Admin endpoints use a separate `GATEWAY_ADMIN_TOKEN`.
#
# When no device secrets are configured the gateway runs OPEN (single-process
# dev/test use) — exactly the mode the local unit tests exercise. Configure at
# least one secret and every write must be signed by a KNOWN device, so a
# fabricated id or a tampered body is rejected (401) and never reaches the log.
AUTH_TS_HEADER = "x-timestamp"
AUTH_SIG_HEADER = "x-signature"
AUTH_DEVICE_HEADER = "x-device-id"
ADMIN_TOKEN_HEADER = "x-admin-token"


def _device_secrets() -> Dict[str, str]:
    """Parse the device-secret map from env each call (cheap, lets a launcher or
    a test set it without re-importing). Malformed JSON = no secrets = open."""
    raw = os.environ.get("GATEWAY_DEVICE_SECRETS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


def _admin_token() -> Optional[str]:
    tok = os.environ.get("GATEWAY_ADMIN_TOKEN", "").strip()
    return tok or None


def expected_signature(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over `"{timestamp}.{body}"` — binds both the body and the
    timestamp so neither can be altered without the device's secret."""
    msg = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


async def require_device(request: "Request") -> Optional[str]:
    """Authenticate the reporting device for a write endpoint.

    Returns the authenticated device id, or None when auth is disabled (no
    secrets configured). Raises 401 on a missing/unknown id or a bad signature.
    Verified with `hmac.compare_digest` (constant-time).
    """
    secrets = _device_secrets()
    if not secrets:
        return None  # open mode: no secrets configured

    device_id = request.headers.get(AUTH_DEVICE_HEADER)
    timestamp = request.headers.get(AUTH_TS_HEADER)
    signature = request.headers.get(AUTH_SIG_HEADER)
    if not device_id or not timestamp or not signature:
        raise HTTPException(status_code=401, detail="missing device signature")

    secret = secrets.get(device_id)
    if secret is None:
        # Unknown device id — a fabricated reporter never enters the log.
        raise HTTPException(status_code=401, detail="unknown device id")

    body = await request.body()
    expected = expected_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="bad device signature")
    return device_id


async def require_admin(request: "Request") -> None:
    """Gate an admin endpoint behind the admin token (when one is configured)."""
    token = _admin_token()
    if token is None:
        return  # open mode
    presented = request.headers.get(ADMIN_TOKEN_HEADER) or ""
    if not hmac.compare_digest(token, presented):
        raise HTTPException(status_code=401, detail="admin authentication required")



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


def _all_events() -> List[Dict[str, Any]]:
    """Scroll the whole append-only event log (every key, every device)."""
    all_events: List[Dict[str, Any]] = []
    if client is None or not client.collection_exists(EVENTS_COLLECTION):
        return all_events
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
    return all_events


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
        # P0-4: point_id is filtered for liveness/tombstone checks and the /delta
        # path; index it so those are not full scans on a real server.
        client.create_payload_index(
            EVENTS_COLLECTION, "point_id", models.PayloadSchemaType.INTEGER
        )


def _ensure_meta_collection() -> None:
    if not client.collection_exists(META_COLLECTION):
        client.create_collection(
            collection_name=META_COLLECTION,
            vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE),
        )



def _ensure_facts_collection() -> None:
    if not client.collection_exists(FACTS_COLLECTION):
        # One source of truth for the named vectors (P0-1): build the collection
        # from the slot->dim map, never from an incoming vector's length.
        vectors_cfg = {
            name: models.VectorParams(size=dim, distance=models.Distance.COSINE)
            for name, dim in vector_space.DENSE_SLOTS.items()
        }
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


def _persist_seq_highwater() -> None:
    """Write the current high-water seq to the meta point (once per batch, not
    per event). A restart reads this one point instead of scanning the log."""
    with _seq_lock:
        value = _seq
    client.upsert(
        META_COLLECTION,
        points=[models.PointStruct(id=SEQ_POINT_ID, vector=[0.0], payload={"seq_highwater": value})],
    )


def _init_sequencer() -> None:
    """Resume the seq counter from the meta point's high-water mark — a single
    point read, never a full scan of fact_events (P0-4). A gateway restart thus
    never re-issues an old sequence number."""
    global _seq
    _ensure_meta_collection()
    recs = client.retrieve(META_COLLECTION, ids=[SEQ_POINT_ID], with_payload=True, with_vectors=False)
    highest = int(recs[0].payload.get("seq_highwater", 0)) if recs else 0
    with _seq_lock:
        _seq = highest



def _startup() -> None:
    global client, QDRANT_URL
    # Re-read QDRANT_URL at startup so a test (or launcher) that sets the env var
    # after import — e.g. QDRANT_URL=":memory:" in the TestClient fixture — is
    # honored instead of the value captured when the module was first imported.
    QDRANT_URL = os.environ.get("QDRANT_URL", QDRANT_URL)
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
            client = QdrantClient(
                url=QDRANT_URL,
                api_key=QDRANT_API_KEY,
                timeout=QDRANT_TIMEOUT_S,
            )
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

    P0-4: a single `order_by(client_sequence DESC) limit 1` filtered query — not
    a full scroll of the device's events — so this stays O(1) as the log grows.
    `client_sequence` is Integer-indexed, which `order_by` requires.
    """
    if not client.collection_exists(EVENTS_COLLECTION):
        return {"max_client_sequence": -1}
    flt = models.Filter(
        must=[models.FieldCondition(key="device_id", match=models.MatchValue(value=device_id))]
    )
    points, _ = client.scroll(
        EVENTS_COLLECTION, scroll_filter=flt, limit=1,
        order_by=models.OrderBy(key="client_sequence", direction=models.Direction.DESC),
        with_payload=True, with_vectors=False,
    )
    if not points:
        return {"max_client_sequence": -1}
    return {"max_client_sequence": int(points[0].payload.get("client_sequence", -1))}


@app.post("/ingest")
def ingest(req: IngestRequest, authed_device: Optional[str] = Depends(require_device)) -> Dict[str, Any]:
    """Upsert the delta batch into `facts` and append OBSERVED events.

    Idempotent: fact points key on the edge's deterministic id and events key on
    (device, point, client_sequence), so a crash-retry re-push writes no
    duplicates.
    """
    # When auth is on, the authenticated device id is authoritative: a device
    # cannot submit a batch under another id (B1).
    if authed_device is not None and authed_device != req.device_id:
        raise HTTPException(status_code=401, detail="device id does not match signature")
    if not req.envelopes:
        return {"acked_ids": [], "count": 0}

    _ensure_facts_collection()

    # Insert-if-absent (P0-3): a crash-retry must never change an existing
    # event's hub `seq`, and must never clobber a fact (e.g. re-create a
    # tombstoned one). So we skip ids the hub already holds and only assign a new
    # seq / write a point for genuinely new ones. Acks still cover every id.
    env_ids = [env["id"] for env in req.envelopes]
    existing_facts = {r.id for r in client.retrieve(
        FACTS_COLLECTION, ids=env_ids, with_payload=False, with_vectors=False
    )}
    event_ids = [
        _event_id(req.device_id, env["id"], env["client_sequence"]) for env in req.envelopes
    ]
    existing_events = {str(r.id) for r in client.retrieve(
        EVENTS_COLLECTION, ids=event_ids, with_payload=False, with_vectors=False
    )}

    fact_points: List[models.PointStruct] = []
    event_points: List[models.PointStruct] = []
    acked: List[int] = []

    # Trust is HUB-DERIVED from the log, never read from the client payload
    # (B2 / P1-2): a device cannot inflate its own weight by sending a high
    # `device_trust_at_report`. We stamp the reporter's current derived trust at
    # report time; the fold recomputes trust from the whole log regardless.
    _trust_now = derive_device_trust(
        _all_events(), threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY
    )
    device_trust_at_report = _trust_now.get(req.device_id, DEFAULT_DEVICE_TRUST)

    now_ns = int(time.time() * 1e9)
    for env in req.envelopes:
        payload = env.get("payload") or {}
        acked.append(env["id"])

        if env["id"] not in existing_facts:
            vectors: Dict[str, Any] = {}
            if env.get("vector") is not None:
                # Place the dense vector in its named slot from the single source
                # of truth, never by guessing from len(vector).
                slot = vector_space.slot_for_envelope(env)
                if slot is None:
                    raise ValueError(
                        f"cannot place dense vector of length "
                        f"{len(env['vector'])} into any known slot "
                        f"{sorted(vector_space.DENSE_SLOTS)}"
                    )
                vectors[slot] = env["vector"]

            if env.get("sparse"):
                vectors[SPARSE_VECTOR] = models.SparseVector(
                    indices=env["sparse"]["indices"], values=env["sparse"]["values"]
                )
            fact_points.append(
                models.PointStruct(id=env["id"], vector=vectors, payload=payload)
            )

        event_id = _event_id(req.device_id, env["id"], env["client_sequence"])
        if str(event_id) in existing_events:
            continue  # already logged — keep its original seq (insert-if-absent)

        seq = _next_seq()  # hub-assigned ordering; device wall-clock stays metadata
        # Phase 5: carry everything the consensus fold needs. Within one device
        # `client_sequence` orders events; `seq` orders across devices and is the
        # tie-break. `received_at_ns` is the hub clock (used for P1-4 staleness).
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
                    "received_at_ns": now_ns,
                    "device_trust_at_report": device_trust_at_report,
                },
            )
        )

    if fact_points:
        client.upsert(FACTS_COLLECTION, points=fact_points)
    if event_points:
        client.upsert(EVENTS_COLLECTION, points=event_points)
        _persist_seq_highwater()  # one meta write per batch (P0-4)
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
            if (p.payload or {}).get("_tombstoned") == 1:
                continue  # retracted fact (P0-2): excluded from pull
            env = _envelope_from_point(p)
            env["client_sequence"] = (p.payload or {}).get("_sync_meta", {}).get(
                "client_sequence", 0
            )
            envelopes.append(env)
        if offset is None:
            break
    return {"envelopes": envelopes}


class SnapshotRequest(BaseModel):
    # The device's immutable-shard manifest (from snapshot_manifest()). Sent for
    # the real partial-snapshot handshake; the current hub ships the full fact
    # set (restore is idempotent by point id) rather than diffing segments.
    manifest: Dict[str, Any] = {}


def _envelope_from_point(p) -> Dict[str, Any]:
    """Turn a stored `facts` point into a pull envelope carrying **every** named
    vector it holds (P0-1).

    The point was written into a named slot (`text`/`image`) decided from the
    single source of truth, so we read each named slot back rather than guessing
    one key. `vectors` maps slot -> dense list; `vector` is the primary dense
    (the point's own `payload.model` slot) kept for backward-compatible readers;
    `sparse` is the BM25 leg.
    """
    vecs = p.vector or {}
    dense_vectors: Dict[str, List[float]] = {}
    sparse = None
    if isinstance(vecs, dict):
        for name, val in vecs.items():
            if name == SPARSE_VECTOR:
                sparse = {"indices": list(val.indices), "values": list(val.values)}
            elif name in vector_space.DENSE_SLOTS and val is not None:
                dense_vectors[name] = list(val)
    model = (p.payload or {}).get("model")
    if isinstance(model, str) and model in dense_vectors:
        primary = dense_vectors[model]
    else:
        primary = next(iter(dense_vectors.values()), None)
    return {
        "id": p.id,
        "vector": primary,
        "vectors": dense_vectors,
        "sparse": sparse,
        "payload": p.payload,
    }


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
            if (p.payload or {}).get("_tombstoned") == 1:
                continue  # retracted fact (P0-2): excluded from the snapshot
            envelopes.append(_envelope_from_point(p))
        if offset is None:
            break
    return envelopes


def _build_edge_snapshot(envelopes: List[Dict[str, Any]], work_dir: str) -> Optional[str]:
    """Pack the hub's facts into a real Qdrant Edge snapshot tar.

    Delegates to the **shared** `build_snapshot_tar` (one builder, used by the
    edge tests too) so the hub and device never drift on shard shape. The config
    carries every named dense slot from the single source of truth, so a mixed
    text/image fact set round-trips and `update_from_snapshot` — which the device
    runs against an immutable shard that has both slots — never rejects a shape
    mismatch. Each envelope carries its named `vectors`, so a missing slot is
    filled by the builder rather than crashing on a `None` dense vector.
    """
    if not envelopes:
        return None
    return build_snapshot_tar(
        envelopes,
        dense_name=vector_space.DEFAULT_DENSE_SLOT,
        dim=vector_space.DENSE_SLOTS[vector_space.DEFAULT_DENSE_SLOT],
        work_dir=work_dir,
        extra_vectors=vector_space.extra_dense_slots(),
    )


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


def _point_is_live(point_id: int) -> bool:
    """True while at least one device's latest event for this point is OBSERVED.

    A point is dead (and so may be tombstoned) once every device that reported it
    has retracted it. Ordering is by hub `seq`, consistent with the fold.
    """
    if not client.collection_exists(EVENTS_COLLECTION):
        return False
    flt = models.Filter(
        must=[models.FieldCondition(key="point_id", match=models.MatchValue(value=point_id))]
    )
    events: List[Dict[str, Any]] = []
    offset = None
    while True:
        pts, offset = client.scroll(
            EVENTS_COLLECTION, scroll_filter=flt, limit=1000, offset=offset,
            with_payload=True, with_vectors=False,
        )
        events.extend(p.payload for p in pts)
        if offset is None:
            break
    latest_by_dev: Dict[str, str] = {}
    for e in sorted(events, key=lambda e: e.get("seq", 0)):
        latest_by_dev[e.get("device_id")] = e.get("event_type")
    return any(t == "OBSERVED" for t in latest_by_dev.values())



@app.get("/consensus/{corroboration_key}")
def consensus(corroboration_key: str) -> Dict[str, Any]:
    """Fold all live events for a fact into one trust-weighted verdict (Phase 5).

    The fold is the pure `consensus_fold.fold_consensus` — ordered strictly by
    hub `seq`, trust derived from the log, retraction terminal — so the hub is
    the single sequencer and the verdict is reproducible.
    """
    events = _events_for_key(corroboration_key)
    # Trust is derived here from the log (never stored, never client-supplied),
    # then fed back as the per-device weight so an outvoted device actually
    # counts for less — trust drives the fold, it is not display-only (B2).
    device_trust = derive_device_trust(
        events, threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY
    )
    result = fold_consensus(
        events, threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY, device_trust=device_trust
    )
    result["device_trust"] = device_trust
    return result


class RetractRequest(BaseModel):
    device_id: str
    point_id: int
    corroboration_key: Optional[str] = None
    client_sequence: int = 0
    client_timestamp_ns: Optional[int] = None


@app.post("/retract")
def retract_event(req: RetractRequest, authed_device: Optional[str] = Depends(require_device)) -> Dict[str, Any]:
    """Append a RETRACTED event and tombstone the fact if it folds to ABSENT.

    Retraction is end-to-end (P0-2): the event joins the append-only log so the
    pure fold withdraws the device's vote, and if no live device still asserts
    this point the fact is tombstoned (payload flag) so it drops out of `/facts`
    and `/snapshot` and is reported to pulling devices via `/retracted`.

    A device may retract only its own observation (B1): with auth on, the
    authenticated id must match the request's device id.
    """
    if authed_device is not None and authed_device != req.device_id:
        raise HTTPException(status_code=401, detail="device id does not match signature")
    event_id = _event_id(req.device_id, req.point_id, req.client_sequence)
    key = req.corroboration_key
    # Resolve the key from the point's own stored observation if not supplied.
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

    if not key:
        # No supplied key and no stored observation to resolve it from.
        raise HTTPException(status_code=404, detail="no observation for (device, point) to retract")

    # Insert-if-absent (P0-3): a retried retract keeps its original seq.
    existing = client.retrieve(
        EVENTS_COLLECTION, ids=[event_id], with_payload=True, with_vectors=False
    )
    if existing:
        seq = existing[0].payload.get("seq")
    else:
        seq = _next_seq()
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
                "received_at_ns": int(time.time() * 1e9),
                "device_trust_at_report": 0.0,
            },
        )
        client.upsert(EVENTS_COLLECTION, points=[event_point])
        _persist_seq_highwater()  # P0-4


    # Tombstone the fact if nothing live references the point any more.
    tombstoned = False
    if not _point_is_live(req.point_id) and client.collection_exists(FACTS_COLLECTION):
        client.set_payload(
            FACTS_COLLECTION, payload={"_tombstoned": 1}, points=[req.point_id]
        )
        tombstoned = True
    return {"retracted": True, "seq": seq, "point_id": req.point_id, "tombstoned": tombstoned}


@app.get("/retracted/{device_id}")
def retracted(device_id: str) -> Dict[str, Any]:
    """Point ids the hub has tombstoned (folded to ABSENT). A pulling device
    deletes any of these it still holds — the snapshot only *adds* points, so
    this is how a retraction propagates to a device that already learned the
    fact."""
    ids: List[int] = []
    if client.collection_exists(FACTS_COLLECTION):
        flt = models.Filter(
            must=[models.FieldCondition(key="_tombstoned", match=models.MatchValue(value=1))]
        )
        offset = None
        while True:
            pts, offset = client.scroll(
                FACTS_COLLECTION, scroll_filter=flt, limit=1000, offset=offset,
                with_payload=False, with_vectors=False,
            )
            ids.extend(p.id for p in pts)
            if offset is None:
                break
    return {"retracted_ids": sorted(ids)}



class InjectConflictRequest(BaseModel):
    corroboration_key: str
    assignments: Dict[str, str]


@app.post("/demo/inject-conflict")
def inject_conflict(req: InjectConflictRequest, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
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
        _persist_seq_highwater()  # P0-4

    # Return updated consensus for the key
    return {
        "injected": True,
        "corroboration_key": req.corroboration_key,
        "consensus": consensus(req.corroboration_key),
    }


@app.get("/cloud/state")
def cloud_state(modality: Optional[str] = None) -> Dict[str, Any]:
    """Merged trusted fleet state across all corroboration keys (API.md §8)."""
    all_events = _all_events()

    # Hub-derived trust, computed once from the whole log and used as the fold
    # weight for every key (B2): unforgeable and not display-only.
    device_trust = derive_device_trust(
        all_events, threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY
    )

    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for ev in all_events:
        k = ev.get("corroboration_key")
        if k:
            by_key.setdefault(k, []).append(ev)

    facts = []
    from datetime import datetime, timezone
    for k in sorted(by_key):
        evts = by_key[k]
        folded = fold_consensus(
            evts, threshold=CONSENSUS_THRESHOLD, decay=CONSENSUS_DECAY, device_trust=device_trust
        )
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

        item_mod = latest_evt.get("modality", "vision" if any(d.startswith("cam-") for d in corrob) else "text")
        thumb = latest_evt.get("thumbnail_url")
        if modality:
            if modality == "vision" and item_mod != "vision" and not thumb:
                continue
            elif modality != "vision" and item_mod != modality:
                continue

        facts.append({
            "corroboration_key": k,
            "state": st,
            "value": str(winner_val),
            "confidence": folded.get("confidence", 0.0),
            "corroborating_devices": sorted(corrob),
            "updated_at": updated_at,
            "modality": item_mod,
            "thumbnail_url": thumb,
        })

    return {"facts": facts, "device_trust": device_trust}

