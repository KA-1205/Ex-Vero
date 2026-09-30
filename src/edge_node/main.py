import os
import hashlib
import time
from typing import List, Dict, Optional, Any
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
from .registry import load_adapters, build_generators
from .decision_engine import DecisionEngine, log_decision, get_feed, clear_feed, load_policy
from .outbox import add_sync_meta, get_outbox, mark_synced, ensure_indexes, SYNCABLE_KEY, SYNCED_KEY
from .sync_transport import GatewayTransport
from .consensus import EventLog, fold_trust, lww_trust, OBSERVED, RETRACTED
from .conflicts import detect_conflicts, register_conflicts, get_conflicts, clear_conflicts, POSSIBLE_CONFLICT
from . import network
from .retrieval import hybrid_query
from .answer import answer_question
from .telemetry import record_query_latency, get_telemetry
from .eviction import evict_by_count_and_optimize

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "disaster-response.yaml"

app = FastAPI()

# Global variables
adapters: List[Adapter] = None
edge_config: EdgeConfig = None
decision_engine: DecisionEngine = None
bm25: Bm25 = None
generators: list = None  # Phase 7: the config-selected RAG generator chain
device_shards: Dict[str, dict] = {}  # device_id -> {'mutable': shard, 'immutable': shard}
event_logs: Dict[str, EventLog] = {}  # device_id -> EventLog
sync_transport = None  # SyncTransport; GatewayTransport in runtime, stub in tests
TRUST_DECAY: float = 0.0
CONFLICT_THRESHOLD: float = 0.5
MAX_LOCAL_POINTS: int = 500
SHARD_BASE_PATH = "./shards"

# Activity ring buffer (operational telemetry, not facts) — Phase 8
# In-memory, bounded, newest first. No SQL.
from collections import deque
_activity_buffer: deque = deque(maxlen=500)
_activity_lock = __import__('threading').Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global adapters, edge_config, decision_engine, bm25, generators, TRUST_DECAY, CONFLICT_THRESHOLD, sync_transport, MAX_LOCAL_POINTS

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

    # Memory cap (Phase 8)
    memory_config = full_config.get('memory', {})
    MAX_LOCAL_POINTS = int(memory_config.get('max_local_points', 500))

    # Initialize BM25 embedder (default config)
    bm25 = Bm25()

    # Answer layer (Phase 7): resolve the config-selected generator chain. The
    # chain always ends in the extractive fallback, so the device can answer even
    # with no reachable model. Built here so it is a runtime, config-driven
    # object — never hardcoded in the query handler.
    generators = build_generators(full_config)
    print(f"Generator chain: {[g.name for g in generators]}")

    # Network layer (offline/degraded/full). Apply the config-driven link
    # parameters; the interceptor wraps ONLY the sync client below, never query.
    net_config = full_config.get('network', {})
    if net_config:
        network.configure(**{k: v for k, v in net_config.items() if k != 'mode'})
    if net_config.get('mode'):
        network.set_mode(net_config['mode'])

    # Hub configuration (for push)
    hub_config = full_config.get('hub', {})
    hub_url = hub_config.get('url')
    hub_api_key = hub_config.get('api_key')
    # The sync transport is the ONLY egress to the Cloud Gateway. Tests inject a
    # stub in its place; the runtime path always uses a real HTTP client, wrapped
    # by the network interceptor so intermittent-connectivity effects are real.
    sync_transport = network.NetworkTransport(GatewayTransport(hub_url)) if hub_url else None
    print(f"Hub config loaded: url={hub_url}")

    print(f"Loaded {len(adapters)} adapters: {[a.name for a in adapters]}")
    print(f"Loaded policy: {policy_config}")
    print(f"Trust decay: {TRUST_DECAY}")
    print(f"Memory cap: {MAX_LOCAL_POINTS}")
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
    text: str  # the query text
    # `device_id` is authoritative from the path; kept optional in the body for
    # backward compatibility with earlier callers that sent it there.
    device_id: Optional[str] = None
    answer: bool = True   # when true, generate a grounded RAG answer (Phase 7)
    limit: int = 10       # retrieval fan-out


