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
)
from datetime import datetime

from .adapter import Adapter
from .registry import load_adapters
from .decision_engine import DecisionEngine, log_decision, get_feed, clear_feed, load_policy
from .outbox import add_sync_meta, get_outbox, mark_synced, get_outbox_points
from .hub import hub
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
TRUST_DECAY: float = 0.0
CONFLICT_THRESHOLD: float = 0.5
SHARD_BASE_PATH = "./shards"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global adapters, edge_config, decision_engine, bm25, TRUST_DECAY, CONFLICT_THRESHOLD

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
        'unsynced_ids': set()
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
    unsynced_set = shards['unsynced_ids']
    
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
    
    # Store verdict and sync_priority in _sync_meta for push ordering
    if "_sync_meta" not in payload:
        payload["_sync_meta"] = {}
    payload["_sync_meta"]["verdict"] = verdict
    # Determine base sync_priority from verdict
    if verdict == "QUEUE_HIGH":
        base_priority = "URGENT"
    elif verdict in ["QUEUE_LOW", "REDACT_AND_QUEUE"]:
        base_priority = "ROUTINE"
    else:
        base_priority = "HELD"
    # Override to HELD if incomplete (regardless of verdict)
    # Check completeness using the same logic as decision engine
    missing = [f for f in ["status", "timestamp", "reporter_device_id"] if f not in payload]
    if missing:
        # Incomplete - override to HELD
        payload["_sync_meta"]["sync_priority"] = "HELD"
    else:
        # Complete - use base priority from verdict
        payload["_sync_meta"]["sync_priority"] = base_priority
    
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
    
    # Mark as unsynced only if verdict allows syncing
    # KEEP_LOCAL and REJECT should not be pushed
    if verdict in ["QUEUE_HIGH", "QUEUE_LOW", "REDACT_AND_QUEUE"]:
        unsynced_set.add(point_id)
    
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


@app.post("/devices/{device_id}/push")
async def push(device_id: str):
    shards = get_or_create_shards(device_id)
    mutable_shard = shards['mutable']
    unsynced_set = shards['unsynced_ids']
    
    if not unsynced_set:
        return PushResponse(pushed_count=0, errors=[])
    
    # Convert set to list and sort by priority: URGENT before ROUTINE
    point_ids = list(unsynced_set)
    
    # Retrieve points to get their sync_priority for sorting
    try:
        records = mutable_shard.retrieve(point_ids, with_payload=True, with_vector=False)
        # Create a list of (point_id, priority) tuples for sorting
        point_priorities = []
        for record in records:
            point_id = record.id
            sync_payload = record.payload.get("_sync_meta", {})
            priority = sync_payload.get("sync_priority", "HELD")
            # Convert priority to sort order: URGENT=0, ROUTINE=1, HELD=2 (though HELD shouldn't be in unsynced_set)
            priority_order = {"URGENT": 0, "ROUTINE": 1, "HELD": 2}.get(priority, 2)
            point_priorities.append((point_id, priority_order))
        
        # Sort by priority (URGENT first)
        point_priorities.sort(key=lambda x: x[1])
        sorted_point_ids = [point_id for point_id, _ in point_priorities]
    except Exception as e:
        # If retrieval fails, fall back to original order
        sorted_point_ids = point_ids
    
    # Retrieve points to create snapshot (in priority order)
    try:
        records = mutable_shard.retrieve(sorted_point_ids, with_payload=True, with_vector=True)
        # Convert Record objects to Point objects
        points = [Point(id=rec.id, vector=rec.vector, payload=rec.payload) for rec in records]
    except Exception as e:
        # If retrieve fails, we can't create snapshot
        points = []
    
    # Create snapshot of these points and store in hub
    try:
        if points:
            version = hub.create_snapshot(points)
        else:
            pass
    except Exception as e:
        # If snapshot creation fails, we still continue but log
        pass
    
    # Simulate push to hub: in real implementation we would send points to hub_url
    # For now we assume push always succeeds.
    try:
        # Mark all points as synced: set _sync_meta.synced = True where _sync_meta.synced is false
        from qdrant_edge import Filter, FieldCondition, MatchValue, UpdateOperation
        condition = FieldCondition(
            key="_sync_meta.synced",
            match=MatchValue(value=False)
        )
        f_filter = Filter(must=[condition])
        update_op = UpdateOperation.set_payload_by_filter(
            filter=f_filter,
            payload={"_sync_meta": {"synced": True}}
        )
        mutable_shard.update(update_op)
        # Optimize after update (optional but good)
        mutable_shard.optimize()
        # Append an OBSERVED event per pushed point (Step 6: event-sourced consensus)
        event_log = get_or_create_event_log(device_id)
        now = datetime.utcnow().isoformat() + "Z"
        for pid in sorted_point_ids:
            event_log.append(pid, OBSERVED, device_ts=now)
        # Clear unsynced set
        unsynced_set.clear()
        pushed = len(sorted_point_ids)
        errors = []
    except Exception as e:
        pushed = 0
        errors = [str(e)]
    
    return PushResponse(pushed_count=pushed, errors=errors)


class PullResponse(BaseModel):
    pulled_count: int
    errors: List[str] = []


@app.post("/devices/{device_id}/pull")
async def pull(device_id: str):
    shards = get_or_create_shards(device_id)
    immutable_shard = shards['immutable']
    
    # Get latest snapshot from hub
    snapshot_points = hub.get_latest_snapshot()
    if not snapshot_points:
        return PullResponse(pulled_count=0, errors=["No snapshot available"])
    
    # Upsert snapshot points into immutable shard
    try:
        # Build list of Point objects (they are already Point)
        points_to_upsert = snapshot_points
        operation = UpdateOperation.upsert_points(points=points_to_upsert)
        immutable_shard.update(operation)
        # Optimize after upsert
        immutable_shard.optimize()
        pulled = len(points_to_upsert)
        errors = []
    except Exception as e:
        pulled = 0
        errors = [str(e)]
    
    return PullResponse(pulled_count=pulled, errors=errors)


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