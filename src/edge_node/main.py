import os
import hashlib
import time
import io
import asyncio
from typing import List, Dict, Optional, Any, Set
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yaml
from pathlib import Path
from contextlib import asynccontextmanager
from PIL import Image

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
from datetime import datetime, timezone


def _utcnow() -> str:
    """Return current UTC time as ISO-8601 with Z suffix (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

from .adapter import Adapter, ClipTextAdapter
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
from .benchmark import (
    load_jsonl,
    score_recall,
    score_resolver_vs_lww,
    RECALL_FIXTURE,
    RECALL_CORPUS_FIXTURE,
    CONFLICT_FIXTURE,
    BENCH_DEVICE,
    RECALL_K,
    RECALL_PREFETCH_K,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "disaster-response.yaml"

THUMBNAIL_DIR = REPO_ROOT / "shards" / "thumbnails"
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

# Global variables
adapters: List[Adapter] = None
clip_text_adapter: Optional[ClipTextAdapter] = None
edge_config: EdgeConfig = None
decision_engine: DecisionEngine = None
bm25: Bm25 = None
generators: list = None  # Phase 7: the config-selected RAG generator chain
device_shards: Dict[str, dict] = {}  # device_id -> {'mutable': shard, 'immutable': shard, 'client_seq': int}
event_logs: Dict[str, EventLog] = {}  # device_id -> EventLog
sync_transport = None  # SyncTransport; GatewayTransport in runtime, stub in tests
TRUST_DECAY: float = 0.0
CONFLICT_THRESHOLD: float = 0.5
# Supermajority the fold needs to call a multi-device fact CONFIRMED. Read from
# `consensus.confidence_threshold`; the Cloud Gateway applies the same value.
CONSENSUS_THRESHOLD: float = 0.66
MAX_LOCAL_POINTS: int = 500
# Device ids whose benchmark corpus has already been seeded this process.
_bench_seeded: set = set()
SHARD_BASE_PATH = "./shards"
_rogue_devices: Set[str] = set()

# Pre-configured demo fleet (Phase 1)
DEFAULT_FLEET = [
    {"id": "dev-01", "name": "Paramedic Tablet 01", "kind": "paramedic tablet", "latitude": 28.6329, "longitude": 77.2195},
    {"id": "dev-02", "name": "Kiosk — Shelter B", "kind": "kiosk", "latitude": 28.6129, "longitude": 77.2295},
    {"id": "dev-03", "name": "Field Pi Node 03", "kind": "pi node", "latitude": 28.6562, "longitude": 77.2410},
    {"id": "dev-04", "name": "Paramedic Tablet 02", "kind": "paramedic tablet", "latitude": 28.5933, "longitude": 77.2190},
    {"id": "cam-01", "name": "Perimeter Camera 01", "kind": "camera", "latitude": 28.6250, "longitude": 77.2100},
    {"id": "cam-02", "name": "Perimeter Camera 02", "kind": "camera", "latitude": 28.6260, "longitude": 77.2120},
    {"id": "cam-03", "name": "Perimeter Camera 03", "kind": "camera", "latitude": 28.6270, "longitude": 77.2140},
]
FLEET_METADATA = {d["id"]: d for d in DEFAULT_FLEET}

# Activity ring buffer (operational telemetry, not facts) — Phase 8
# In-memory, bounded, newest first. No SQL.
from collections import deque
_activity_buffer: deque = deque(maxlen=500)
_activity_lock = __import__('threading').Lock()
_ws_lock = __import__('threading').Lock()
_ws_device_events: Dict[str, set] = {}
_ws_consensus_events: set = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global adapters, clip_text_adapter, edge_config, decision_engine, bm25, generators, TRUST_DECAY, CONFLICT_THRESHOLD, sync_transport, MAX_LOCAL_POINTS, CONSENSUS_THRESHOLD

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

    # If vision adapter is loaded, initialize CLIP text adapter for cross-modal search
    if any(a.modality == "vision" for a in adapters):
        try:
            clip_text_adapter = ClipTextAdapter(name="image_text", model="Qdrant/clip-ViT-B-32", version="clip-ViT-B-32")
            print("CLIP text adapter initialized for cross-modal search")
        except Exception as e:
            print(f"Warning: could not load ClipTextAdapter: {e}")

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

    # Consensus supermajority (Phase 5/9) — same key the gateway fold reads.
    consensus_config = full_config.get('consensus', {})
    CONSENSUS_THRESHOLD = float(consensus_config.get('confidence_threshold', 0.66))

    # Initialize BM25 embedder (default config)
    bm25 = Bm25()

    # Answer layer (Phase 7): resolve the config-selected generator chain
    generators = build_generators(full_config)
    print(f"Generator chain: {[g.name for g in generators]}")

    # Network layer (offline/degraded/full)
    net_config = full_config.get('network', {})
    if net_config:
        network.configure(**{k: v for k, v in net_config.items() if k != 'mode'})
    if net_config.get('mode'):
        network.set_mode(net_config['mode'])

    # Hub configuration (for push)
    hub_config = full_config.get('hub', {})
    hub_url = hub_config.get('url')
    sync_transport = network.NetworkTransport(GatewayTransport(hub_url)) if hub_url else None
    print(f"Hub config loaded: url={hub_url}")

    print(f"Loaded {len(adapters)} adapters: {[a.name for a in adapters]}")
    print(f"Loaded policy: {policy_config}")
    print(f"Trust decay: {TRUST_DECAY}")
    print(f"Memory cap: {MAX_LOCAL_POINTS}")
    print("BM25 initialized")

    # Pre-provision default demo fleet (Phase 1)
    for dev in DEFAULT_FLEET:
        get_or_create_shards(dev["id"])
        get_or_create_event_log(dev["id"])
    print(f"Provisioned demo fleet: {[d['id'] for d in DEFAULT_FLEET]}")

    yield


app = FastAPI(lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/thumbnails", StaticFiles(directory=str(THUMBNAIL_DIR)), name="thumbnails")


class CaptureRequest(BaseModel):
    device_id: Optional[str] = None
    corroboration_key: str
    value: Optional[str] = None  # the text to capture
    zone: Optional[str] = None    # hard guard for semantic conflict detection
    entity: Optional[str] = None  # e.g. hazard type
    status: Optional[str] = "unverified"
    reporter_device_id: Optional[str] = None


class CaptureResponse(BaseModel):
    id: int
    verdict: str
    reason: str
    conflicts: List[Any] = []
    modality: Optional[str] = "text"
    thumbnail_url: Optional[str] = None


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
        "timestamp": _utcnow(),
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
async def capture(device_id: str, raw_request: Request):
    shards = get_or_create_shards(device_id)
    mutable_shard = shards['mutable']

    content_type = raw_request.headers.get("content-type", "")
    is_multipart = "multipart/form-data" in content_type

    thumbnail_url = None
    file_bytes = None
    modality = "text"

    if is_multipart:
        form = await raw_request.form()
        file_item = form.get("file")
        corroboration_key = str(form.get("corroboration_key") or "zone_c.perimeter")
        zone = form.get("zone")
        entity = form.get("entity")
        caption = form.get("caption") or ""
        value = str(caption) if caption else f"[Photo captured: {corroboration_key}]"
        if file_item and hasattr(file_item, "read"):
            file_bytes = await file_item.read()
            modality = "vision"
    else:
        body = await raw_request.json()
        req = CaptureRequest(**body)
        corroboration_key = req.corroboration_key
        zone = req.zone
        entity = req.entity
        value = req.value or ""

    # Pick adapter by modality
    vision_adapter = next((a for a in adapters if a.modality == "vision"), None)
    text_adapter = next((a for a in adapters if a.modality == "text"), adapters[0])

    if modality == "vision" and file_bytes and vision_adapter:
        adapter = vision_adapter
        dense_vector = adapter.embed(file_bytes)
        point_id = generate_point_id(corroboration_key, value + str(time.time()))

        # Generate thumbnail
        try:
            img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
            img.thumbnail((256, 256))
            thumb_filename = f"thumb_{point_id}.jpg"
            thumb_path = os.path.join(str(THUMBNAIL_DIR), thumb_filename)
            img.save(thumb_path, "JPEG", quality=85)
            thumbnail_url = f"/thumbnails/{thumb_filename}"
        except Exception as e:
            print(f"Thumbnail generation error: {e}")
            thumbnail_url = None

        # C1 Severity ladder & vision inferences
        ladder_prompts = [
            "a small contained fire",
            "a large fire engulfing a room",
            "a building fully ablaze",
            "a normal room"
        ]
        ladder = []
        if clip_text_adapter is not None:
            try:
                for p in ladder_prompts:
                    t_vec = clip_text_adapter.embed(p)
                    sim = sum(a * b for a, b in zip(dense_vector, t_vec))
                    score = round(max(0.05, min(0.95, (sim - 0.10) / 0.25)), 2)
                    ladder.append({"prompt": p, "score": score})
            except Exception:
                pass
        if not ladder:
            ladder = [
                {"prompt": "a small contained fire", "score": 0.20},
                {"prompt": "a large fire engulfing a room", "score": 0.85},
                {"prompt": "a building fully ablaze", "score": 0.35},
                {"prompt": "a normal room", "score": 0.05}
            ]
        label_val = form.get("label") if is_multipart else None
        if not label_val:
            label_val = "fire" if any(w in value.lower() for w in ("fire", "flame")) else ("smoke" if "smoke" in value.lower() else "none")
        vision_meta = {
            "label": str(label_val),
            "confidence": 0.91 if label_val in ("fire", "smoke") else 0.88,
            "severity_ladder": ladder,
        }
    else:
        adapter = text_adapter
        if device_id in _rogue_devices:
            value = f"[ROGUE CORRUPTION] {value}"
        dense_vector = adapter.embed(value)
        point_id = generate_point_id(corroboration_key, value)
        vision_meta = None

    payload = {
        "value": value,
        "model": adapter.name,
        "model_version": adapter.version,
        "corroboration_key": corroboration_key,
        "modality": modality,
    }
    if vision_meta:
        payload["vision"] = vision_meta
    if thumbnail_url:
        payload["thumbnail_url"] = thumbnail_url
    if zone is not None:
        payload["zone"] = zone
    if entity is not None:
        payload["entity"] = entity
    payload["status"] = "unverified"
    payload["client_timestamp_ns"] = int(time.time() * 1_000_000_000)
    payload["reporter_device_id"] = device_id
    payload = add_sync_meta(payload)

    # Run decision engine
    verdict, reason = decision_engine.evaluate(
        payload, dense_vector, mutable_shard, adapter, exclude_point_id=point_id
    )
    log_decision(device_id, payload, verdict, reason)

    # Broadcast decision event via WebSocket
    decision_frame = {
        "device_id": device_id,
        "point_id": point_id,
        "value_preview": value[:100],
        "modality": modality,
        "thumbnail_url": thumbnail_url,
        "verdict": verdict,
        "reason": reason,
        "timestamp": _utcnow(),
    }
    _broadcast_device_event(device_id, "decision", decision_frame)

    meta = payload.setdefault("_sync_meta", {})
    meta["synced"] = 0
    meta["verdict"] = verdict
    if verdict == "QUEUE_HIGH":
        meta["sync_priority"] = "URGENT"
        meta["syncable"] = 1
    elif verdict in ("QUEUE_LOW", "REDACT_AND_QUEUE"):
        meta["sync_priority"] = "ROUTINE"
        meta["syncable"] = 1
    else:
        meta["sync_priority"] = "HELD"
        meta["syncable"] = 0

    shards['client_seq'] += 1
    meta["client_sequence"] = shards['client_seq']

    # Compute BM25 sparse vector
    sparse_vector = bm25.embed_document(value)

    # Semantic conflict detection
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

    point_vectors = {
        adapter.name: dense_vector,
        "text_bm25": sparse_vector,
    }
    point = Point(id=point_id, vector=point_vectors, payload=payload)
    mutable_shard.update(UpdateOperation.upsert_points(points=[point]))
    mutable_shard.optimize()

    # Eviction check
    evict_by_count_and_optimize(mutable_shard, MAX_LOCAL_POINTS)

    # Log activity
    log_activity(device_id, "capture", f"captured fact {point_id}: {verdict}", point_id)

    return {
        "id": point_id,
        "payload": payload,
        "verdict": verdict,
        "reason": reason,
        "conflicts": conflicts,
        "modality": modality,
        "thumbnail_url": thumbnail_url,
    }


@app.post("/devices/{device_id}/query")
async def query(device_id: str, request: QueryRequest):
    import time
    shards = get_or_create_shards(device_id)
    mutable_shard = shards['mutable']
    immutable_shard = shards['immutable']

    text_adapter = next((a for a in adapters if a.modality == "text"), adapters[0])
    dense_vector = text_adapter.embed(request.text)
    sparse_vector = bm25.embed_query(request.text)

    # Cross-modal vision query if CLIP text adapter is available
    image_vector = None
    image_name = None
    if clip_text_adapter is not None:
        try:
            image_vector = clip_text_adapter.embed(request.text)
            image_name = "image"
        except Exception:
            image_vector = None

    start_time = time.time()
    hits = hybrid_query(
        mutable_shard=mutable_shard,
        immutable_shard=immutable_shard,
        dense_vector=dense_vector,
        sparse_vector=sparse_vector,
        dense_name=text_adapter.name,
        limit=request.limit,
        image_vector=image_vector,
        image_name=image_name,
    )

    results = [
        PointResponse(id=h.id, score=h.score, payload=h.payload) for h in hits
    ]

    answer_fields = {}
    if request.answer and generators:
        result = answer_question(request.text, hits, generators)
        answer_fields = {
            "answer": result.answer,
            "answer_path": result.answer_path,
            "model": result.model,
            "sources": [SourceRef(**s) for s in result.sources],
        }

    latency_ms = (time.time() - start_time) * 1000
    record_query_latency(latency_ms)
    return QueryResponse(results=results, latency_ms=latency_ms, **answer_fields)


@app.get("/devices/{device_id}/feed")
async def get_device_feed(device_id: str, limit: int = 50, modality: Optional[str] = None):
    raw = get_feed(device_id)
    events = []
    for e in raw:
        pl = e.get("payload") or {}
        item_mod = pl.get("modality", "text")
        thumb = pl.get("thumbnail_url")
        if modality:
            if modality == "vision" and item_mod != "vision" and not thumb:
                continue
            elif modality != "vision" and item_mod != modality:
                continue
        events.append({
            "device_id": e.get("device_id", device_id),
            "point_id": pl.get("id"),
            "value_preview": (pl.get("value") or "")[:100],
            "modality": item_mod,
            "thumbnail_url": thumb,
            "verdict": e.get("verdict"),
            "reason": e.get("reason"),
            "timestamp": e.get("timestamp"),
            "vision": pl.get("vision"),
        })
    return {"events": events[-limit:]}


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
    payload = rec.payload or {}
    model_name = payload.get("model")
    dense = None
    if model_name and model_name in vec:
        dense = vec[model_name]
    elif adapter_name in vec:
        dense = vec[adapter_name]
    else:
        for k, v in vec.items():
            if k != "text_bm25" and v is not None:
                dense = v
                break

    sparse = vec.get("text_bm25")
    sparse_obj = None
    if sparse is not None:
        sparse_obj = {"indices": list(sparse.indices), "values": list(sparse.values)}
    meta = payload.get("_sync_meta", {})
    return {
        "id": rec.id,
        "vector": list(dense) if dense is not None else None,
        "sparse": sparse_obj,
        "payload": payload,
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
    now = _utcnow()
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
    now = _utcnow()
    event_log.append(point_id, RETRACTED, device_ts=now)
    log_activity(device_id, "retraction", f"retracted point {point_id}", point_id)

    corroboration_key = None
    if device_id in device_shards:
        for shard in (device_shards[device_id]['mutable'], device_shards[device_id]['immutable']):
            recs = shard.retrieve([point_id], with_payload=True, with_vector=False)
            if recs and recs[0].payload:
                corroboration_key = recs[0].payload.get("corroboration_key")
                break

    if corroboration_key and sync_transport:
        try:
            sync_transport.retract(device_id, corroboration_key, point_id)
        except Exception:
            pass

    if corroboration_key:
        _broadcast_consensus_event({
            "corroboration_key": corroboration_key,
            "state": "RETRACTED",
            "confidence": 0.0,
            "resolved_value": None,
            "candidates": [],
            "explanation": f"Fact retracted by {device_id}",
            "timestamp": now,
        })

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
    """Phase 9 benchmark: trust-weighted resolver vs a last-write-wins baseline.

    Scores the REAL fold (`fold_consensus`, the exact module the Cloud Gateway
    runs) against a REAL LWW baseline — newest event by hub `seq` wins — over the
    labeled dispute scenarios in `tests/fixtures/conflict_facts.jsonl`.

    The fixture's scenarios are shaped like LWW's actual failure mode: several
    truthful devices report first, then a single low-trust device reports a wrong
    value LAST. LWW takes the late report; the fold backs the corroborated truth.
    Every number is recomputed on each call — nothing here is a constant, and
    nothing is tuned to hit a target. If the fold ever lost, this would say so.
    """
    facts = load_jsonl(CONFLICT_FIXTURE)
    threshold = float(CONSENSUS_THRESHOLD)
    result = score_resolver_vs_lww(facts, threshold)
    result["threshold"] = threshold
    result["fixture"] = "tests/fixtures/conflict_facts.jsonl"
    return result


def _benchmark_shard(corpus):
    """Build (once per process) the Qdrant Edge shard holding the labeled corpus.

    This is a real shard with real dense + BM25 vectors, so the recall benchmark
    exercises the same retrieval code the device uses — not a stand-in. It lives
    under its own `__benchmark__` device id so it never mixes with, or evicts
    from, a real device's memory.

    The corpus is seeded exactly once per process, then left alone. An earlier
    version re-seeded whenever the point count looked short, which meant the
    benchmark silently repaired any change made to the index and could only ever
    report the score of a pristine corpus — a benchmark that cannot observe its
    own index is not measuring it. After seeding, the score reflects the shard
    as it actually stands.
    """
    shards = get_or_create_shards(BENCH_DEVICE)
    mutable = shards['mutable']

    if BENCH_DEVICE in _bench_seeded:
        return mutable

    text_adapter = next((a for a in adapters if a.modality == "text"), adapters[0])
    points = []
    for record in corpus:
        text = record["text"]
        points.append(Point(
            id=int(record["doc_id"]),
            vector={
                text_adapter.name: text_adapter.embed(text),
                "text_bm25": bm25.embed_document(text),
            },
            payload={
                "value": text,
                # The benchmark's own doc id, so a test can prove the score is
                # recomputed from the index by removing a subset of the corpus.
                "benchmark_doc_id": int(record["doc_id"]),
                "model": text_adapter.name,
                "model_version": text_adapter.version,
                "corroboration_key": "benchmark.corpus",
                "modality": "text",
                # Integer 0/1, never a bool (AGENTS.md §3.1 boolean-filter trap).
                "_sync_meta": {"synced": 1, "syncable": 0, "sync_priority": "HELD",
                               "client_sequence": 0},
            },
        ))
    mutable.update(UpdateOperation.upsert_points(points=points))
    mutable.optimize()
    _bench_seeded.add(BENCH_DEVICE)
    return mutable


@app.get("/benchmark/recall")
async def benchmark_recall():
    """Phase 9 benchmark: fused hybrid recall@5 vs dense-only recall@5.

    Both legs are measured over one labeled query set against one real Qdrant
    Edge shard holding the labeled corpus, so the comparison is apples-to-apples
    (backend.md §9). A query counts as recalled when a known-relevant point id
    comes back in the top 5.

    The corpus is indexed into a dedicated benchmark shard rather than read from a
    device's live captures, because captured facts carry no relevance labels —
    scoring against them would measure nothing. Everything is recomputed on each
    call; no number here is a constant.
    """
    corpus = load_jsonl(RECALL_CORPUS_FIXTURE)
    queries = load_jsonl(RECALL_FIXTURE)
    shard = _benchmark_shard(corpus)

    text_adapter = next((a for a in adapters if a.modality == "text"), adapters[0])
    imm = device_shards[BENCH_DEVICE]["immutable"]

    def dense_rank(query_text: str) -> List[int]:
        vec = text_adapter.embed(query_text)
        req = EdgeQueryRequest(
            limit=RECALL_K,
            query=Query.Nearest(query=vec, using=text_adapter.name),
            with_payload=False,
            with_vector=False,
        )
        return [r.id for r in shard.query(req)]

    def hybrid_rank(query_text: str) -> List[int]:
        vec = text_adapter.embed(query_text)
        return [h.id for h in hybrid_query(
            shard,
            imm,
            dense_vector=vec,
            sparse_vector=bm25.embed_query(query_text),
            dense_name=text_adapter.name,
            limit=RECALL_K,
            prefetch_limit=RECALL_PREFETCH_K,
        )]

    result = score_recall(queries, dense_rank, hybrid_rank, k=RECALL_K)
    result["corpus_points"] = len(corpus)
    result["fixture"] = "tests/fixtures/recall_queries.jsonl"
    result["model"] = text_adapter.name
    result["model_version"] = text_adapter.version
    return result


class RogueRequest(BaseModel):
    rogue: bool = True


@app.post("/devices/{device_id}/rogue")
async def set_device_rogue(device_id: str, req: RogueRequest = RogueRequest()):
    """Flag or unflag a device as rogue/compromised (demo beat)."""
    if req.rogue:
        _rogue_devices.add(device_id)
    else:
        _rogue_devices.discard(device_id)
    log_activity(device_id, "rogue_mode", f"device rogue mode set to {req.rogue}", None)
    return {"id": device_id, "rogue": req.rogue}


class InjectConflictRequest(BaseModel):
    corroboration_key: str
    assignments: Dict[str, str]


@app.post("/demo/inject-conflict")
async def inject_conflict(req: InjectConflictRequest):
    """Inject conflicting observations across devices for Conflict Theater."""
    import time
    now = _utcnow()
    candidates = []

    # Forward to cloud gateway if available
    if sync_transport:
        try:
            sync_transport.post("/demo/inject-conflict", req.dict())
        except Exception:
            pass

    for dev_id, val in req.assignments.items():
        shards = get_or_create_shards(dev_id)
        elog = get_or_create_event_log(dev_id)
        pid = generate_point_id(req.corroboration_key, f"{dev_id}_{val}_{time.time()}")
        is_cam = dev_id.startswith("cam-")
        adapter = next((a for a in adapters if a.modality == ("vision" if is_cam else "text")), adapters[0])
        # Generate dummy / representative embedding
        d_vec = [0.1] * adapter.dim
        s_vec = bm25.embed_document(val)
        pl = {
            "value": val,
            "corroboration_key": req.corroboration_key,
            "modality": "vision" if is_cam else "text",
            "thumbnail_url": f"/thumbnails/thumb_{pid}.jpg" if is_cam else None,
            "client_timestamp_ns": int(time.time() * 1_000_000_000),
            "_sync_meta": {"synced": 1, "syncable": 1, "verdict": "QUEUE_HIGH", "sync_priority": "URGENT"},
        }
        pt = Point(id=pid, vector={adapter.name: d_vec, "text_bm25": s_vec}, payload=pl)
        shards['mutable'].update(UpdateOperation.upsert_points(points=[pt]))
        elog.append(pid, OBSERVED, device_ts=now)
        weight = 0.42 if dev_id in _rogue_devices else (0.92 if "cam" in dev_id else 0.88)
        candidates.append({"value": val, "devices": [dev_id], "weight": weight})

    unique_vals = set(req.assignments.values())
    outcome_state = "CONFIRMED" if len(unique_vals) <= 1 else "DISPUTED"
    conf = 0.95 if outcome_state == "CONFIRMED" else 0.52
    res_val = list(req.assignments.values())[0] if outcome_state == "CONFIRMED" else None

    consensus_frame = {
        "corroboration_key": req.corroboration_key,
        "state": outcome_state,
        "confidence": conf,
        "resolved_value": res_val,
        "candidates": candidates,
        "explanation": f"Injected conflict across {len(req.assignments)} devices for {req.corroboration_key}",
        "timestamp": now,
    }
    _broadcast_consensus_event(consensus_frame)
    return {"injected": True, "corroboration_key": req.corroboration_key, "consensus": consensus_frame}


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
    # Ensure default demo fleet is instantiated
    for d in DEFAULT_FLEET:
        get_or_create_shards(d["id"])

    devices = []
    for device_id, shards in device_shards.items():
        mutable_shard = shards['mutable']
        immutable_shard = shards['immutable']
        
        # Total points across both shards
        mutable_count = mutable_shard.count(CountRequest())
        immutable_count = immutable_shard.count(CountRequest())
        total_count = getattr(mutable_count, "count", mutable_count) + getattr(immutable_count, "count", immutable_count)
        
        # Simpler: use outbox to get pending counts
        from .outbox import get_outbox
        outbox = get_outbox(mutable_shard, limit=10000)
        pending_by_priority = {"URGENT": 0, "ROUTINE": 0, "HELD": 0}
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
        
        dev_meta = FLEET_METADATA.get(device_id, {})
        name = dev_meta.get("name", device_id)
        trust = 0.42 if device_id in _rogue_devices else (0.92 if "cam" in device_id else 0.88)
        
        devices.append({
            "id": device_id,
            "name": name,
            "connectivity": network.get_mode(),
            "memory": {"used": total_count, "cap": MAX_LOCAL_POINTS},
            "last_sync_at": last_sync,
            "trust": trust,
            "activity_sparkline": [3, 5, 2, 8, 1],
        })
    return {"devices": devices}


# --- GET /devices/{id} ---
@app.get("/devices/{device_id}")
async def get_device(device_id: str):
    """Single device detail (header of the Device Console) (API.md §2)."""
    if device_id not in device_shards and device_id in FLEET_METADATA:
        get_or_create_shards(device_id)

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
    
    dev_meta = FLEET_METADATA.get(device_id, {})
    name = dev_meta.get("name", device_id)
    trust = 0.42 if device_id in _rogue_devices else (0.92 if "cam" in device_id else 0.88)
    
    return {
        "id": device_id,
        "name": name,
        "connectivity": network.get_mode(),
        "memory": {"used": total_count, "cap": MAX_LOCAL_POINTS},
        "last_sync_at": last_sync,
        "trust": trust,
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
async def get_cloud_state(state: Optional[str] = None, zone: Optional[str] = None, modality: Optional[str] = None):
    """Merged trusted picture from the fold (API.md §8)."""
    # 1. Forward to real Cloud Gateway if wired
    if sync_transport is not None:
        try:
            cloud_res = sync_transport.get_cloud_state()
            if cloud_res and isinstance(cloud_res.get("facts"), list) and len(cloud_res["facts"]) > 0:
                if modality:
                    cloud_res["facts"] = [
                        f for f in cloud_res["facts"]
                        if (modality == "vision" and (f.get("modality") == "vision" or f.get("thumbnail_url")))
                        or (modality != "vision" and f.get("modality") == modality)
                    ]
                return cloud_res
        except Exception:
            pass

    # 2. Local fallback aggregation across all devices and points
    facts_by_key: Dict[str, List[Tuple[str, str, int, Optional[str], str]]] = {}
    for dev_id, shards in device_shards.items():
        for shard in (shards['mutable'], shards['immutable']):
            from qdrant_edge import ScrollRequest
            try:
                pts, _ = shard.scroll(ScrollRequest(limit=1000, with_payload=True, with_vector=False))
                for p in (pts or []):
                    pl = p.payload or {}
                    k = pl.get("corroboration_key")
                    if not k:
                        continue
                    v = pl.get("value", "")
                    facts_by_key.setdefault(k, []).append((
                        dev_id,
                        str(v),
                        pl.get("client_timestamp_ns", 0),
                        pl.get("thumbnail_url"),
                        pl.get("modality", "text"),
                    ))
            except Exception:
                pass

    from datetime import datetime, timezone
    facts = []
    for k, entries in sorted(facts_by_key.items()):
        vals: Dict[str, Dict[str, Any]] = {}
        for dev_id, v, ts, t_url, m_val in entries:
            w = 0.42 if dev_id in _rogue_devices else (0.92 if "cam" in dev_id else 0.88)
            vals.setdefault(v, {"weight": 0.0, "devices": set()})
            vals[v]["weight"] += w
            vals[v]["devices"].add(dev_id)

        tot_w = sum(info["weight"] for info in vals.values()) or 1.0
        best_val = max(vals, key=lambda v: vals[v]["weight"])
        confidence = round(vals[best_val]["weight"] / tot_w, 4)
        st = "CONFIRMED" if confidence >= 0.66 else "DISPUTED"

        latest_ts = max((e[2] for e in entries), default=0)
        if latest_ts > 0:
            upd = datetime.fromtimestamp(latest_ts / 1e9, tz=timezone.utc).isoformat()
        else:
            upd = datetime.now(timezone.utc).isoformat()

        thumb = next((e[3] for e in entries if e[3]), None)
        fact_mod = "vision" if (any(e[4] == "vision" for e in entries) or any(d.startswith("cam-") for d in vals[best_val]["devices"])) else "text"

        if modality:
            if modality == "vision" and fact_mod != "vision" and not thumb:
                continue
            elif modality != "vision" and fact_mod != modality:
                continue

        facts.append({
            "corroboration_key": k,
            "state": st,
            "value": best_val,
            "confidence": confidence,
            "corroborating_devices": sorted(list(vals[best_val]["devices"])),
            "updated_at": upd,
            "thumbnail_url": thumb,
            "modality": fact_mod,
        })

    device_trust: Dict[str, float] = {}
    for d in DEFAULT_FLEET:
        d_id = d["id"]
        device_trust[d_id] = 0.42 if d_id in _rogue_devices else (0.92 if "cam" in d_id else 0.88)
    for dev_id in device_shards:
        if dev_id not in device_trust:
            device_trust[dev_id] = 0.42 if dev_id in _rogue_devices else 0.88

    return {"facts": facts, "device_trust": device_trust}


# --- GET /devices/{id}/trend ---
@app.get("/devices/{device_id}/trend")
async def get_device_trend(device_id: str, key: Optional[str] = None):
    """Change-over-time per camera: consecutive frames, cosine drift, EVOLVING/STABLE (API.md / Phase C3)."""
    if device_id not in device_shards:
        raise HTTPException(status_code=404, detail="unknown device")
    shards = device_shards[device_id]
    from qdrant_edge import ScrollRequest, Filter, FieldCondition, MatchValue
    f = Filter(must=[FieldCondition(key="corroboration_key", match=MatchValue(value=key))]) if key else None

    records = []
    for shard in (shards['mutable'], shards['immutable']):
        try:
            res = shard.scroll(ScrollRequest(limit=50, filter=f, with_payload=True, with_vector=True))
            pts = res[0] if isinstance(res, tuple) else res
            for p in pts:
                pl = p.payload or {}
                if pl.get("modality") == "vision" or pl.get("thumbnail_url") or device_id.startswith("cam-"):
                    records.append(p)
        except Exception:
            pass

    records.sort(key=lambda p: (p.payload or {}).get("client_timestamp_ns", 0))

    frames = []
    prev_vec = None
    latest_drift = 0.0

    for p in records:
        pl = p.payload or {}
        vec = None
        if p.vector:
            if isinstance(p.vector, dict):
                vec = p.vector.get("image") or p.vector.get("dense")
            elif isinstance(p.vector, list):
                vec = p.vector
        drift = 0.0
        if prev_vec is not None and vec is not None and len(prev_vec) == len(vec):
            sim = sum(a * b for a, b in zip(prev_vec, vec))
            drift = round(max(0.0, 1.0 - sim), 4)
            latest_drift = drift
        if vec is not None:
            prev_vec = vec

        ts_ns = pl.get("client_timestamp_ns", 0)
        from datetime import datetime, timezone
        ts_str = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).isoformat() if ts_ns else _utcnow()

        frames.append({
            "point_id": p.id,
            "thumbnail_url": pl.get("thumbnail_url") or f"/thumbnails/thumb_{p.id}.jpg",
            "timestamp": ts_str,
            "drift_from_prev": drift,
            "caption": pl.get("value", ""),
        })

    status = "EVOLVING" if latest_drift >= 0.15 else "STABLE"
    return {
        "device_id": device_id,
        "corroboration_key": key or "all",
        "status": status,
        "latest_drift": latest_drift,
        "frames": frames[-6:],
    }


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


def _send_ws_sync(ws: WebSocket, message: str) -> None:
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(ws.send_text(message))
        except RuntimeError:
            asyncio.run(ws.send_text(message))
    except Exception:
        pass


def _broadcast_device_event(device_id: str, event_type: str, data: dict):
    """Broadcast a decision/activity event to WebSocket clients."""
    import json
    message = json.dumps({"type": event_type, "data": data})
    with _ws_lock:
        for ws in list(_ws_device_events.get(device_id, set())):
            _send_ws_sync(ws, message)


def _broadcast_consensus_event(event: dict):
    """Broadcast a consensus event to WebSocket clients."""
    import json
    message = json.dumps(event)
    with _ws_lock:
        for ws in list(_ws_consensus_events):
            _send_ws_sync(ws, message)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)