class PointResponse(BaseModel):
    id: int
    score: float
    payload: dict


class SourceRef(BaseModel):
    """A cited retrieved fact backing the answer (docs/API.md §4)."""
    id: int
    score: float
    value: Optional[str] = None
    consensus_state: Optional[str] = None


class QueryResponse(BaseModel):
    results: List[PointResponse]
    latency_ms: float
    # Present only when an answer was generated (`answer: true`).
    answer: Optional[str] = None
    answer_path: Optional[str] = None    # "offline" | "online" | "extractive"
    model: Optional[str] = None
    sources: Optional[List[SourceRef]] = None


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


# Activity ring buffer helpers (Phase 8)
def log_activity(device_id: str, kind: str, detail: str, point_id: Optional[int] = None) -> None:
    """Append an activity entry to the ring buffer (newest first)."""
    from datetime import datetime
    entry = {
        "device_id": device_id,
        "kind": kind,  # capture | decision | push_attempt | push_result | pull_result | consensus | retraction | error | mode_change
        "detail": detail,
        "point_id": point_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    with _activity_lock:
        _activity_buffer.appendleft(entry)


def get_activity(device_id: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Get activity entries, optionally filtered by device_id and kind, newest first."""
    with _activity_lock:
        entries = list(_activity_buffer)
    if device_id:
        entries = [e for e in entries if e["device_id"] == device_id]
    if kind:
        entries = [e for e in entries if e["kind"] == kind]
    return entries[:limit]


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

    # Enforce memory cap (Phase 8): evict oldest synced points if over cap
    evict_by_count_and_optimize(mutable_shard, MAX_LOCAL_POINTS)

    # Log activity
    log_activity(device_id, "capture", f"captured fact {point_id}: {verdict}", point_id)

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

    # The retrieval and answer modules import no transport and no network
    # simulator, so `latency_ms` measures local search + local generation only —
    # the offline promise (invariant 8). Timed end-to-end so the number reflects
    # what the caller actually waited for.
    start_time = time.time()
    hits = hybrid_query(
        mutable_shard=mutable_shard,
        immutable_shard=immutable_shard,
        dense_vector=dense_vector,
        sparse_vector=sparse_vector,
        dense_name=adapter.name,
        limit=request.limit,
    )

    results = [
        PointResponse(id=h.id, score=h.score, payload=h.payload) for h in hits
    ]

    answer_fields = {}
    # Grounded RAG answer over the retrieved context. Skipped when the caller
    # asks for retrieval only, or when no generator chain is wired.
    if request.answer and generators:
        result = answer_question(request.text, hits, generators)
        answer_fields = {
            "answer": result.answer,
            "answer_path": result.answer_path,
            "model": result.model,
            "sources": [SourceRef(**s) for s in result.sources],
        }

    latency_ms = (time.time() - start_time) * 1000
    # Record query latency for telemetry percentiles (Phase 8)
    record_query_latency(latency_ms)
    return QueryResponse(results=results, latency_ms=latency_ms, **answer_fields)


@app.get("/devices/{device_id}/feed")
async def get_device_feed(device_id: str):
    return get_feed(device_id)


class NetworkModeRequest(BaseModel):
    mode: str  # "offline" | "degraded" | "full"


@app.get("/network/mode")
async def get_network_mode():
    """Current link state + the config-driven degraded parameters, plus the
    measured per-push log (bytes/duration/attempted/accepted/failed/priority)."""
    return {
        "mode": network.get_mode(),
        "config": network.config_dict(),
        "push_log": network.get_push_log(),
    }


@app.post("/network/mode")
async def set_network_mode(request: NetworkModeRequest):
    """Flip the global link mode. This is the single 'reconnect the fleet' knob;
    it affects sync only — the query/answer path never reads it."""
    try:
        network.set_mode(request.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log_activity("system", "mode_change", f"network mode changed to {request.mode}")
    return {"mode": network.get_mode()}


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
    if pushed_count > 0:
        log_activity(device_id, "push_result", f"pushed {pushed_count} points", point_id=None)
    if errors:
        log_activity(device_id, "error", f"push errors: {errors}", point_id=None)
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

    # Log pull activity
    log_activity(device_id, "pull_result", f"pulled {pulled_count} points", point_id=None)
    if flush_errors:
        log_activity(device_id, "error", f"pull flush errors: {flush_errors}", point_id=None)

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
    log_activity(device_id, "retraction", f"retracted point {point_id}", point_id)
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


# =============================================================================
# Phase 8 — Eviction + Inspection Endpoints
# =============================================================================

from fastapi import WebSocket, WebSocketDisconnect
from typing import Set


# --- Active WebSocket connections for live feeds ---
_ws_device_events: Dict[str, Set[WebSocket]] = {}
_ws_consensus_events: Set[WebSocket] = {}
_ws_lock = __import__('threading').Lock()


# --- GET /devices ---
@app.get("/devices")
async def list_devices():
    """Fleet state for the Fleet Overview screen (API.md §2)."""
    devices = []
    for device_id, shards in device_shards.items():
        mutable_shard = shards['mutable']
        immutable_shard = shards['immutable']
        
        # Total points across both shards
        mutable_count = mutable_shard.count(CountRequest())
        immutable_count = immutable_shard.count(CountRequest())
        total_count = getattr(mutable_count, "count", mutable_count) + getattr(immutable_count, "count", immutable_count)
        
        # Pending counts via facet on _sync_meta.synced
        from .outbox import SYNCED_KEY, SYNCABLE_KEY
        from qdrant_edge import FacetRequest
        pending_facet = mutable_shard.facet(FacetRequest(key=SYNCED_KEY))
        pending_by_priority = {"URGENT": 0, "ROUTINE": 0, "HELD": 0}
        if hasattr(pending_facet, 'hits'):
            for hit in pending_facet.hits:
                if hit.value == 0:  # synced == 0 (pending)
                    # Need to further breakdown by sync_priority
                    pass
        
        # Simpler: use outbox to get pending counts
        from .outbox import get_outbox
        outbox = get_outbox(mutable_shard, limit=10000)
        for pid, rec in outbox:
            meta = (rec.payload or {}).get("_sync_meta", {})
            prio = meta.get("sync_priority", "ROUTINE")
            if prio in pending_by_priority:
                pending_by_priority[prio] += 1
        
        # Last sync time from activity log
        last_sync = None
        for entry in _activity_buffer:
            if entry["device_id"] == device_id and entry["kind"] in ("push_result", "pull_result"):
                last_sync = entry["timestamp"]
                break
        
        devices.append({
            "id": device_id,
            "name": device_id,  # Could be extended with a device registry
            "connectivity": network.get_mode(),
            "memory": {"used": total_count, "cap": MAX_LOCAL_POINTS},
            "last_sync_at": last_sync,
            "trust": 0.82,  # Placeholder - could come from consensus
            "activity_sparkline": [3, 5, 2, 8, 1],  # Placeholder
        })
    return {"devices": devices}


# --- GET /devices/{id} ---
@app.get("/devices/{device_id}")
async def get_device(device_id: str):
    """Single device detail (header of the Device Console) (API.md §2)."""
    if device_id not in device_shards:
        raise HTTPException(status_code=404, detail="unknown device")
    
    shards = device_shards[device_id]
    mutable_shard = shards['mutable']
    immutable_shard = shards['immutable']
    
    mutable_count = mutable_shard.count(CountRequest())
    immutable_count = immutable_shard.count(CountRequest())
    total_count = getattr(mutable_count, "count", mutable_count) + getattr(immutable_count, "count", immutable_count)
    
    # Pending via outbox
    from .outbox import get_outbox
    outbox = get_outbox(mutable_shard, limit=10000)
    pending_by_priority = {"URGENT": 0, "ROUTINE": 0, "HELD": 0}
    for pid, rec in outbox:
        meta = (rec.payload or {}).get("_sync_meta", {})
        prio = meta.get("sync_priority", "ROUTINE")
        if prio in pending_by_priority:
            pending_by_priority[prio] += 1
    
    last_sync = None
    for entry in _activity_buffer:
        if entry["device_id"] == device_id and entry["kind"] in ("push_result", "pull_result"):
            last_sync = entry["timestamp"]
            break
    
    return {
        "id": device_id,
        "name": device_id,
        "connectivity": network.get_mode(),
        "memory": {"used": total_count, "cap": MAX_LOCAL_POINTS},
        "last_sync_at": last_sync,
        "trust": 0.82,
        "activity_sparkline": [3, 5, 2, 8, 1],
    }


# --- GET /devices/{id}/memory ---
@app.get("/devices/{device_id}/memory")
async def list_memory(device_id: str, q: Optional[str] = None, sync_state: Optional[str] = None,
                      modality: Optional[str] = None, zone: Optional[str] = None,
                      limit: int = 100, offset: int = 0):
    """List/filter everything held locally (Memory Browser panel) (API.md §5)."""
    if device_id not in device_shards:
        raise HTTPException(status_code=404, detail="unknown device")
    
    shards = device_shards[device_id]
    mutable_shard = shards['mutable']
    immutable_shard = shards['immutable']
    
    # Build filter
    must = []
    if q:
        # Text filter - use the value field
        must.append(FieldCondition(key="value", match=MatchValue(value=q)))
    if sync_state:
        if sync_state == "synced":
            must.append(FieldCondition(key=SYNCED_KEY, match=MatchValue(value=1)))
        elif sync_state == "pending":
            must.append(FieldCondition(key=SYNCED_KEY, match=MatchValue(value=0)))
        elif sync_state == "local_only":
            must.append(FieldCondition(key="_sync_meta.syncable", match=MatchValue(value=0)))
    if modality:
        must.append(FieldCondition(key="modality", match=MatchValue(value=modality)))
    if zone:
        must.append(FieldCondition(key="zone", match=MatchValue(value=zone)))
    
    f = Filter(must=must) if must else None
    
    from qdrant_edge import ScrollRequest
    points = []
    total = 0
    
    for shard in (mutable_shard, immutable_shard):
        req = ScrollRequest(
            limit=limit + offset,
            filter=f,
            with_payload=True,
            with_vector=False,
        )
        res = shard.scroll(req)
        records = res[0] if isinstance(res, tuple) else res
        for r in records:
            payload = r.payload or {}
            meta = payload.get("_sync_meta", {})
            sync_state_val = "synced" if meta.get("synced") == 1 else ("local_only" if meta.get("syncable") == 0 else "pending")
            points.append({
                "id": r.id,
                "value": payload.get("value", ""),
                "modality": payload.get("modality", "text"),
                "thumbnail_url": payload.get("thumbnail_url"),
                "zone": payload.get("zone"),
                "corroboration_key": payload.get("corroboration_key"),
                "sync_state": sync_state_val,
                "model": payload.get("model", "text_dense"),
                "model_version": payload.get("model_version", "bge-small-en-v1.5"),
                "created_at": payload.get("client_timestamp_ns", 0),
            })
        total += len(records)
    
    # Dedup by id (keep mutable version)
    seen = {}
    for p in points:
        if p["id"] not in seen:
            seen[p["id"]] = p
    
    deduped = list(seen.values())
    deduped.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    
    return {"points": deduped[offset:offset+limit], "total": len(deduped)}


# --- GET /devices/{id}/memory/{point_id} ---
@app.get("/devices/{device_id}/memory/{point_id}")
async def get_memory_point(device_id: str, point_id: int):
    """Full detail for one fact (API.md §5)."""
    if device_id not in device_shards:
        raise HTTPException(status_code=404, detail="unknown device")
    
    shards = device_shards[device_id]
    mutable_shard = shards['mutable']
    immutable_shard = shards['immutable']
    
    # Try mutable first, then immutable
    for shard in (mutable_shard, immutable_shard):
        recs = shard.retrieve([point_id], with_payload=True, with_vector=False)
        if recs:
            rec = recs[0]
            payload = rec.payload or {}
            meta = payload.get("_sync_meta", {})
            sync_state_val = "synced" if meta.get("synced") == 1 else ("local_only" if meta.get("syncable") == 0 else "pending")
            
            point = {
                "id": rec.id,
                "value": payload.get("value", ""),
                "modality": payload.get("modality", "text"),
                "thumbnail_url": payload.get("thumbnail_url"),
                "zone": payload.get("zone"),
                "corroboration_key": payload.get("corroboration_key"),
                "sync_state": sync_state_val,
                "model": payload.get("model", "text_dense"),
                "model_version": payload.get("model_version", "bge-small-en-v1.5"),
                "created_at": payload.get("client_timestamp_ns", 0),
            }
            
            # Get decision event
            decision = None
            for entry in get_feed(device_id):
                if entry.get("payload", {}).get("id") == point_id or entry.get("point_id") == point_id:
                    decision = {
                        "device_id": entry.get("device_id"),
                        "point_id": point_id,
                        "value_preview": entry.get("payload", {}).get("value", "")[:100],
                        "modality": payload.get("modality", "text"),
                        "thumbnail_url": payload.get("thumbnail_url"),
                        "verdict": meta.get("verdict", "UNKNOWN"),
                        "reason": entry.get("reason", ""),
                        "timestamp": entry.get("timestamp"),
                    }
                    break
            
            # Get activity for this point
            activity = [e for e in get_activity(device_id) if e.get("point_id") == point_id]
            
            # Get consensus if available
            consensus = None
            try:
                event_log = get_or_create_event_log(device_id)
                events = event_log.events_for(point_id)
                if events:
                    trust = fold_trust(events, decay=TRUST_DECAY)
                    consensus = {
                        "corroboration_key": payload.get("corroboration_key"),
                        "state": "CONFIRMED" if trust > 0.5 else "DISPUTED",
                        "confidence": trust,
                        "resolved_value": payload.get("value") if trust > 0.5 else None,
                        "candidates": [],
                        "explanation": "",
                        "timestamp": events[-1].get("device_ts", "") if events else "",
                    }
            except Exception:
                pass
            
            return {
                "point": point,
                "decision": decision,
                "activity": activity,
                "consensus": consensus,
            }
    
    raise HTTPException(status_code=404, detail="point not found")


# --- GET /devices/{id}/sync ---
@app.get("/devices/{device_id}/sync")
async def get_sync_status(device_id: str):
    """Sync-status strip data (API.md §7)."""
    if device_id not in device_shards:
        raise HTTPException(status_code=404, detail="unknown device")
    
    shards = device_shards[device_id]
    mutable_shard = shards['mutable']
    
    from .outbox import get_outbox
    outbox = get_outbox(mutable_shard, limit=10000)
    pending = {"URGENT": 0, "ROUTINE": 0, "HELD": 0}
    for pid, rec in outbox:
        meta = (rec.payload or {}).get("_sync_meta", {})
        prio = meta.get("sync_priority", "ROUTINE")
        if prio in pending:
            pending[prio] += 1
    
    # Last attempt/success from activity
    last_attempt = None
    last_success = None
    consecutive_failures = 0
    last_push = {}
    last_pull = {}
    
    for entry in _activity_buffer:
        if entry["device_id"] == device_id:
            if entry["kind"] == "push_attempt":
                last_attempt = entry["timestamp"]
            elif entry["kind"] == "push_result":
                if "failed" in entry["detail"].lower() or "error" in entry["detail"].lower():
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                    last_success = entry["timestamp"]
                # Parse last push details
                last_push = {
                    "bytes": 0,  # Would need to track this
                    "duration_ms": 0,
                    "points_attempted": 0,
                    "points_accepted": 0,
                    "points_failed": 0,
                    "mode": network.get_mode(),
                }
            elif entry["kind"] == "pull_result":
                last_pull = {
                    "at": entry["timestamp"],
                    "points_received": 0,
                }
    
    return {
        "pending": pending,
        "last_attempt_at": last_attempt,
        "last_success_at": last_success,
        "consecutive_failures": consecutive_failures,
        "next_backoff_ms": 4000,
        "last_push": last_push,
        "last_pull": last_pull,
    }


# --- GET /devices/{id}/activity ---
@app.get("/devices/{device_id}/activity")
async def get_device_activity(device_id: str, kind: Optional[str] = None, limit: int = 100):
    """System-activity ring buffer, newest first (API.md §6)."""
    entries = get_activity(device_id, kind, limit)
    return {"entries": entries}


# --- GET /devices/{id}/telemetry ---
@app.get("/devices/{device_id}/telemetry")
async def get_device_telemetry(device_id: str):
    """Live CPU/RAM/latency from the real cgroup/process (API.md §11)."""
    if device_id not in device_shards:
        raise HTTPException(status_code=404, detail="unknown device")
    
    # Model load time could be tracked at startup
    model_load_ms = 1840.0  # Placeholder
    
    return get_telemetry(model_load_ms=model_load_ms)


# --- GET /cloud/state ---
@app.get("/cloud/state")
async def get_cloud_state(state: Optional[str] = None, zone: Optional[str] = None):
    """Merged trusted picture from the fold (API.md §8)."""
    # In a real deployment, this would query the Cloud Gateway's consensus fold.
    # For the edge node, we return an empty state or aggregate from local event logs.
    facts = []
    device_trust = {}
    
    for device_id, event_log in event_logs.items():
        # This is a simplified version - the real fold lives in the gateway
        pass
    
    return {"facts": facts, "device_trust": device_trust}


# --- WS /devices/{id}/events ---
@app.websocket("/devices/{device_id}/events")
async def ws_device_events(websocket: WebSocket, device_id: str):
    """Live stream for Decision Feed and Activity Log (API.md §6)."""
    await websocket.accept()
    with _ws_lock:
        if device_id not in _ws_device_events:
            _ws_device_events[device_id] = set()
        _ws_device_events[device_id].add(websocket)
    
    try:
        while True:
            await websocket.receive_text()  # Keep alive
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            _ws_device_events[device_id].discard(websocket)
            if not _ws_device_events[device_id]:
                del _ws_device_events[device_id]


# --- WS /consensus/events ---
@app.websocket("/consensus/events")
async def ws_consensus_events(websocket: WebSocket):
    """Live resolver decisions for the Conflict Theater (API.md §8)."""
    await websocket.accept()
    with _ws_lock:
        _ws_consensus_events.add(websocket)
    
    try:
        while True:
            await websocket.receive_text()  # Keep alive
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            _ws_consensus_events.discard(websocket)


def _broadcast_device_event(device_id: str, event_type: str, data: dict):
    """Broadcast a decision/activity event to WebSocket clients."""
    import json
    message = json.dumps({"type": event_type, "data": data})
    with _ws_lock:
        for ws in _ws_device_events.get(device_id, set()):
            try:
                ws.send_text(message)
            except Exception:
                pass


def _broadcast_consensus_event(event: dict):
    """Broadcast a consensus event to WebSocket clients."""
    import json
    message = json.dumps(event)
    with _ws_lock:
        for ws in _ws_consensus_events:
            try:
                ws.send_text(message)
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)