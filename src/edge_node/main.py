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
from .answer import answer_question
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
        result = await asyncio.to_thread(answer_question, request.text, hits, generators)
        answer_fields = {
            "answer": result.answer,
            "answer_path": result.answer_path,
            "model": result.model,
            "sources": [SourceRef(id=str(s["id"]), score=s["score"], value=s.get("value"), consensus_state=s.get("consensus_state")) for s in result.sources],
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
    # Never ship the device-local redaction record (it holds the raw PII the
    # redaction removed): strip it from the outbound payload copy (D5 / T7).
    payload = {k: v for k, v in payload.items() if k != "_redaction"}
    meta = payload.get("_sync_meta", {})
    return {
        "id": rec.id,
        "vector": list(dense) if dense is not None else None,
        "sparse": sparse_obj,
        "payload": payload,
        "client_sequence": meta.get("client_sequence", 0),
    }


def _redact_envelope_in_place(env: dict) -> List[Dict[str, Any]]:
    """Redact PII from an outbound envelope so the hub never sees it (D5 / T7).

    Rebuilds the envelope from the REDACTED text: the `value`, the sparse BM25
    (recomputed from the redacted text — the stored sparse was built from the raw
    value and would otherwise leak PII tokens), and, for a text point, the dense
    vector (re-embedded from the redacted text). An image point's dense vector
    comes from the image, not the caption, so it is left untouched. Returns the
    removed spans (for the device's local record), or [] when there was no PII.
    """
    payload = env.get("payload") or {}
    raw_value = payload.get("value")
    patterns = [getattr(p, "pattern", p) for p in getattr(decision_engine, "pii_patterns", [])]
    if not isinstance(raw_value, str) or not patterns:
        return []
    redacted, spans = _redact_text(raw_value, patterns)
    if not spans:
        return []

    new_payload = dict(payload)
    new_payload["value"] = redacted
    new_payload.pop("_redaction", None)  # defensive: never leak the local record
    new_payload["redacted"] = 1
    env["payload"] = new_payload

    # Recompute the sparse leg from the redacted text (the raw sparse leaked PII).
    if env.get("sparse") is not None:
        sp = bm25.embed_document(redacted)
        env["sparse"] = {"indices": list(sp.indices), "values": list(sp.values)}

    # Re-embed the dense vector from the redacted text for text points only.
    if payload.get("modality", "text") == "text" and env.get("vector") is not None:
        model_name = payload.get("model")
        adapter = next((a for a in adapters if a.name == model_name), None)
        if adapter is None:
            adapter = next((a for a in adapters if a.modality == "text"), adapters[0])
        env["vector"] = list(adapter.embed(redacted))

    return spans


@app.post("/devices/{device_id}/push")
async def push(device_id: str):
    # The sync drain does real blocking I/O (the degraded link sleeps, the token
    # bucket waits, the gateway HTTP round-trips). Run it off the event loop so a
    # slow/degraded push never stalls concurrent local queries (AGENTS.md: handlers
    # must not block the loop).
    pushed_count, errors = await asyncio.to_thread(_do_push, device_id)
    if pushed_count > 0:
        log_activity(device_id, "push_result", f"pushed {pushed_count} points", point_id=None)
    if errors:
        log_activity(device_id, "error", f"push errors: {errors}", point_id=None)
    return PushResponse(pushed_count=pushed_count, errors=errors)


def _do_push(device_id: str):
    """Drain the outbox to the hub. Returns (pushed_count, errors).

    Shared by the push endpoint and the pull flush-first step. Marks synced ONLY
    on ack (invariant 1): any failure leaves the point pending and on device.
    Queued retractions drain here too, in client_sequence order with observations.
    """
    shards = get_or_create_shards(device_id)
    mutable_shard = shards['mutable']

    if sync_transport is None:
        # No transport wired: fail loudly rather than silently "succeeding".
        return 0, ["no sync transport configured"]

    pushed_count = 0
    errors: List[str] = []

    # The outbox is a filtered VIEW over the mutable shard
    # (synced == 0 AND syncable == 1), URGENT before ROUTINE — not a second store.
    outbox = get_outbox(mutable_shard, limit=1000)
    if outbox:
        adapter_name = adapters[0].name

        # Build the delta batch straight from the outbox view. We deliberately do
        # NOT pre-filter by the hub's high-water client_sequence (P0-6d): a
        # partial or out-of-order ack would advance that watermark past a point
        # the hub never actually stored and strand every lower-sequence pending
        # point forever. The hub upsert is idempotent by deterministic id, so
        # re-sending a point it already holds is harmless, and the outbox view
        # itself (synced == 0) keeps us from re-sending points already acked.
        envelopes = []
        ids_in_order = []
        for pid, rec in outbox:
            env = _envelope_from_record(rec, adapter_name)
            envelopes.append(env)
            ids_in_order.append(pid)

        # PII redaction before anything leaves the device (D5 / T7): rebuild each
        # envelope from redacted text (value + sparse + dense), so no PII reaches
        # the hub. The raw copy stays in the local shard; we keep the removed
        # spans to persist a local redaction record on the acked points below.
        redaction_records: Dict[int, List[Dict[str, Any]]] = {}
        for pid, env in zip(ids_in_order, envelopes):
            spans = _redact_envelope_in_place(env)
            if spans:
                redaction_records[pid] = spans

        # Value-per-KB sync gate (D3 / T1): under a byte budget, select the most
        # valuable facts first (URGENT before ROUTINE, then age-boosted value
        # density) and defer the rest to the next push — they stay pending on the
        # device, never dropped. A non-positive budget means no limit (push all),
        # preserving the pre-D3 behaviour. Selection is a pure function so it is
        # reproducible and the deferred points are not stranded (a lower-sequence
        # deferred point still goes next time; the hub upsert is idempotent).
        if SYNC_BUDGET_BYTES > 0 and envelopes:
            now_ns = time.time() * 1_000_000_000
            candidates = []
            for pid, rec, env in zip(
                [p for p, _ in outbox], [r for _, r in outbox], envelopes
            ):
                meta = (rec.payload or {}).get("_sync_meta", {})
                nov = float(meta.get("novelty", 0.0))
                urg = float(meta.get("urgency", 0.0))
                ts = (rec.payload or {}).get("client_timestamp_ns")
                age_s = max(0.0, (now_ns - float(ts)) / 1e9) if ts else 0.0
                candidates.append({
                    "id": pid,
                    "env": env,
                    "bytes": len(json.dumps(env, default=str).encode("utf-8")),
                    "value": nov + urg,
                    "age_s": age_s,
                    "urgent": meta.get("sync_priority") == "URGENT",
                    "client_sequence": meta.get("client_sequence", 0),
                })
            selected, _deferred = sync_gate.select_batch(
                candidates, SYNC_BUDGET_BYTES, aging_factor=SYNC_AGING_FACTOR
            )
            envelopes = [c["env"] for c in selected]
            ids_in_order = [c["id"] for c in selected]

        if envelopes:
            # Push to the gateway. Mark synced ONLY on ack (invariant 1): on any
            # failure we leave synced == 0, so the point stays pending and on the
            # device — a failed push is never silent data loss.
            try:
                ack = sync_transport.push(device_id, envelopes)
                # Default to NOTHING acked, and skip (mark synced) strictly by the
                # set of ids the hub acknowledged — never by a max-sequence
                # watermark. An ack that omits an id (or omits `acked_ids`
                # entirely) leaves that point pending for the next push, so a
                # partial ack can never strand a lower-sequence point.
                acked = set(ack.get("acked_ids", []))
                mark_ids = [pid for pid in ids_in_order if pid in acked]
                mark_synced(mutable_shard, mark_ids)

                # Persist the device-local redaction record for the acked points
                # (D5 / T7): the raw fact stays in the shard, plus the spans we
                # removed, so the UI can show both the raw and redacted copies.
                for pid in mark_ids:
                    if pid in redaction_records:
                        mutable_shard.update(UpdateOperation.set_payload_by_filter(
                            filter=Filter(must=[HasIdCondition(point_ids={pid})]),
                            payload={"spans": redaction_records[pid]},
                            key="_redaction",
                        ))

                # Append a local OBSERVED event per pushed point (consensus trust
                # view), in push order so URGENT precedes ROUTINE.
                event_log = get_or_create_event_log(device_id)
                now = _utcnow()
                for pid in mark_ids:
                    event_log.append(pid, OBSERVED, device_ts=now)
                pushed_count = len(mark_ids)
            except Exception as e:
                return 0, [f"push failed: {e}"]

    # Drain any queued retractions (their own client_sequence keeps hub order).
    _, retract_errors = _drain_retractions(device_id)
    errors.extend(retract_errors)
    return pushed_count, errors



class PullResponse(BaseModel):
    pulled_count: int
    errors: List[str] = []
    retracted_ids: List[int] = []
    superseded_ids: List[int] = []


@app.post("/devices/{device_id}/pull")
async def pull(device_id: str):
    # Pull flushes the outbox and restores a snapshot — all blocking I/O. Run it
    # off the event loop so it never stalls concurrent local queries.
    return await asyncio.to_thread(_do_pull, device_id)


def _do_pull(device_id: str) -> "PullResponse":
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

    # 1b. Apply the hub's tombstones (P0-2): delete any fact the hub has retracted
    # from BOTH shards and drop its retraction marker. The snapshot only *adds*
    # points, so a retraction can only propagate to a device that already learned
    # the fact through this explicit delete — and it must run even when the hub is
    # now empty (snapshot 204), so we do it before the snapshot step.
    retracted_ids: List[int] = []
    retract_errors: List[str] = []
    try:
        retracted_ids = [int(x) for x in sync_transport.get_retracted(device_id)]
    except Exception as e:  # noqa: BLE001 — surfaced, not swallowed
        retract_errors.append(f"retracted fetch failed: {e}")
    if retracted_ids:
        delete_points(mutable_shard, retracted_ids)
        delete_points(immutable_shard, retracted_ids)

    # 1c. Apply the hub's RESOLVED supersedes (D7 / T3): an operator's resolve
    # flags the losing facts, which the device deletes from BOTH shards (and so
    # their outbox entries), making the superseded value disappear from local
    # search and answers. Mirrors the tombstone path and is likewise applied even
    # when the snapshot is empty. Guarded so a transport double without the method
    # (older stub) still works.
    superseded_ids: List[int] = []
    if hasattr(sync_transport, "get_superseded"):
        try:
            superseded_ids = [int(x) for x in sync_transport.get_superseded(device_id)]
        except Exception as e:  # noqa: BLE001 — surfaced, not swallowed
            retract_errors.append(f"superseded fetch failed: {e}")
    if superseded_ids:
        delete_points(mutable_shard, superseded_ids)
        delete_points(immutable_shard, superseded_ids)

    flush_errors = flush_errors + retract_errors

    # The sync boundary: points synced at/before now are safely on the hub and
    # will come back in the snapshot, so they are the ones we may dedupe.
    sync_timestamp = int(time.time() * 1_000_000_000)

    # 2. Pull + restore a real partial snapshot into the immutable shard.
    manifest = immutable_shard.snapshot_manifest()
    try:
        snapshot_path = sync_transport.pull_snapshot(device_id, manifest)
    except Exception as e:
        return PullResponse(pulled_count=0, errors=flush_errors + [f"pull failed: {e}"], retracted_ids=retracted_ids, superseded_ids=superseded_ids)

    if snapshot_path is None:
        return PullResponse(pulled_count=0, errors=flush_errors, retracted_ids=retracted_ids, superseded_ids=superseded_ids)

    # Refuse to restore a snapshot whose vectors came from a model incompatible
    # with ours (D2 / T5): a cosine search across spaces returns silent garbage.
    # A pre-D2 snapshot carries no stamp and is accepted (back-compatible).
    incoming_stamp = edge_snapshot.read_snapshot_stamp(snapshot_path)
    if incoming_stamp:
        problems = model_stamp.diff_fleet(
            _current_fleet_stamp(), incoming_stamp, min_cosine=STAMP_COSINE
        )
        if problems:
            try:
                os.remove(snapshot_path)
            except OSError:
                pass
            return PullResponse(
                pulled_count=0,
                errors=flush_errors + [f"snapshot stamp mismatch, restore refused: {problems}"],
                retracted_ids=retracted_ids,
                superseded_ids=superseded_ids,
            )

    before = immutable_shard.count(CountRequest())
    try:
        immutable_shard.update_from_snapshot(snapshot_path)
        immutable_shard.optimize()
    except Exception as e:
        return PullResponse(pulled_count=0, errors=flush_errors + [f"snapshot restore failed: {e}"], retracted_ids=retracted_ids, superseded_ids=superseded_ids)
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

    return PullResponse(pulled_count=max(pulled_count, 0), errors=flush_errors, retracted_ids=retracted_ids, superseded_ids=superseded_ids)


def _count_value(count_result) -> int:
    """`count()` returns a CountResult with `.count`; be tolerant of a bare int."""
    return getattr(count_result, "count", count_result)


class RetractResponse(BaseModel):
    retracted: bool
    point_id: int


@app.post("/devices/{device_id}/retract/{point_id}")
async def retract(device_id: str, point_id: int):
    """Withdraw a fact end-to-end (P0-2).

    Resolves the point's corroboration_key from the stored observation (404 if
    the device never held it), deletes it from BOTH local shards, queues a
    persistent retraction in the outbox (its own client_sequence, pushed in order
    with observations), and attempts to notify the hub immediately. An offline /
    failed notify leaves the retraction queued — never a silent `except: pass`.
    """
    if device_id not in device_shards:
        raise HTTPException(status_code=404, detail="unknown device")

    shards = device_shards[device_id]
    mutable_shard = shards["mutable"]
    immutable_shard = shards["immutable"]

    corroboration_key = None
    found = False
    for shard in (mutable_shard, immutable_shard):
        recs = shard.retrieve([point_id], with_payload=True, with_vector=False)
        if recs and recs[0].payload is not None:
            found = True
            corroboration_key = recs[0].payload.get("corroboration_key")
            if corroboration_key:
                break
    if not found or not corroboration_key:
        # Nothing to resolve the key from — refuse rather than ghost-retract.
        raise HTTPException(status_code=404, detail="no local observation for this point")

    now = _utcnow()
    event_log = get_or_create_event_log(device_id)
    event_log.append(point_id, RETRACTED, device_ts=now)
    log_activity(device_id, "retraction", f"retracted point {point_id}", point_id)

    # Withdraw locally: a retracted fact must stop appearing in search/answers.
    delete_points(mutable_shard, [point_id])
    delete_points(immutable_shard, [point_id])

    # Queue the retraction with its own persistent client_sequence so it drains
    # in order with observations.
    shards["client_seq"] += 1
    rseq = shards["client_seq"]
    queue_retraction(mutable_shard, point_id, corroboration_key, rseq)

    # Try to drain the retraction queue now; failures leave it queued (offline).
    _, retract_errors = _drain_retractions(device_id)
    if retract_errors:
        log_activity(device_id, "error", f"retract push deferred: {retract_errors}", point_id)

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


def _drain_retractions(device_id: str):
    """Push pending retraction markers to the hub in client_sequence order.

    Marks a marker synced ONLY on ack (invariant 1): a transport failure leaves
    it pending so a later push (when back online) completes the retraction.
    Returns (drained_count, errors).
    """
    if sync_transport is None:
        return 0, ["no sync transport configured"]
    shards = device_shards.get(device_id)
    if not shards:
        return 0, []
    mutable_shard = shards["mutable"]
    pending = get_pending_retractions(mutable_shard, limit=1000)
    drained = 0
    errors: List[str] = []
    for marker in pending:
        info = (marker.payload or {}).get("_retract", {})
        try:
            sync_transport.retract(
                device_id=device_id,
                point_id=info.get("target_point_id"),
                corroboration_key=info.get("corroboration_key"),
                client_sequence=info.get("client_sequence", 0),
            )
        except Exception as e:  # noqa: BLE001 — surfaced, not swallowed
            errors.append(f"retract push failed: {e}")
            break  # stop on first failure; preserve order for the retry
        mark_retraction_synced(mutable_shard, [marker.id])
        delete_points(mutable_shard, [marker.id])  # drained; drop the marker
        drained += 1
    return drained, errors



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
async def set_device_rogue(device_id: str, request: Request, req: RogueRequest = RogueRequest()):
    """Flag or unflag a device as rogue/compromised (demo beat).

    Gated (A5 / P0-5): 404 unless ENABLE_DEMO_ENDPOINTS is set, and 401 without
    the configured admin token. A production node is not flaggable by default.
    """
    _require_demo(request)
    # Confine the flag to the reserved demo namespace (A5): a demo can never make
    # a REAL device's future captures corrupt.
    demo_dev = _demo_device_id(device_id)
    if req.rogue:
        _rogue_devices.add(demo_dev)
    else:
        _rogue_devices.discard(demo_dev)
    activity = log_activity(demo_dev, "mode_change", f"device rogue mode set to {req.rogue}", None)
    _broadcast_device_event(demo_dev, "activity", activity)
    return {"id": demo_dev, "rogue": req.rogue}


class InjectConflictRequest(BaseModel):
    corroboration_key: str
    assignments: Dict[str, str]


@app.post("/demo/inject-conflict")
async def inject_conflict(request: Request, req: InjectConflictRequest):
    """Inject conflicting observations across devices for Conflict Theater.

    Gated (A5 / P0-5) and confined to the reserved `demo-` namespaces: every
    device id is prefixed and every key namespaced, ids are deterministic md5
    (never Python's randomized `hash()`), and each vector is a REAL embedding of
    the supplied value (never a constant fill). Demo writes therefore cannot
    alter a real device's shards or its `/delta`.
    """
    _require_demo(request)
    now = _utcnow()
    demo_key = _demo_key(req.corroboration_key)
    candidates = []

    # A demo has no image bytes to embed, so it always uses the text adapter and
    # never fabricates a vision vector.
    adapter = next((a for a in adapters if a.modality == "text"), adapters[0])

    # Forward to the cloud gateway if wired (best-effort: demo only).
    if sync_transport:
        try:
            sync_transport.inject_conflict(
                {"corroboration_key": demo_key, "assignments": req.assignments}
            )
        except Exception as exc:  # noqa: BLE001 — a demo forward must never 500
            print(f"demo inject forward failed: {exc}")

    for dev_id, val in req.assignments.items():
        demo_dev = _demo_device_id(dev_id)
        shards = get_or_create_shards(demo_dev)
        elog = get_or_create_event_log(demo_dev)
        # Deterministic md5 point id (never `hash()`), no wall-clock input.
        pid = generate_point_id(demo_key, f"{demo_dev}:{val}")
        d_vec = adapter.embed(val)  # real embedding, not a dummy fill
        s_vec = bm25.embed_document(val)
        pl = {
            "value": val,
            "corroboration_key": demo_key,
            "modality": "text",
            "thumbnail_url": None,
            "client_timestamp_ns": int(time.time() * 1_000_000_000),
            "_demo": 1,
            "_sync_meta": {
                "synced": 1,
                "syncable": 1,
                "verdict": "QUEUE_HIGH",
                "sync_priority": "URGENT",
                "client_sequence": _next_demo_client_seq(),
            },
        }
        pt = Point(id=pid, vector={adapter.name: d_vec, "text_bm25": s_vec}, payload=pl)
        shards['mutable'].update(UpdateOperation.upsert_points(points=[pt]))
        elog.append(pid, OBSERVED, device_ts=now)
        weight = 0.42 if demo_dev in _rogue_devices else 0.88  # hardcode-ok: scripted demo theater
        candidates.append({"value": val, "devices": [demo_dev], "weight": weight})

    unique_vals = set(req.assignments.values())
    outcome_state = "CONFIRMED" if len(unique_vals) <= 1 else "DISPUTED"
    conf = 0.95 if outcome_state == "CONFIRMED" else 0.52  # hardcode-ok: scripted demo theater
    res_val = list(req.assignments.values())[0] if outcome_state == "CONFIRMED" else None

    consensus_frame = {
        "corroboration_key": demo_key,
        "state": outcome_state,
        "confidence": conf,
        "resolved_value": res_val,
        "candidates": candidates,
        "explanation": f"Injected conflict across {len(req.assignments)} devices for {demo_key}",
        "timestamp": now,
    }
    _broadcast_consensus_event(consensus_frame)
    return {"injected": True, "corroboration_key": demo_key, "consensus": consensus_frame}


def _delete_all_points(shard) -> int:
    """Delete every point in a shard (used by demo reset on confined `demo-*`
    shards only)."""
    from qdrant_edge import ScrollRequest
    res = shard.scroll(ScrollRequest(limit=100000, with_payload=False, with_vector=False))
    records = res[0] if isinstance(res, tuple) else res
    ids = [r.id for r in records]
    if ids:
        delete_points(shard, ids)
    return len(ids)


@app.post("/admin/reset-demo")
async def reset_demo(request: Request):
    """Delete ONLY `demo-*` data (A5 / P0-5): drop every demo device's shard
    points and event log, leaving every real device untouched."""
    _require_demo(request)
    demo_ids = [d for d in list(device_shards) if d.startswith(DEMO_DEVICE_PREFIX)]
    deleted = 0
    for dev_id in demo_ids:
        shards = device_shards.pop(dev_id, None)
        if shards:
            deleted += _delete_all_points(shards['mutable'])
            deleted += _delete_all_points(shards['immutable'])
        elog = event_logs.pop(dev_id, None)
        if elog is not None:
            deleted += _delete_all_points(elog.shard)
    # Demo-flagged points that live on a non-prefixed shard (none today, but
    # keeps the guarantee if demo naming changes) are not touched here; the
    # reserved namespace is the isolation boundary.
    _rogue_devices.difference_update(demo_ids)
    return {"reset": True, "demo_devices": demo_ids, "deleted_points": deleted}


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
_ws_consensus_events: Set[WebSocket] = set()
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

        devices.append({
            "id": device_id,
            "name": name,
            "kind": dev_meta.get("kind", "edge node"),
            "zone": dev_meta.get("zone"),
            "latitude": dev_meta.get("latitude"),
            "longitude": dev_meta.get("longitude"),
            "connectivity": network.get_mode(),
            "memory": _memory_dict(mutable_shard, total_count),
            "last_sync_at": last_sync,
            "trust": _device_trust(device_id),
            "activity_sparkline": _activity_sparkline(device_id),
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

    return {
        "id": device_id,
        "name": name,
        "kind": dev_meta.get("kind", "edge node"),
        "zone": dev_meta.get("zone"),
        "latitude": dev_meta.get("latitude"),
        "longitude": dev_meta.get("longitude"),
        "connectivity": network.get_mode(),
        "memory": _memory_dict(mutable_shard, total_count),
        "last_sync_at": last_sync,
        "trust": _device_trust(device_id),
        "activity_sparkline": _activity_sparkline(device_id),
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
                "id": str(r.id),
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
@app.get("/devices/{device_id}/memory/{point_id}")
async def get_memory_point(device_id: str, point_id: str):
    if device_id not in device_shards:
        raise HTTPException(status_code=404, detail="unknown device")
    
    shards = device_shards[device_id]
    mutable_shard = shards['mutable']
    immutable_shard = shards['immutable']
    
    # Try exact integer first, then string prefix / payload match if rounded
    target_ids = []
    try:
        target_ids.append(int(point_id))
    except ValueError:
        pass

    for shard in (mutable_shard, immutable_shard):
        # 1. Exact lookup
        if target_ids:
            recs = shard.retrieve(target_ids, with_payload=True, with_vector=False)
            if recs:
                rec = recs[0]
                payload = rec.payload or {}
                meta = payload.get("_sync_meta", {})
                sync_state_val = "synced" if meta.get("synced") == 1 else ("local_only" if meta.get("syncable") == 0 else "pending")
                return {
                    "point": {
                        "id": str(rec.id),
                        "value": payload.get("value", ""),
                        "modality": payload.get("modality", "text"),
                        "thumbnail_url": payload.get("thumbnail_url"),
                        "zone": payload.get("zone"),
                        "corroboration_key": payload.get("corroboration_key"),
                        "sync_state": sync_state_val,
                        "model": payload.get("model", "text_dense"),
                        "model_version": payload.get("model_version", "bge-small-en-v1.5"),
                        "created_at": payload.get("client_timestamp_ns", 0),
                    },
                    "decision": None,
                    "activity": [],
                    "consensus": None,
                }
        
        # 2. Precision-loss fallback: match point where str(id) starts with target prefix
        from qdrant_edge import ScrollRequest
        pts, _ = shard.scroll(ScrollRequest(limit=200, with_payload=True, with_vector=False))
        for rec in (pts or []):
            if str(rec.id)[:12] == point_id[:12]:
                payload = rec.payload or {}
                meta = payload.get("_sync_meta", {})
                sync_state_val = "synced" if meta.get("synced") == 1 else ("local_only" if meta.get("syncable") == 0 else "pending")
                return {
                    "point": {
                        "id": str(rec.id),
                        "value": payload.get("value", ""),
                        "modality": payload.get("modality", "text"),
                        "thumbnail_url": payload.get("thumbnail_url"),
                        "zone": payload.get("zone"),
                        "corroboration_key": payload.get("corroboration_key"),
                        "sync_state": sync_state_val,
                        "model": payload.get("model", "text_dense"),
                        "model_version": payload.get("model_version", "bge-small-en-v1.5"),
                        "created_at": payload.get("client_timestamp_ns", 0),
                    },
                    "decision": None,
                    "activity": [],
                    "consensus": None,
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
            elif entry["kind"] == "pull_result" and not last_pull:
                # Real count parsed from the logged detail ("pulled N points"),
                # not a zero placeholder (C5).
                received = 0
                m = re.search(r"pulled\s+(\d+)", entry["detail"])
                if m:
                    received = int(m.group(1))
                last_pull = {"at": entry["timestamp"], "points_received": received}

    # last_push comes from the real network push log (measured bytes/duration/
    # accepted), not fabricated zeros (C5).
    last_push = _last_push_summary(device_id)

    return {
        "pending": pending,
        "last_attempt_at": last_attempt,
        "last_success_at": last_success,
        "consecutive_failures": consecutive_failures,
        "next_backoff_ms": _next_backoff_ms(consecutive_failures),
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

    return get_telemetry(model_load_ms=MODEL_LOAD_MS)


# --- GET /cloud/state ---
@app.get("/cloud/state")
async def get_cloud_state(state: Optional[str] = None, zone: Optional[str] = None, modality: Optional[str] = None):
    """Merged trusted picture from the fold (API.md §8)."""
    # 1. Forward to real Cloud Gateway if wired
    if sync_transport is not None:
        try:
            cloud_res = sync_transport.get_cloud_state()
            if cloud_res and isinstance(cloud_res.get("facts"), list):
                # Cache the hub-derived per-device trust so the device list/detail
                # can surface the real number (C5 / B2); it is the only honest
                # trust source.
                dt = cloud_res.get("device_trust")
                if isinstance(dt, dict):
                    _last_hub_device_trust.update(
                        {k: float(v) for k, v in dt.items() if isinstance(v, (int, float))}
                    )
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
        # Offline fallback: the edge CANNOT compute real consensus (that is the
        # hub's trust-weighted fold). We refuse to invent trust weights, a
        # confidence number, or a CONFIRMED verdict here (B3 / P1-3). We surface
        # the distinct local values and their reporting devices, mark the fact
        # UNCORROBORATED, and flag the whole response `degraded`.
        devices_by_val: Dict[str, set] = {}
        for dev_id, v, ts, t_url, m_val in entries:
            devices_by_val.setdefault(v, set()).add(dev_id)

        best_val = max(devices_by_val, key=lambda v: len(devices_by_val[v]))

        latest_ts = max((e[2] for e in entries), default=0)
        if latest_ts > 0:
            upd = datetime.fromtimestamp(latest_ts / 1e9, tz=timezone.utc).isoformat()
        else:
            upd = datetime.now(timezone.utc).isoformat()

        thumb = next((e[3] for e in entries if e[3]), None)
        fact_mod = "vision" if any(e[4] == "vision" for e in entries) else "text"

        if modality:
            if modality == "vision" and fact_mod != "vision" and not thumb:
                continue
            elif modality != "vision" and fact_mod != modality:
                continue

        facts.append({
            "corroboration_key": k,
            "state": "UNCORROBORATED",
            "value": best_val,
            "confidence": None,
            "corroborating_devices": sorted(devices_by_val[best_val]),
            "updated_at": upd,
            "thumbnail_url": thumb,
            "modality": fact_mod,
        })

    # No invented trust offline — the real number is hub-derived (B2).
    return {"facts": facts, "device_trust": {}, "degraded": True}


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
                # Vision records are identified by their payload (modality set at
                # capture, or a thumbnail), never by the device-id prefix (F2).
                if pl.get("modality") == "vision" or pl.get("thumbnail_url"):
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