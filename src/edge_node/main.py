import os
import hashlib
from typing import List, Dict
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import fastembed
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
from .adapter import Adapter
from .registry import load_adapters
from .decision_engine import DecisionEngine, log_decision, get_feed, clear_feed, load_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "disaster-response.yaml"

app = FastAPI()

# Global variables
adapters: List[Adapter] = None
edge_config: EdgeConfig = None
decision_engine: DecisionEngine = None
bm25: Bm25 = None
device_shards: Dict[str, dict] = {}  # device_id -> {'mutable': shard, 'immutable': shard}
SHARD_BASE_PATH = "./shards"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global adapters, edge_config, decision_engine, bm25

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

    # Initialize BM25 embedder (default config)
    bm25 = Bm25()

    print(f"Loaded {len(adapters)} adapters: {[a.name for a in adapters]}")
    print(f"Loaded policy: {policy_config}")
    print("BM25 initialized")
    yield


app = FastAPI(lifespan=lifespan)


class CaptureRequest(BaseModel):
    device_id: str
    corroboration_key: str
    value: str  # the text to capture


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
        'immutable': immutable_shard
    }
    return device_shards[device_id]


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
        "model_version": fastembed.__version__,
        "corroboration_key": request.corroboration_key,
        "modality": adapter.modality
    }

    # Run decision engine
    verdict, reason = decision_engine.evaluate(payload, dense_vector, mutable_shard, adapter)
    # Log decision for feed
    log_decision(device_id, payload, verdict, reason)

    # Compute BM25 sparse vector for the document (embed_document)
    sparse_vector = bm25.embed_document(request.value)

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

    return {"id": point_id, "payload": payload, "verdict": verdict, "reason": reason}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)