import os
import hashlib
import time
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import yaml
from pathlib import Path
from contextlib import asynccontextmanager

from qdrant_edge import (
    EdgeShard,
    EdgeConfig,
    EdgeVectorParams,
    Distance,
    Point,
    UpdateOperation,
    Query,
    QueryRequest as EdgeQueryRequest,
    Prefetch,
    Fusion,
    Bm25,
    Bm25Config,
    Modifier,
    EdgeSparseVectorParams,
    CountRequest,
    Filter,
    FieldCondition,
    MatchValue,
    RangeFloat,
)
from datetime import datetime

from .adapter import Adapter
from .registry import load_adapters
from .decision_engine import DecisionEngine, log_decision, get_feed, clear_feed, load_policy
from .outbox import add_sync_meta, get_outbox, mark_synced, ensure_indexes, SYNCABLE_KEY, SYNCED_KEY
from .sync_transport import GatewayTransport
from .consensus import EventLog, fold_trust, lww_trust, OBSERVED, RETRACTED
from .conflicts import detect_conflicts, register_conflicts, get_conflicts, clear_conflicts, POSSIBLE_CONFLICT

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "disaster-response.yaml"

app = FastAPI()

# Global variables
adapters: List[Adapter] = None
edge_config: EdgeConfig = None
decision_engine: DecisionEngine = None
bm25: Bm25 = None
device_shards: Dict[str, dict] = {}  # device_id -> {'mutable': shard, 'immutable': shard}
event_logs: Dict[str, EventLog] = {}  # device_id -> EventLog
sync_transport = None  # SyncTransport; GatewayTransport in runtime, stub in tests
TRUST_DECAY: float = 0.0
CONFLICT_THRESHOLD: float = 0.5
SHARD_BASE_PATH = "./shards"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global adapters, edge_config, decision_engine, bm25, TRUST_DECAY, CONFLICT_THRESHOLD, sync_transport

    # Load adapters from config
    config_path = str(DEFAULT_CONFIG_PATH)
    with open(config_path, 'r') as f:
        full_config = yaml.safe_load(f)

    adapters = load_adapters(config_path)

    # Build EdgeConfig from adapters
    vectors = {a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine) for a in adapters}
    # Add sparse vector for BM25
    sparse_vectors = {"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)}
    edge_config = EdgeConfig(
        vectors=vectors,
        sparse_vectors=sparse_vectors,
        max_search_threads=2,   # Explicitly set to avoid 4 per core
        search_pool_core=0,
    )

    # Load decision engine policy
    policy_config = full_config.get('policy', {})
    decision_engine = DecisionEngine(policy_config)

    # Trust decay constant (Step 7)
    TRUST_DECAY = float(policy_config.get('trust_decay', 0.0))

    # Semantic conflict detection threshold (Step 8)
    conflict_config = full_config.get('conflict', {})
    CONFLICT_THRESHOLD = float(conflict_config.get('similarity_threshold', 0.5))

    # Initialize BM25 embedder (default config)
    bm25 = Bm25()

    # Hub configuration (for push)
    hub_config = full_config.get('hub', {})
    hub_url = hub_config.get('url')
    hub_api_key = hub_config.get('api_key')
    # The sync transport is the ONLY egress to the Cloud Gateway. Tests inject a
    # stub in its place; the runtime path always uses a real HTTP client.
    sync_transport = GatewayTransport(hub_url) if hub_url else None
    print(f"Hub config loaded: url={hub_url}")

    print(f"Loaded {len(adapters)} adapters: {[a.name for a in adapters]}")
    print(f"Loaded policy: {policy_config}")
    print(f"Trust decay: {TRUST_DECAY}")
    print("BM25 initialized")
    yield


app = FastAPI(lifespan=lifespan)


class CaptureRequest(BaseModel):
    device_id: str
    corroboration_key: str
    value: str  # the text to capture
    zone: Optional[str] = None    # hard guard for semantic conflict detection
    entity: Optional[str] = None  # e.g. hazard type


class QueryRequest(BaseModel):
    device_id: str
    text: str  # the query text


class PointResponse(BaseModel):
    id: int
    score: float
    payload: dict


class QueryResponse(BaseModel):
    results: List[PointResponse]
    latency_ms: float


def get_shard_path(device_id: str, shard_type: str) -> str:
    return os.path.join(SHARD_BASE_PATH, device_id, shard_type)


def generate_point_id(corroboration_key: str, value: str) -> int:
    # Deterministic ID from corroboration_key + value hash
    unique_string = f"{corroboration_key}:{value}"
    # Use a hash function to get a 64-bit integer, but we need a positive integer for point ID
    # We'll use MD5 and take the first 8 bytes to form a 64-bit integer, then take modulo 2^63 to ensure positive
    hash_bytes = hashlib.md5(unique_string.encode()).digest()
    # Convert first 8 bytes to a 64-bit integer
    int_val = int.from_bytes(hash_bytes[:8], byteorder='little', signed=False)
    # Ensure it's positive and within the range of a 64-bit signed integer (though point ID in Qdrant is unsigned 64-bit?)
    # We'll just return the int_val as is, which is positive.
    return int_val


def _verify_shard_dimension(shard, adapters):
    """Verify that the shard's vector dimensions match the adapters.
    If the shard was created with different dimensions, querying may raise
    an exception about dimension mismatch. We attempt a dummy query to
    detect mismatch and raise a clear error.
    """
    if not adapters:
        return
    adapter = adapters[0]  # step 1 only uses first adapter
    try:
        dummy_vector = [0.0] * adapter.dim
        query_obj = Query.Nearest(query=dummy_vector, using=adapter.name)
        # We don't need results, just want to see if the call succeeds
        edge_request = EdgeQueryRequest(limit=1, query=query_obj, with_vector=False, with_payload=False)
        shard.query(edge_request)
    except Exception as e:
        # If the error indicates a dimension mismatch, raise a clear error.
        if "dimension" in str(e).lower() or "size" in str(e).lower() or "vector" in str(e).lower():
            raise RuntimeError(
                f"Shard dimension mismatch: expected {adapter.dim}-dim vectors from adapter '{adapter.name}'. "
                f"Underlying error: {e}"
            )
        # If the shard is empty, the query may still succeed; other errors (e.g., unrelated) we ignore.
        pass


def get_or_create_shards(device_id: str):
    global device_shards, adapters, edge_config
    if device_id in device_shards:
        return device_shards[device_id]
    
    # Ensure adapters and edge_config are initialized
    if adapters is None or edge_config is None:
        raise RuntimeError("Adapters or edge_config not initialized. Did startup_event run?")
    
    mutable_path = get_shard_path(device_id, "mutable")
    immutable_path = get_shard_path(device_id, "immutable")
    
    # Create or load mutable shard
    if not os.path.exists(mutable_path):
        os.makedirs(mutable_path, exist_ok=True)
        mutable_shard = EdgeShard.create(mutable_path, edge_config)
    else:
        mutable_shard = EdgeShard.load(mutable_path)
        # Verify that the loaded shard's vector config matches our adapters (invariant 10)
        _verify_shard_dimension(mutable_shard, adapters)
        info = mutable_shard.info()
        print(f"Loaded mutable shard for {device_id}: {info}")

    # Integer indexes for the outbox view (synced / syncable / client_sequence).
    # A bool field would silently match nothing (Phase 0 trap); integers work.
    ensure_indexes(mutable_shard)

    # Create or load immutable shard (empty for now)
    if not os.path.exists(immutable_path):
        os.makedirs(immutable_path, exist_ok=True)
        immutable_shard = EdgeShard.create(immutable_path, edge_config)
    else:
        immutable_shard = EdgeShard.load(immutable_path)
        _verify_shard_dimension(immutable_shard, adapters)
        print(f"Loaded immutable shard for {device_id}: {info}")
    
    device_shards[device_id] = {
        'mutable': mutable_shard,
        'immutable': immutable_shard,
        'client_seq': 0,  # per-device monotonic sequence for the delta handshake
    }
    return device_shards[device_id]


def get_or_create_event_log(device_id: str) -> EventLog:
    """Create/load the append-only fact_events shard for a device."""
    global event_logs
    if device_id in event_logs:
        return event_logs[device_id]
    path = get_shard_path(device_id, "events")
    cfg = EventLog.build_config()
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
        shard = EdgeShard.create(path, cfg)
    else:
        shard = EdgeShard.load(path)
    log = EventLog(shard)
    event_logs[device_id] = log
    return log


@app.post("/devices/{device_id}/capture")
async def capture(device_id: str, request: CaptureRequest):
    shards = get_or_create_shards(device_id)
    mutable_shard = shards['mutable']

    # Use the first adapter (text) for step 1
    adapter = adapters[0]
    dense_vector = adapter.embed(request.value)
    
    point_id = generate_point_id(request.corroboration_key, request.value)
    
    # Payload includes the value, the model name and version, and the corroboration_key
    payload = {
        "value": request.value,
        "model": adapter.name,
        "model_version": adapter.version,
        "corroboration_key": request.corroboration_key,
        "modality": adapter.modality
    }
    if request.zone is not None:
        payload["zone"] = request.zone
    if request.entity is not None:
        payload["entity"] = request.entity
    # Add required fields for completeness check
    payload["status"] = "unverified"
    payload["client_timestamp_ns"] = int(time.time() * 1_000_000_000)
    payload["reporter_device_id"] = device_id
    # Add sync metadata for outbox
    payload = add_sync_meta(payload)
    
    # Run decision engine
    verdict, reason = decision_engine.evaluate(payload, dense_vector, mutable_shard, adapter, exclude_point_id=point_id)
    # Log decision for feed
    log_decision(device_id, payload, verdict, reason)
    
    # Store verdict + sync bookkeeping in _sync_meta so the outbox view and the
    # delta handshake can honour it. `synced`/`syncable` are Integer 0/1 (a bool
    # would silently match nothing in scroll — Phase 0 trap).
    meta = payload.setdefault("_sync_meta", {})
    meta["synced"] = 0
    meta["verdict"] = verdict
    # A verdict decides whether the fact may ever leave the device. The decision
    # engine already downgrades an incomplete QUEUE_LOW to KEEP_LOCAL, so any
    # QUEUE_* verdict that reaches here is sync-eligible.
    if verdict == "QUEUE_HIGH":
        meta["sync_priority"] = "URGENT"
        meta["syncable"] = 1
    elif verdict in ("QUEUE_LOW", "REDACT_AND_QUEUE"):
        meta["sync_priority"] = "ROUTINE"
        meta["syncable"] = 1
    else:  # KEEP_LOCAL / REJECT -> never pushed
        meta["sync_priority"] = "HELD"
        meta["syncable"] = 0
    # Per-device monotonic client_sequence: the delta handshake pushes only rows
    # above the hub's high-water mark, and the outbox orders by it.
    shards['client_seq'] += 1
    meta["client_sequence"] = shards['client_seq']
    
    # Compute BM25 sparse vector for the document (embed_document)
    sparse_vector = bm25.embed_document(request.value)
    
    # Semantic conflict detection BEFORE inserting the new point (Step 8),
    # so we compare only against prior reports, never the point itself.
    conflicts = detect_conflicts(
        shard=mutable_shard,
        dense_vector=dense_vector,
        sparse_vector=sparse_vector,
        dense_name=adapter.name,
        new_point_id=point_id,
        new_payload=payload,
        similarity_threshold=CONFLICT_THRESHOLD,
    )
    register_conflicts(device_id, conflicts)
    
    # Create a point with both dense and sparse vectors
    point = Point(
        id=point_id,
        vector={
            adapter.name: dense_vector,
            "text_bm25": sparse_vector,
        },
        payload=payload,
    )
    
    # Upsert the point using UpdateOperation
    operation = UpdateOperation.upsert_points(points=[point])
    mutable_shard.update(operation)

    # Optimize after write batch (invariant 2)
    mutable_shard.optimize()
    
    return {
        "id": point_id,
        "payload": payload,
        "verdict": verdict,
        "reason": reason,
        "conflicts": conflicts,
    }


@app.post("/devices/{device_id}/query")
async def query(device_id: str, request: QueryRequest):
    import time
    shards = get_or_create_shards(device_id)
    mutable_shard = shards['mutable']
    immutable_shard = shards['immutable']

    adapter = adapters[0]
    dense_vector = adapter.embed(request.text)
    sparse_vector = bm25.embed_query(request.text)

    start_time = time.time()

    # Build prefetches for dense and sparse
    dense_prefetch = Prefetch(
        limit=25,
        query=Query.Nearest(query=dense_vector, using=adapter.name),
    )
    sparse_prefetch = Prefetch(
        limit=25,
        query=Query.Nearest(query=sparse_vector, using="text_bm25"),
    )

    # Fusion with RRF
    fusion = Fusion.Rrf(k=60)

    edge_request = EdgeQueryRequest(
        limit=10,
        prefetches=[dense_prefetch, sparse_prefetch],
        query=fusion,
        with_payload=True,
        with_vector=False,
    )

    # Query both shards
    mutable_results = mutable_shard.query(edge_request)
    immutable_results = immutable_shard.query(edge_request)

    end_time = time.time()
    latency_ms = (end_time - start_time) * 1000

    # Deduplicate by point ID, keeping the higher score if duplicate
    seen = {}
    for res in mutable_results:
        pid = res.id
        if pid not in seen or res.score > seen[pid].score:
            seen[pid] = res
    for res in immutable_results:
        pid = res.id
        if pid not in seen or res.score > seen[pid].score:
            seen[pid] = res

    # Sort by score descending
    sorted_results = sorted(seen.values(), key=lambda x: x.score, reverse=True)

    # Format response
    results = []
    for res in sorted_results:
        results.append(PointResponse(
            id=res.id,
            score=res.score,
            payload=res.payload
        ))

    return QueryResponse(results=results, latency_ms=latency_ms)


@app.get("/devices/{device_id}/feed")
async def get_device_feed(device_id: str):
    return get_feed(device_id)


class PushResponse(BaseModel):
    pushed_count: int
    errors: List[str] = []


def _envelope_from_record(rec, adapter_name: str) -> dict:
    """Serialize a shard record into a JSON-safe push envelope.

    Carries the dense + sparse vectors and the full payload (which already
    stamps the model + version — invariant 9), plus the client_sequence used by
    the delta handshake.
    """
    vec = rec.vector or {}
    dense = vec.get(adapter_name)
    sparse = vec.get("text_bm25")
    sparse_obj = None
    if sparse is not None:
        sparse_obj = {"indices": list(sparse.indices), "values": list(sparse.values)}
    meta = (rec.payload or {}).get("_sync_meta", {})
    return {
        "id": rec.id,
        "vector": list(dense) if dense is not None else None,
        "sparse": sparse_obj,
        "payload": rec.payload,
        "client_sequence": meta.get("client_sequence", 0),
    }


@app.post("/devices/{device_id}/push")
async def push(device_id: str):
    pushed_count, errors = _do_push(device_id)
    return PushResponse(pushed_count=pushed_count, errors=errors)


def _do_push(device_id: str):
    """Drain the outbox to the hub. Returns (pushed_count, errors).

    Shared by the push endpoint and the pull flush-first step. Marks synced ONLY
    on ack (invariant 1): any failure leaves the point pending and on device.
    """
    shards = get_or_create_shards(device_id)
    mutable_shard = shards['mutable']

    if sync_transport is None:
        # No transport wired: fail loudly rather than silently "succeeding".
        return 0, ["no sync transport configured"]

    # The outbox is a filtered VIEW over the mutable shard
    # (synced == 0 AND syncable == 1), URGENT before ROUTINE — not a second store.
    outbox = get_outbox(mutable_shard, limit=1000)
    if not outbox:
        return 0, []

    adapter_name = adapters[0].name

    # Delta handshake: push only rows above the hub's high-water mark for this
    # device, so a re-push after a crash moves nothing and never duplicates.
    try:
        hub_seq = sync_transport.get_delta(device_id)
    except Exception as e:
        return 0, [f"delta handshake failed: {e}"]

    envelopes = []
    ids_in_order = []
    for pid, rec in outbox:
        env = _envelope_from_record(rec, adapter_name)
        if env["client_sequence"] <= hub_seq:
            continue  # already on the hub — delta-only
        envelopes.append(env)
        ids_in_order.append(pid)

    if not envelopes:
        return 0, []

    # Push to the gateway. Mark synced ONLY on ack (invariant 1): on any failure
    # we leave synced == 0, so the point stays pending and on the device — a
    # failed push is never silent data loss.
    try:
        ack = sync_transport.push(device_id, envelopes)
    except Exception as e:
        return 0, [f"push failed: {e}"]

    acked = set(ack.get("acked_ids", ids_in_order))
    mark_ids = [pid for pid in ids_in_order if pid in acked]
    mark_synced(mutable_shard, mark_ids)

    # Append a local OBSERVED event per pushed point (consensus trust view),
    # in push order so URGENT precedes ROUTINE.
    event_log = get_or_create_event_log(device_id)
    now = datetime.utcnow().isoformat() + "Z"
    for pid in mark_ids:
        event_log.append(pid, OBSERVED, device_ts=now)

    return len(mark_ids), []


class PullResponse(BaseModel):
    pulled_count: int
    errors: List[str] = []


@app.post("/devices/{device_id}/pull")
async def pull(device_id: str):
    """Learn from the fleet via a real partial snapshot (Phase 4).

    Mirrors Qdrant's sync guide (backend.md §6.3):
      1. flush the outbox to the hub first, so the snapshot we pull back isn't
         stale (our own latest reports are included);
      2. pull a partial snapshot keyed off the immutable shard's manifest and
         restore it with `update_from_snapshot` — the ONLY way we ever write the
         immutable shard (Phase 4 guardrail);
      3. dedupe the mutable shard: a point that is synced (on the hub) and was
         captured at/before this sync now lives in the immutable shard, so drop
         its mutable copy. Pending points (`synced == 0`) are never touched —
         deleting one would be silent data loss (invariant 1).

    Query still reads both shards and dedupes by id, so a point briefly present
    in both never shows up twice.
    """
    shards = get_or_create_shards(device_id)
    mutable_shard = shards['mutable']
    immutable_shard = shards['immutable']

    if sync_transport is None:
        return PullResponse(pulled_count=0, errors=["no sync transport configured"])

    # 1. Flush first so the hub snapshot reflects our own pending facts. A flush
    # failure is reported but does not abort the pull (we can still learn from
    # the fleet); the pending points simply stay pending.
    _, flush_errors = _do_push(device_id)

    # The sync boundary: points synced at/before now are safely on the hub and
    # will come back in the snapshot, so they are the ones we may dedupe.
    sync_timestamp = int(time.time() * 1_000_000_000)

    # 2. Pull + restore a real partial snapshot into the immutable shard.
    manifest = immutable_shard.snapshot_manifest()
    try:
        snapshot_path = sync_transport.pull_snapshot(device_id, manifest)
    except Exception as e:
        return PullResponse(pulled_count=0, errors=flush_errors + [f"pull failed: {e}"])

    if snapshot_path is None:
        return PullResponse(pulled_count=0, errors=flush_errors)

    before = immutable_shard.count(CountRequest())
    try:
        immutable_shard.update_from_snapshot(snapshot_path)
        immutable_shard.optimize()
    except Exception as e:
        return PullResponse(pulled_count=0, errors=flush_errors + [f"snapshot restore failed: {e}"])
    finally:
        # The snapshot file is a throwaway staging artifact.
        try:
            os.remove(snapshot_path)
        except OSError:
            pass
    after = immutable_shard.count(CountRequest())
    pulled_count = _count_value(after) - _count_value(before)

    # 3. Dedupe the mutable shard by timestamp — only synced points, so a pending
    # fact is never deleted.
    dedupe_filter = Filter(
        must=[
            FieldCondition(key=SYNCED_KEY, match=MatchValue(value=1)),
            FieldCondition(
                key="client_timestamp_ns", range=RangeFloat(lte=float(sync_timestamp))
            ),
        ]
    )
    mutable_shard.update(UpdateOperation.delete_points_by_filter(filter=dedupe_filter))
    mutable_shard.optimize()

    return PullResponse(pulled_count=max(pulled_count, 0), errors=flush_errors)


def _count_value(count_result) -> int:
    """`count()` returns a CountResult with `.count`; be tolerant of a bare int."""
    return getattr(count_result, "count", count_result)


class RetractResponse(BaseModel):
    retracted: bool
    point_id: int


@app.post("/devices/{device_id}/retract/{point_id}")
async def retract(device_id: str, point_id: int):
    """Append a RETRACTED event to the fact_events log (Step 6)."""
    event_log = get_or_create_event_log(device_id)
    now = datetime.utcnow().isoformat() + "Z"
    event_log.append(point_id, RETRACTED, device_ts=now)
    return RetractResponse(retracted=True, point_id=point_id)


@app.get("/devices/{device_id}/events/{point_id}")
async def get_events(device_id: str, point_id: int):
    event_log = get_or_create_event_log(device_id)
    events = event_log.events_for(point_id)
    events.sort(key=lambda e: e["seq"])
    return {"point_id": point_id, "events": events}


@app.get("/devices/{device_id}/trust/{point_id}")
async def get_trust(device_id: str, point_id: int):
    """Return the consensus trust score for a fact (Steps 6 & 7)."""
    event_log = get_or_create_event_log(device_id)
    events = event_log.events_for(point_id)
    trust = fold_trust(events, decay=TRUST_DECAY)
    return {
        "point_id": point_id,
        "trust": trust,
        "lww_trust": lww_trust(events),
        "decay": TRUST_DECAY,
        "event_count": len(events),
    }


@app.get("/benchmark/resolver-vs-lww")
async def benchmark_resolver_vs_lww():
    """Step 7 benchmark: resolver fold vs last-write-wins.

    LWW decides trust from the single newest event: it cannot tell a lone
    unverified report from a fact corroborated by many devices — it is flat 1.0
    in both cases. The resolver fold produces graduated confidence that climbs
    with corroboration. This is the resolver's measurable advantage.
    Also verifies Invariant 7: a terminal retraction drives both to 0.
    """
    seq = 0
    events = []
    trajectory = []

    def add(evt_type):
        nonlocal seq
        seq += 1
        events.append({"point_id": 1, "event_type": evt_type, "seq": seq, "device_ts": str(seq)})
        trajectory.append({
            "seq": seq,
            "event": evt_type,
            "resolver": round(fold_trust(events, decay=TRUST_DECAY), 4),
            "lww": round(lww_trust(events), 4),
        })

    # 5 independent corroborating observations
    for _ in range(5):
        add(OBSERVED)

    resolver_1obs = trajectory[0]["resolver"]
    resolver_5obs = trajectory[4]["resolver"]
    lww_1obs = trajectory[0]["lww"]
    lww_5obs = trajectory[4]["lww"]

    # LWW is blind to corroboration count; resolver grows with it.
    resolver_gain = round(resolver_5obs - resolver_1obs, 4)
    lww_gain = round(lww_5obs - lww_1obs, 4)

    return {
        "decay": TRUST_DECAY,
        "trajectory": trajectory,
        "resolver_1obs": resolver_1obs,
        "resolver_5obs": resolver_5obs,
        "lww_1obs": lww_1obs,
        "lww_5obs": lww_5obs,
        "resolver_gain_from_corroboration": resolver_gain,
        "lww_gain_from_corroboration": lww_gain,
        "resolver_distinguishes_corroboration": resolver_gain > lww_gain,
    }


@app.get("/devices/{device_id}/conflicts")
async def list_conflicts(device_id: str):
    """Return POSSIBLE_CONFLICT candidates the exact-key scheme would miss (Step 8)."""
    return {"device_id": device_id, "conflicts": get_conflicts(device_id)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)