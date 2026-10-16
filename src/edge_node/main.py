import os
import io
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import asyncio
from typing import List, Dict, Optional, Any, Set, Tuple
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
    HasIdCondition,
    GeoRadius,
    GeoPoint,
    PayloadSchemaType,
)
from datetime import datetime, timezone


def _utcnow() -> str:
    """Return current UTC time as ISO-8601 with Z suffix (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

from .adapter import Adapter, ClipTextAdapter
from .registry import load_adapters, build_generators
from . import model_stamp
from . import snapshot as edge_snapshot
from . import sync_gate
from .redaction import redact as _redact_text
from .decision_engine import DecisionEngine, log_decision, get_feed, clear_feed, load_policy
from .outbox import (
    add_sync_meta,
    get_outbox,
    mark_synced,
    ensure_indexes,
    SYNCABLE_KEY,
    SYNCED_KEY,
    queue_retraction,
    get_pending_retractions,
    mark_retraction_synced,
    delete_points,
)
from .sync_transport import GatewayTransport
from .consensus import EventLog, fold_trust, lww_trust, OBSERVED, RETRACTED
from .conflicts import detect_conflicts, register_conflicts, get_conflicts, clear_conflicts, POSSIBLE_CONFLICT
from . import network
from .retrieval import hybrid_query
from .answer import answer_question, generate_cited
from .telemetry import record_query_latency, get_telemetry
from .eviction import (
    evict_by_count_and_optimize,
    memory_status,
    demote_images_over_thumbnail_cap,
    remove_orphan_thumbnails,
)
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
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("EDGE_CONFIG_PATH") or (REPO_ROOT / "config" / "disaster-response.yaml")
)

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
# Tiered storage (D8 / T9): byte cap for thumbnails before oldest synced image
# points are demoted to embedding-only. 0 = disabled.
MAX_THUMBNAIL_BYTES: int = 0
# Geo-aware recall (D9 / T8): a per-vertical zone -> {lat, lon} map (source (b)).
# Empty = the feature is off for this vertical unless a capture carries explicit
# coordinates (source (c)).
GEO_ZONE_COORDS: Dict[str, Dict[str, float]] = {}
# Presentation tunables (C5), loaded from config in the lifespan. No fabricated
# UI constants in the handlers: the sync backoff is derived from the real
# consecutive-failure count, and the activity sparkline from the real ring
# buffer. VISION_CFG carries the CLIP severity-score map + label prompts.
SYNC_BACKOFF_BASE_MS: int = 1000
SYNC_BACKOFF_CAP_MS: int = 60000
# Value-per-KB sync gate (D3 / T1). `SYNC_BUDGET_BYTES <= 0` means no limit (push
# the whole outbox, the pre-D3 behaviour); a positive budget caps each push so a
# constrained link sends the most valuable facts first. Read from config
# `sync.budget_bytes` / `sync.aging_factor` in the lifespan.
SYNC_BUDGET_BYTES: int = 0
SYNC_AGING_FACTOR: float = sync_gate.DEFAULT_AGING_FACTOR
SPARKLINE_BUCKETS: int = 12
VISION_CFG: Dict[str, Any] = {}
# Last hub-derived per-device trust seen from a /cloud/state fold (B2 is the only
# source of a real trust number). Used so the device list/detail surfaces the
# real value when the hub has computed one, and `null` otherwise — never a
# fabricated constant (C5).
_last_hub_device_trust: Dict[str, float] = {}
# Device ids whose benchmark corpus has already been seeded this process.
_bench_seeded: set = set()
# Where device shards live on disk. Overridable so a crash/durability harness
# (tools/crash_test.py, D1) can point a subprocess at a throwaway temp dir
# instead of the repo's ./shards (D1 Verify-first).
SHARD_BASE_PATH = os.environ.get("EDGE_SHARD_DIR", "./shards")
_rogue_devices: Set[str] = set()

# --- Demo / rogue surface gate (A5 / P0-5) ---------------------------------
# The `/demo/*` endpoints and `/devices/{id}/rogue` exist for the Conflict
# Theater demo, not for the sync protocol. Off by default, so a production edge
# cannot be driven into injecting scripted conflicts or flagging devices rogue
# by an unauthenticated caller. Enable with ENABLE_DEMO_ENDPOINTS=1; when
# EDGE_ADMIN_TOKEN is also set, the caller must present it in X-Admin-Token.
# Demo writes are confined to reserved `demo-*` device shards and `demo:` keys
# (its own client_sequence counter), so they can never touch a real device's
# data or `/delta`.
DEMO_DEVICE_PREFIX = "demo-"
DEMO_KEY_PREFIX = "demo:"
_demo_client_seq: int = 0
_demo_seq_lock = threading.Lock()


def _demo_endpoints_enabled() -> bool:
    return os.environ.get("ENABLE_DEMO_ENDPOINTS", "").strip().lower() in {"1", "true", "yes", "on"}


def _require_demo(request: Request) -> None:
    """Dependency for demo/rogue endpoints: 404 when the surface is disabled,
    401 when an admin token is configured but missing/wrong."""
    if not _demo_endpoints_enabled():
        raise HTTPException(status_code=404, detail="demo endpoints are disabled")
    token = os.environ.get("EDGE_ADMIN_TOKEN", "").strip()
    if token:
        presented = request.headers.get("x-admin-token") or ""
        if not hmac.compare_digest(token, presented):
            raise HTTPException(status_code=401, detail="admin authentication required")


def _demo_device_id(device_id: str) -> str:
    """Confine a demo device to the reserved `demo-` namespace."""
    return device_id if device_id.startswith(DEMO_DEVICE_PREFIX) else DEMO_DEVICE_PREFIX + device_id


def _demo_key(corroboration_key: str) -> str:
    """Confine a demo corroboration key to the reserved `demo:` namespace."""
    return corroboration_key if corroboration_key.startswith(DEMO_KEY_PREFIX) else DEMO_KEY_PREFIX + corroboration_key


def _next_demo_client_seq() -> int:
    """Demo's own monotonic sequence counter, never a real device's."""
    global _demo_client_seq
    with _demo_seq_lock:
        _demo_client_seq += 1
        return _demo_client_seq


# Embedding-model stamp guard (D2 / T5). A shard carries the space stamp of the
# model that produced its vectors; loading it under a different model is refused
# so a cosine search never silently crosses incompatible vector spaces. The
# cosine floor is config/env-overridable (named default lives in model_stamp).
STAMP_COSINE = float(os.environ.get("EDGE_STAMP_COSINE", model_stamp.DEFAULT_STAMP_COSINE))


def _rebuild_requested() -> bool:
    """`--rebuild` (argv) or EDGE_REBUILD (env) lets an operator deliberately
    re-stamp a shard after an intended model change, instead of being blocked."""
    if os.environ.get("EDGE_REBUILD", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    import sys as _sys

    return "--rebuild" in _sys.argv


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

# Wall-clock cost of loading the models and building the local index at
# startup, in ms. Measured once during lifespan; reported by /telemetry.
MODEL_LOAD_MS: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global adapters, clip_text_adapter, edge_config, decision_engine, bm25, generators, TRUST_DECAY, CONFLICT_THRESHOLD, sync_transport, MAX_LOCAL_POINTS, CONSENSUS_THRESHOLD, MODEL_LOAD_MS

    startup_started = time.perf_counter()

    # Load adapters from config
    config_path = str(DEFAULT_CONFIG_PATH)
    with open(config_path, 'r') as f:
        full_config = yaml.safe_load(f)

    # A container cannot reach the host's localhost. Naming the gateway through
    # the environment keeps this one config file usable both on a laptop and
    # inside the compose network, instead of forking it per deployment.
    hub_url_override = os.environ.get("EDGE_HUB_URL")
    if hub_url_override:
        full_config.setdefault("hub", {})["url"] = hub_url_override

    adapters = load_adapters(config_path)
    # Recompute the model space stamp for this (possibly reloaded) model set.
    global _fleet_stamp_cache
    _fleet_stamp_cache = None

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
    clip_text_adapter = None
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
    # Tiered storage (D8 / T9): once thumbnails exceed this many bytes, demote the
    # oldest synced image points to embedding-only (drop the thumbnail, keep the
    # vector). 0 = disabled (no demotion), the pre-D8 behaviour.
    global MAX_THUMBNAIL_BYTES
    MAX_THUMBNAIL_BYTES = int(memory_config.get('max_thumbnail_bytes', 0))

    # Geo-aware recall (D9 / T8): load the vertical's zone -> coords map, if any.
    global GEO_ZONE_COORDS
    geo_config = full_config.get('geo', {}) or {}
    GEO_ZONE_COORDS = {
        str(z): {"lat": float(c["lat"]), "lon": float(c["lon"])}
        for z, c in (geo_config.get('zone_coords', {}) or {}).items()
        if isinstance(c, dict) and "lat" in c and "lon" in c
    }

    # Consensus supermajority (Phase 5/9) — same key the gateway fold reads.
    consensus_config = full_config.get('consensus', {})
    CONSENSUS_THRESHOLD = float(consensus_config.get('confidence_threshold', 0.66))

    # Presentation tunables (C5): sync backoff, sparkline, vision score map.
    global SYNC_BACKOFF_BASE_MS, SYNC_BACKOFF_CAP_MS, SPARKLINE_BUCKETS, VISION_CFG
    global SYNC_BUDGET_BYTES, SYNC_AGING_FACTOR
    sync_config = full_config.get('sync', {})
    SYNC_BACKOFF_BASE_MS = int(sync_config.get('backoff_base_ms', 1000))
    SYNC_BACKOFF_CAP_MS = int(sync_config.get('backoff_cap_ms', 60000))
    # Value-per-KB sync gate (D3 / T1): a non-positive budget keeps the pre-D3
    # "push everything" behaviour; a positive one caps each push by bytes.
    SYNC_BUDGET_BYTES = int(sync_config.get('budget_bytes', 0))
    SYNC_AGING_FACTOR = float(sync_config.get('aging_factor', sync_gate.DEFAULT_AGING_FACTOR))
    ui_config = full_config.get('ui', {})
    SPARKLINE_BUCKETS = int(ui_config.get('sparkline_buckets', 12))
    VISION_CFG = full_config.get('vision', {}) or {}

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
    # Carry the fleet's model space stamp on every push so the hub refuses a
    # batch from an incompatible embedding model (D2 / T5).
    if sync_transport is not None and hasattr(sync_transport, "inner"):
        try:
            sync_transport.inner.space_stamps = _current_fleet_stamp()
        except Exception as exc:  # noqa: BLE001 — never block startup on this
            print(f"Could not compute model space stamp: {exc}")
    print(f"Hub config loaded: url={hub_url}")

    print(f"Loaded {len(adapters)} adapters: {[a.name for a in adapters]}")
    print(f"Vision enabled: {any(a.modality == 'vision' for a in adapters)}")
    print(f"Loaded policy: {policy_config}")
    print(f"Trust decay: {TRUST_DECAY}")
    print(f"Memory cap: {MAX_LOCAL_POINTS}")
    print("BM25 initialized")

    # Pre-provision default demo fleet (Phase 1)
    for dev in DEFAULT_FLEET:
        get_or_create_shards(dev["id"])
        get_or_create_event_log(dev["id"])
    print(f"Provisioned demo fleet: {[d['id'] for d in DEFAULT_FLEET]}")

    # Real startup cost, measured rather than asserted. Recording it here means
    # the number covers adapter loading through fleet provisioning — the window
    # during which the node cannot yet answer a query.
    MODEL_LOAD_MS = (time.perf_counter() - startup_started) * 1000.0
    print(f"Startup took {MODEL_LOAD_MS:.0f} ms")

    yield


app = FastAPI(lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware

# The dashboard runs on a different origin from the node, so the browser needs
# permission to read the API. Localhost is fine for development but meaningless
# once deployed: the served origin is whatever domain the deployment is on, and
# a hardcoded list would silently block the real UI. DEPLOY_ORIGINS carries the
# comma-separated origins to allow in addition to the local ones.
_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "https://ex-vero.vercel.app",
]
for _origin in (os.environ.get("ALLOWED_ORIGINS") or "").split(","):
    _origin = _origin.strip()
    if _origin and _origin not in _ALLOWED_ORIGINS:
        _ALLOWED_ORIGINS.append(_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    """Readiness, derived from real startup state rather than a fixed reply.

    Orchestrators (docker compose, the container acceptance test) need a way to
    tell "still loading models" from "can answer a query". Reporting "ok"
    unconditionally would make them push traffic at a node that cannot serve it.
    """
    ready = adapters is not None and bm25 is not None and bool(device_shards)
    return {
        "status": "ok" if ready else "starting",
        "adapters": [a.name for a in adapters] if adapters else [],
        "devices": len(device_shards),
        "model_load_ms": MODEL_LOAD_MS,
    }
app.mount("/thumbnails", StaticFiles(directory=str(THUMBNAIL_DIR)), name="thumbnails")


class CaptureRequest(BaseModel):
    device_id: Optional[str] = None
    corroboration_key: str
    value: Optional[str] = None  # the text to capture
    zone: Optional[str] = None    # hard guard for semantic conflict detection
    entity: Optional[str] = None  # e.g. hazard type
    status: Optional[str] = "unverified"
    reporter_device_id: Optional[str] = None
    # Optional explicit coordinates (D9 / T8, source (c)). When absent, a zone in
    # the vertical's geo.zone_coords map supplies them (source (b)); when neither
    # is present the fact simply has no location and geo recall ignores it.
    lat: Optional[float] = None
    lon: Optional[float] = None


class CaptureResponse(BaseModel):
    id: str
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
    # Cited answers (D6 / T4): when true, return a per-sentence citation breakdown
    # (`sentences`) alongside the answer, each sentence validated against sources.
    cited: bool = False
    # Optional geo recall (D9 / T8): restrict hits to within `radius_m` of
    # (lat, lon). This filters retrieval only — it never changes a
    # corroboration_key or the consensus fold.
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_m: Optional[float] = None


class PointResponse(BaseModel):
    id: str
    score: float
    payload: dict


class SourceRef(BaseModel):
    """A cited retrieved fact backing the answer (docs/API.md §4)."""
    id: str
    score: float
    value: Optional[str] = None
    consensus_state: Optional[str] = None
    # Provenance for cited answers (D6 / T4).
    device: Optional[str] = None
    modality: Optional[str] = None


class CitedSentence(BaseModel):
    """One sentence of a cited answer (D6 / T4): its text, the source numbers it
    cites, and whether those citations validate against the retrieved set."""
    text: str
    citations: List[int] = []
    supported: bool = False


class QueryResponse(BaseModel):
    results: List[PointResponse]
    latency_ms: float
    # Present only when an answer was generated (`answer: true`).
    answer: Optional[str] = None
    answer_path: Optional[str] = None    # "offline" | "online" | "extractive"
    model: Optional[str] = None
    sources: Optional[List[SourceRef]] = None
    # Present only when `cited: true` was requested (D6 / T4).
    sentences: Optional[List[CitedSentence]] = None


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


def _next_point_id() -> int:
    """Return a unique positive point ID for generated conflict observations."""
    with _point_id_lock:
        return secrets.randbits(63) or 1


def _cleanup_thumbnail(payload: Dict[str, Any]) -> None:
    """Remove the on-disk thumbnail of an evicted vision point (C1).

    The payload stores only the served URL (`/thumbnails/<name>`); the file
    lives under THUMBNAIL_DIR. A missing file (already pruned) is not an error.
    """
    url = payload.get("thumbnail_url")
    if not url:
        return
    path = os.path.join(str(THUMBNAIL_DIR), os.path.basename(str(url)))
    try:
        os.remove(path)
    except OSError:
        pass


def _thumbnail_bytes_of(payload: Dict[str, Any]) -> int:
    """On-disk byte size of a point's thumbnail (0 if none / missing). Used by the
    tiered-storage demotion to measure the thumbnail budget (D8 / T9)."""
    url = payload.get("thumbnail_url")
    if not url:
        return 0
    path = os.path.join(str(THUMBNAIL_DIR), os.path.basename(str(url)))
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _demote_thumbnails_if_over_cap(mutable_shard) -> list:
    """Tier-demote oldest synced image points if thumbnails exceed the byte cap
    (D8 / T9). No-op when tiering is disabled (MAX_THUMBNAIL_BYTES <= 0)."""
    if MAX_THUMBNAIL_BYTES <= 0:
        return []
    return demote_images_over_thumbnail_cap(
        mutable_shard, MAX_THUMBNAIL_BYTES,
        thumbnail_bytes_of=_thumbnail_bytes_of,
        on_demote=_cleanup_thumbnail,
    )


def _resolve_location(zone, lat, lon) -> Optional[Dict[str, float]]:
    """Resolve a capture's geo location (D9 / T8): explicit lat/lon (source (c))
    takes precedence, else the vertical's zone -> coords map (source (b)), else
    None (the fact has no location and geo recall ignores it)."""
    if lat is not None and lon is not None:
        return {"lat": float(lat), "lon": float(lon)}
    if zone and zone in GEO_ZONE_COORDS:
        c = GEO_ZONE_COORDS[zone]
        return {"lat": float(c["lat"]), "lon": float(c["lon"])}
    return None


def _ensure_geo_index(shard) -> None:
    """Create the geo payload index on `location` so GeoRadius filters are not a
    full scan (D9 / T8). Idempotent / benign if it already exists."""
    try:
        shard.update(UpdateOperation.create_field_index(
            field_name="location", schema=PayloadSchemaType.Geo
        ))
    except Exception:
        pass


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    import math
    r = 6371000.0  # Earth radius (m)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _memory_dict(mutable_shard, total_count: int) -> Dict[str, Any]:
    """Memory-usage shape (API.md §1) plus the visible cap status (C1)."""
    st = memory_status(mutable_shard, MAX_LOCAL_POINTS)
    return {
        "used": total_count,
        "cap": MAX_LOCAL_POINTS,
        "over_cap": st["over_cap"],
        "status": st["status"],
    }


def _activity_sparkline(device_id: str, buckets: Optional[int] = None) -> List[int]:
    """A real histogram of this device's activity entries (C5).

    The ring buffer is split oldest→newest into `buckets` equal columns and each
    column is the count of entries that fall in it. No fabricated constant
    series: an idle device returns all-zeros, a busy one shows its real shape.
    """
    n = buckets or SPARKLINE_BUCKETS
    if n <= 0:
        return []
    with _activity_lock:
        entries = [e for e in _activity_buffer if e["device_id"] == device_id]
    out = [0] * n
    if not entries:
        return out
    entries.reverse()  # oldest first
    import math
    size = max(1, math.ceil(len(entries) / n))
    for i in range(n):
        out[i] = len(entries[i * size:(i + 1) * size])
    return out


def _device_trust(device_id: str) -> Optional[float]:
    """The real hub-derived trust for a device, or None when the hub has not
    computed one (C5). Never a fabricated constant."""
    return _last_hub_device_trust.get(device_id)


def _next_backoff_ms(consecutive_failures: int) -> int:
    """Exponential retry backoff derived from the REAL consecutive-failure count
    (C5): base * 2**(failures-1), capped. 0 when there is no active failure."""
    if consecutive_failures <= 0:
        return 0
    return int(min(SYNC_BACKOFF_CAP_MS, SYNC_BACKOFF_BASE_MS * (2 ** (consecutive_failures - 1))))


def _last_push_summary(device_id: str) -> Dict[str, Any]:
    """The last real push record for a device from the network push log (C5).

    network.get_push_log() records true bytes/duration/attempted/accepted/failed
    and priority mix per push, so the sync panel shows measured numbers instead
    of zero placeholders. Empty dict when the device has never pushed.
    """
    for entry in reversed(network.get_push_log()):
        if entry.get("device_id") not in (None, device_id):
            continue
        return {
            "bytes": entry.get("bytes", 0),
            "duration_ms": round(entry.get("duration_ms", 0.0), 1),
            "points_attempted": entry.get("attempted", 0),
            "points_accepted": entry.get("accepted", 0),
            "points_failed": entry.get("failed", 0),
            "priority_mix": entry.get("priority_mix", {}),
            "mode": entry.get("mode", network.get_mode()),
        }
    return {}


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


_fleet_stamp_cache: Optional[Dict[str, Any]] = None


def _current_fleet_stamp() -> Dict[str, Any]:
    """The space stamp of the loaded models (D2 / T5), computed once and cached.

    Each slot's stamp is derived from the adapter's identity + real dim + a probe
    vector it produces — never a typed version string. Used to stamp new shards,
    verify loaded ones, sign push batches, and check pulled snapshots.
    """
    global _fleet_stamp_cache
    if _fleet_stamp_cache is None:
        _fleet_stamp_cache = model_stamp.fleet_stamp(adapters or [])
    return _fleet_stamp_cache


def _verify_shard_stamp(shard_dir: str) -> None:
    """Guard a shard dir against a model change (D2 / T5, replaces the old
    zero-vector `_verify_shard_dimension`).

    * A freshly created / never-stamped shard is stamped with the live fleet
      stamp (back-compatible: pre-D2 shards get stamped on first load).
    * A shard whose stored stamp is incompatible with the live models (different
      dimension, or a probe vector that no longer agrees by cosine) is REFUSED —
      we raise so startup fails loudly instead of serving silent garbage. An
      operator who changed the model on purpose passes `--rebuild` / EDGE_REBUILD
      to re-stamp it.
    """
    if not adapters:
        return
    live = _current_fleet_stamp()
    stored = model_stamp.read_stamp(shard_dir)
    if stored is None:
        model_stamp.write_stamp(shard_dir, live)
        return
    problems = model_stamp.diff_fleet(live, stored, min_cosine=STAMP_COSINE)
    if problems:
        if _rebuild_requested():
            print(f"[stamp] --rebuild: re-stamping {shard_dir} ({problems})")
            model_stamp.write_stamp(shard_dir, live)
            return
        raise RuntimeError(
            "Embedding-model stamp mismatch for shard "
            f"'{shard_dir}': {problems}. The shard's vectors were produced by a "
            "different model than the one now loaded; a search across them would "
            "return silent garbage. Re-embed the data or restart with --rebuild "
            "(EDGE_REBUILD=1) to re-stamp this shard deliberately."
        )


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
        model_stamp.write_stamp(mutable_path, _current_fleet_stamp())
    else:
        mutable_shard = EdgeShard.load(mutable_path)
        # Refuse to load a shard produced by a different embedding model (D2 /
        # T5): a cosine search across incompatible spaces returns silent garbage.
        _verify_shard_stamp(mutable_path)
        info = mutable_shard.info()
        print(f"Loaded mutable shard for {device_id}: {info}")
        # Tiered storage (D8 / T9): on restart, prune thumbnail files no loaded
        # point references any more (gated on tiering being enabled).
        if MAX_THUMBNAIL_BYTES > 0:
            try:
                remove_orphan_thumbnails(mutable_shard, str(THUMBNAIL_DIR))
            except Exception as exc:  # noqa: BLE001 — cleanup must never block load
                print(f"orphan thumbnail scan failed for {device_id}: {exc}")

    # Integer indexes for the outbox view (synced / syncable / client_sequence).
    # A bool field would silently match nothing (Phase 0 trap); integers work.
    ensure_indexes(mutable_shard)
    # Geo index for optional geo-aware recall (D9 / T8).
    _ensure_geo_index(mutable_shard)

    # Create or load immutable shard (empty for now)
    if not os.path.exists(immutable_path):
        os.makedirs(immutable_path, exist_ok=True)
        immutable_shard = EdgeShard.create(immutable_path, edge_config)
        model_stamp.write_stamp(immutable_path, _current_fleet_stamp())
    else:
        immutable_shard = EdgeShard.load(immutable_path)
        _verify_shard_stamp(immutable_path)
        print(f"Loaded immutable shard for {device_id}: {info}")
    _ensure_geo_index(immutable_shard)
    
    max_client_sequence = 0
    from qdrant_edge import ScrollRequest
    for shard in (mutable_shard, immutable_shard):
        try:
            scroll_result = shard.scroll(ScrollRequest(limit=100000, with_payload=True, with_vector=False))
            records = scroll_result[0] if isinstance(scroll_result, tuple) else scroll_result
            for record in records:
                sequence = ((record.payload or {}).get("_sync_meta") or {}).get("client_sequence", 0)
                if isinstance(sequence, int):
                    max_client_sequence = max(max_client_sequence, sequence)
        except Exception as exc:
            print(f"Could not restore client sequence for {device_id}: {exc}")

    device_shards[device_id] = {
        'mutable': mutable_shard,
        'immutable': immutable_shard,
        'client_seq': max_client_sequence,  # resume monotonic sequence after restart
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
        corroboration_key = str(form.get("corroboration_key") or "").strip()
        zone = form.get("zone")
        entity = form.get("entity")
        caption = form.get("caption") or ""
        value = str(caption) if caption else f"[Photo captured: {corroboration_key}]"
        if not corroboration_key:
            raise HTTPException(status_code=422, detail="corroboration_key is required")
        if not zone:
            raise HTTPException(status_code=422, detail="zone is required for image capture")
        if not file_item or not hasattr(file_item, "read"):
            raise HTTPException(status_code=422, detail="file is required for image capture")
        file_bytes = await file_item.read()
        if not file_bytes:
            raise HTTPException(status_code=422, detail="uploaded image is empty")
        modality = "vision"
    else:
        body = await raw_request.json()
        req = CaptureRequest(**body)
        corroboration_key = req.corroboration_key
        zone = req.zone
        entity = req.entity
        value = req.value or ""
        if not value.strip():
            raise HTTPException(status_code=422, detail="value is required")

    # Pick adapter by modality
    vision_adapter = next((a for a in adapters if a.modality == "vision"), None)
    text_adapter = next((a for a in adapters if a.modality == "text"), adapters[0])

    if modality == "vision" and file_bytes:
        if vision_adapter is None:
            text_image_fallback = os.environ.get("EDGE_ALLOW_TEXT_IMAGE_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}
            if not text_image_fallback:
                raise HTTPException(status_code=503, detail="vision embedding is not enabled in backend configuration")
            # Low-memory profile: keep the real image and thumbnail, but index
            # the supplied caption with the text model instead of loading CLIP.
            adapter = text_adapter
            dense_vector = adapter.embed(value)
        else:
            adapter = vision_adapter
            dense_vector = adapter.embed(file_bytes)
        # Content-hash id (C1): the same image bytes always map to the same point
        # id, so a re-capture de-duplicates instead of appending a new point.
        point_id = generate_point_id(corroboration_key, hashlib.sha256(file_bytes).hexdigest())

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

        # Severity ladder & vision inferences (C5): the score map comes from
        # config, and the ladder is only populated from REAL CLIP similarities.
        # When the CLIP text adapter is unavailable we emit NO ladder rather than
        # fabricating severity numbers the model never produced.
        ladder_prompts = [str(p.get("prompt")) for p in VISION_CFG.get("severity_prompts", [])]
        sim_offset = float(VISION_CFG.get("score_sim_offset", 0.10))
        sim_scale = float(VISION_CFG.get("score_sim_scale", 0.25)) or 1.0
        clamp_min = float(VISION_CFG.get("score_clamp_min", 0.05))
        clamp_max = float(VISION_CFG.get("score_clamp_max", 0.95))
        ladder = []
        if clip_text_adapter is not None and ladder_prompts:
            try:
                for p in ladder_prompts:
                    t_vec = clip_text_adapter.embed(p)
                    sim = sum(a * b for a, b in zip(dense_vector, t_vec))
                    score = round(max(clamp_min, min(clamp_max, (sim - sim_offset) / sim_scale)), 2)
                    ladder.append({"prompt": p, "score": score})
            except Exception:
                ladder = []
        label_val = form.get("label") if is_multipart else None
        if not label_val:
            label_val = "fire" if any(w in value.lower() for w in ("fire", "flame")) else ("smoke" if "smoke" in value.lower() else "none")
        # Confidence is the strongest real severity signal in the ladder, not a
        # per-label constant. With no ladder (no CLIP) there is no honest
        # confidence to report, so it is omitted.
        vision_meta = {
            "label": str(label_val),
            "severity_ladder": ladder,
        }
        if ladder:
            vision_meta["confidence"] = max(item["score"] for item in ladder)
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
    # Geo-aware recall (D9 / T8): stamp a `location` geo field when the capture
    # carries coordinates or its zone maps to coords. This is recall metadata
    # only — it never changes the corroboration_key or the consensus fold.
    _req_lat = getattr(req, "lat", None) if not is_multipart else None
    _req_lon = getattr(req, "lon", None) if not is_multipart else None
    _location = _resolve_location(zone, _req_lat, _req_lon)
    if _location is not None:
        payload["location"] = _location
    payload["status"] = (req.status or "unverified") if not is_multipart else "unverified"
    payload["client_timestamp_ns"] = int(time.time() * 1_000_000_000)
    payload["reporter_device_id"] = (req.reporter_device_id or req.device_id or device_id) if not is_multipart else device_id
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

    # Persist the novelty + urgency scores at capture (D3 / T1) so the
    # value-per-KB sync gate can rank pending facts by information-per-byte
    # without re-querying the shard at push time. Novelty = 1 - nearest-neighbour
    # similarity; urgency = keyword score. Both already drove the verdict above;
    # here we recompute and store them as explicit sync-value inputs.
    try:
        nov, urg = decision_engine.score(
            payload, dense_vector, mutable_shard, adapter, exclude_point_id=point_id
        )
        meta["novelty"] = round(nov, 6)
        meta["urgency"] = round(urg, 6)
    except Exception:
        # Scoring is best-effort metadata; a failure must never block a capture.
        meta.setdefault("novelty", 0.0)
        meta.setdefault("urgency", 0.0)

    # Compute BM25 sparse vector
    sparse_vector = bm25.embed_document(value)

    # Semantic conflict detection over BOTH shards (B7): the immutable shard
    # holds facts pulled from the fleet, so a contradicting report another device
    # made is caught here.
    immutable_shard = shards['immutable']
    conflicts = detect_conflicts(
        shard=mutable_shard,
        dense_vector=dense_vector,
        sparse_vector=sparse_vector,
        dense_name=adapter.name,
        new_point_id=point_id,
        new_payload=payload,
        similarity_threshold=CONFLICT_THRESHOLD,
        extra_shards=[immutable_shard],
    )
    register_conflicts(device_id, conflicts)
    # Persist the conflict flag on the point payload so it survives a restart
    # (not only the in-memory register).
    if conflicts:
        payload["has_conflict"] = 1
        payload["conflict_with"] = sorted({str(c["existing_point_id"]) for c in conflicts})

    point_vectors = {
        adapter.name: dense_vector,
        "text_bm25": sparse_vector,
    }
    point = Point(id=point_id, vector=point_vectors, payload=payload)
    mutable_shard.update(UpdateOperation.upsert_points(points=[point]))
    mutable_shard.optimize()
    # Durability (D1 / T10): optimize() alone did not survive a SIGKILL in the
    # crash harness (facts were lost), so flush the segment to disk after the
    # write. Measured cost is recorded in results/crash_safety.json.
    mutable_shard.flush()

    # Eviction check (removes the thumbnail file of any evicted vision point)
    evict_by_count_and_optimize(mutable_shard, MAX_LOCAL_POINTS, on_evict=_cleanup_thumbnail)
    # Tiered storage (D8 / T9): after the count cap, enforce the thumbnail byte
    # cap by demoting the oldest synced image points to embedding-only.
    _demote_thumbnails_if_over_cap(mutable_shard)

    # Log activity
    log_activity(device_id, "capture", f"captured fact {point_id}: {verdict}", point_id)

    return {
        "id": str(point_id),
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
    # Optional geo recall (D9 / T8): restrict hits to within radius_m of (lat,lon).
    geo_filter = None
    if request.lat is not None and request.lon is not None and request.radius_m:
        geo_filter = Filter(must=[FieldCondition(
            key="location",
            geo_radius=GeoRadius(
                center=GeoPoint(lat=float(request.lat), lon=float(request.lon)),
                radius=float(request.radius_m),
            ),
        )])
    hits = hybrid_query(
        mutable_shard=mutable_shard,
        immutable_shard=immutable_shard,
        dense_vector=dense_vector,
        sparse_vector=sparse_vector,
        dense_name=text_adapter.name,
        limit=request.limit,
        image_vector=image_vector,
        image_name=image_name,
        geo_filter=geo_filter,
    )

    results = []
    for h in hits:
        payload = h.payload
        # Annotate the haversine distance from the query centre (recall metadata
        # only; a "possible same event" hint, never a fold input).
        if geo_filter is not None:
            loc = (payload or {}).get("location")
            if isinstance(loc, dict) and "lat" in loc and "lon" in loc:
                payload = dict(payload or {})
                payload["distance_m"] = round(
                    _haversine_m(float(request.lat), float(request.lon),
                                 float(loc["lat"]), float(loc["lon"])), 1
                )
        results.append(PointResponse(id=str(h.id), score=h.score, payload=payload))

    answer_fields = {}
    if request.answer and generators:
        # Ollama generation is a blocking HTTP round-trip; keep it off the event
        # loop so the answer path never stalls other requests (AGENTS.md).
        if request.cited:
            result = await asyncio.to_thread(generate_cited, request.text, hits, generators)
        else:
            result = await asyncio.to_thread(answer_question, request.text, hits, generators)
        answer_fields = {
            "answer": result.answer,
            "answer_path": result.answer_path,
            "model": result.model,
            "sources": [SourceRef(id=str(s["id"]), score=s["score"], value=s.get("value"), consensus_state=s.get("consensus_state"), device=s.get("device"), modality=s.get("modality")) for s in result.sources],
        }
        if result.sentences is not None:
            answer_fields["sentences"] = [
                CitedSentence(text=s["text"], citations=s["citations"], supported=s["supported"])
                for s in result.sentences
            ]

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
            "point_id": str(pl.get("id")) if pl.get("id") is not None else None,
            "value_preview": (pl.get("value") or "")[:100],
            "modality": item_mod,
            "thumbnail_url": thumb,
            "verdict": e.get("verdict"),
            "reason": e.get("reason"),
            "timestamp": e.get("timestamp"),
            "vision": pl.get("vision"),
        })
    return {"events": events[-limit:]}


# =============================================================================
# Phase 8 — Eviction + Inspection Endpoints
# =============================================================================

from fastapi import WebSocket, WebSocketDisconnect
from typing import Set


# --- Active WebSocket connections for live feeds ---
_ws_device_events: Dict[str, Set[WebSocket]] = {}
_ws_consensus_events: Set[WebSocket] = set()
_ws_lock = __import__('threading').Lock()


# --- GET /devices ---


# --- GET /devices/{id} ---


# --- GET /devices/{id}/memory ---


# --- GET /devices/{id}/memory/{point_id} ---
# @app.get("/devices/{device_id}/memory/{point_id}")
# async def get_memory_point(device_id: str, point_id: str):
#     """Full detail for one fact (API.md §5)."""
#     try:
#         numeric_point_id = int(point_id)
#     except ValueError:
#         raise HTTPException(status_code=400, detail="invalid point_id")

#     if device_id not in device_shards:
#         raise HTTPException(status_code=404, detail="unknown device")
    
#     shards = device_shards[device_id]
#     mutable_shard = shards['mutable']
#     immutable_shard = shards['immutable']
    
#     # Try mutable first, then immutable
#     for shard in (mutable_shard, immutable_shard):
#         recs = shard.retrieve([numeric_point_id], with_payload=True, with_vector=False)
#         if recs:
#             rec = recs[0]
#             payload = rec.payload or {}
#             meta = payload.get("_sync_meta", {})
#             sync_state_val = "synced" if meta.get("synced") == 1 else ("local_only" if meta.get("syncable") == 0 else "pending")
            
#             point = {
#                 "id": str(rec.id),
#                 "value": payload.get("value", ""),
#                 "modality": payload.get("modality", "text"),
#                 "thumbnail_url": payload.get("thumbnail_url"),
#                 "zone": payload.get("zone"),
#                 "corroboration_key": payload.get("corroboration_key"),
#                 "sync_state": sync_state_val,
#                 "model": payload.get("model", "text_dense"),
#                 "model_version": payload.get("model_version", "bge-small-en-v1.5"),
#                 "created_at": payload.get("client_timestamp_ns", 0),
#             }
            
#             # Get decision event
#             decision = None
#             for entry in get_feed(device_id):
#                 if entry.get("payload", {}).get("id") == point_id or entry.get("point_id") == point_id:
#                     decision = {
#                         "device_id": entry.get("device_id"),
#                         "point_id": point_id,
#                         "value_preview": entry.get("payload", {}).get("value", "")[:100],
#                         "modality": payload.get("modality", "text"),
#                         "thumbnail_url": payload.get("thumbnail_url"),
#                         "verdict": meta.get("verdict", "UNKNOWN"),
#                         "reason": entry.get("reason", ""),
#                         "timestamp": entry.get("timestamp"),
#                     }
#                     break
            
#             # Get activity for this point
#             activity = [e for e in get_activity(device_id) if e.get("point_id") == point_id]
            
#             # Get consensus if available
#             consensus = None
#             try:
#                 event_log = get_or_create_event_log(device_id)
#                 events = event_log.events_for(point_id)
#                 if events:
#                     trust = fold_trust(events, decay=TRUST_DECAY)
#                     consensus = {
#                         "corroboration_key": payload.get("corroboration_key"),
#                         "state": "CONFIRMED" if trust > 0.5 else "DISPUTED",
#                         "confidence": trust,
#                         "resolved_value": payload.get("value") if trust > 0.5 else None,
#                         "candidates": [],
#                         "explanation": "",
#                         "timestamp": events[-1].get("device_ts", "") if events else "",
#                     }
#             except Exception:
#                 pass
            
#             return {
#                 "point": point,
#                 "decision": decision,
#                 "activity": activity,
#                 "consensus": consensus,
#             }
    
#     raise HTTPException(status_code=404, detail="point not found")

# --- GET /devices/{id}/memory/{point_id} ---


# --- GET /devices/{id}/sync ---


# --- GET /devices/{id}/activity ---


# --- GET /devices/{id}/telemetry ---


# --- GET /cloud/state ---


# --- GET /devices/{id}/trend ---


# --- WS /devices/{id}/events ---


# --- WS /consensus/events ---


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


# API factories share this runtime's existing state and preserve its public seams.
import sys as _sys
from .services.sync_orchestration import register as _register_sync_services
from .api import network_routes, benchmark_routes, device_routes, demo_routes, stream_routes, sync_routes
_runtime = _sys.modules[__name__]
_register_sync_services(_runtime)
for _routes in (network_routes, sync_routes, benchmark_routes, demo_routes, device_routes, stream_routes):
    app.include_router(_routes.register(_runtime))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)