"""Phase 8 — Eviction + Inspection endpoints acceptance tests.

Acceptance criterion (docs/30-phases.md, Phase 8):
  - Overfill the cap with pending points → none evicted
  - Overfill with synced points → oldest evicted and count drops after optimize()
  - Each endpoint returns the documented shape
  - Telemetry values are read from the real process, not constants

Invariants touched (AGENTS.md §5):
  - Invariant 3: Eviction never touches unsynced points.
  - Invariant 2: optimize() before you believe a write.
  - Invariant 8: Query path never touches the network layer.
"""

import os
import sys
import tempfile
import time
import subprocess

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
from edge_node.registry import load_adapters  # noqa: E402
from edge_node.decision_engine import DecisionEngine, clear_feed  # noqa: E402
from qdrant_edge import (  # noqa: E402
    EdgeConfig,
    EdgeVectorParams,
    Distance,
    EdgeSparseVectorParams,
    Modifier,
    Bm25,
    CountRequest,
    Filter,
    FieldCondition,
    MatchValue,
)
from fastapi.testclient import TestClient  # noqa: E402

from sync_helpers import StubTransport  # noqa: E402


def _init(max_local_points: int = 500, inner_transport=None):
    """Fresh app state with configurable memory cap."""
    config_path = str(main.DEFAULT_CONFIG_PATH)
    main.adapters = load_adapters(config_path)
    vectors = {a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine) for a in main.adapters}
    sparse_vectors = {"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)}
    main.edge_config = EdgeConfig(
        vectors=vectors,
        sparse_vectors=sparse_vectors,
        max_search_threads=2,
        search_pool_core=0,
    )
    main.device_shards = {}
    main.event_logs = {}
    with open(config_path) as f:
        full_config = yaml.safe_load(f)
    # Override memory cap for testing
    full_config["memory"] = {"max_local_points": max_local_points}
    policy_config = full_config.get("policy", {})
    main.decision_engine = DecisionEngine(policy_config)
    main.TRUST_DECAY = float(policy_config.get("trust_decay", 0.0))
    main.CONFLICT_THRESHOLD = float(full_config.get("conflict", {}).get("similarity_threshold", 0.5))
    main.MAX_LOCAL_POINTS = max_local_points
    main.bm25 = Bm25()
    main.sync_transport = inner_transport or StubTransport()
    main.generators = []
    import edge_node.network as network
    network.reset()
    clear_feed()
    from edge_node.conflicts import clear_conflicts
    clear_conflicts()
    # Clear activity buffer
    main._activity_buffer.clear()


def _capture_urgent(client, device_id, key, value):
    r = client.post(
        f"/devices/{device_id}/capture",
        json={"device_id": device_id, "corroboration_key": key, "value": value},
    )
    assert r.status_code == 200
    return r.json()


def _capture_routine(client, device_id, key, value):
    # Low urgency, low novelty -> QUEUE_LOW
    r = client.post(
        f"/devices/{device_id}/capture",
        json={"device_id": device_id, "corroboration_key": key, "value": value},
    )
    assert r.status_code == 200
    return r.json()


def _get_synced_count(device_id):
    shard = main.device_shards[device_id]["mutable"]
    res = shard.count(CountRequest(filter=Filter(must=[FieldCondition(key="_sync_meta.synced", match=MatchValue(value=1))])))
    return getattr(res, "count", res)


def _get_total_count(device_id):
    shard = main.device_shards[device_id]["mutable"]
    res = shard.count(CountRequest())
    return getattr(res, "count", res)


# --------------------------------------------------------------------------- #
# 1. Eviction: pending points are NEVER evicted (Invariant 3)
# --------------------------------------------------------------------------- #
def test_eviction_never_touches_pending_points():
    """Overfill the cap with pending (unsynced) points → none evicted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            # Tiny cap: 5 points
            _init(max_local_points=5)
            client = TestClient(main.app)
            device_id = "dev_evict_pending"

            # Capture 10 URGENT points (synced=0, syncable=1) - all pending
            for i in range(10):
                _capture_urgent(client, device_id, f"k{i}", f"structural collapse at site {i}")

            # All 10 points should be in the mutable shard (none evicted)
            total = _get_total_count(device_id)
            assert total == 10, f"Expected 10 points, got {total} - pending points were evicted!"

            # None should be synced
            synced = _get_synced_count(device_id)
            assert synced == 0, f"Expected 0 synced, got {synced}"
        finally:
            os.chdir(old)


# --------------------------------------------------------------------------- #
# 2. Eviction: synced points ARE evicted (oldest first)
# --------------------------------------------------------------------------- #
def test_eviction_removes_oldest_synced_points():
    """Overfill with synced points → oldest evicted and count drops after optimize()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            # Tiny cap: 5 points
            _init(max_local_points=5)
            client = TestClient(main.app)
            device_id = "dev_evict_synced"

            # Capture 10 URGENT points
            pids = []
            for i in range(10):
                cap = _capture_urgent(client, device_id, f"k{i}", f"structural collapse at site {i}")
                pids.append(cap["id"])

            # Total should be 10 (no eviction yet - all pending)
            assert _get_total_count(device_id) == 10

            # Now mark them all as synced (simulate successful push)
            shard = main.device_shards[device_id]["mutable"]
            from edge_node.outbox import mark_synced
            mark_synced(shard, pids)
            shard.optimize()

            # Now all 10 are synced - verify
            synced = _get_synced_count(device_id)
            assert synced == 10, f"Expected 10 synced, got {synced}"

            # Now trigger eviction by adding one more point (which will be pending)
            # But wait - we need to test eviction of synced points when over cap
            # The eviction runs after each capture. Since we have 10 synced and cap is 5,
            # the next capture should trigger eviction of 5 oldest synced points.

            # Actually, eviction happens on capture. Let's add more captures
            # and check that oldest synced are evicted.
            # But the new capture will be pending, not synced.
            # Let's use the pull path which calls eviction, or directly test evict_by_count_and_optimize.

            from edge_node.eviction import evict_by_count_and_optimize
            evicted = evict_by_count_and_optimize(shard, 5)
            assert evicted == 5, f"Expected 5 evicted, got {evicted}"

            # After eviction, total should be 5 (the 5 newest synced)
            total = _get_total_count(device_id)
            assert total == 5, f"Expected 5 after eviction, got {total}"

            # The remaining should be the 5 newest (highest client_sequence)
            # which correspond to the last 5 captures
            remaining_ids = set()
            from qdrant_edge import ScrollRequest
            for rec in shard.scroll(ScrollRequest(limit=100, with_payload=False, with_vector=False))[0]:
                remaining_ids.add(rec.id)
            # The 5 newest should remain
            assert remaining_ids == set(pids[-5:]), f"Wrong points evicted: remaining={remaining_ids}, expected={set(pids[-5:])}"
        finally:
            os.chdir(old)


# --------------------------------------------------------------------------- #
# 3. Telemetry: reads real process metrics, not constants
# --------------------------------------------------------------------------- #
def test_telemetry_reads_real_process_metrics():
    """Telemetry endpoint returns real CPU/RAM/latency from the process."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)
            device_id = "dev_telemetry"

            # Capture a point so device exists
            _capture_urgent(client, device_id, "k", "test fact")

            # Call telemetry endpoint
            resp = client.get(f"/devices/{device_id}/telemetry")
            assert resp.status_code == 200, resp.text
            data = resp.json()

            # Check all required fields per API.md §11
            assert "target_label" in data
            assert "cpu_pct" in data
            assert "ram_mb" in data
            assert "ram_limit_mb" in data
            assert "model_load_ms" in data
            assert "query_latency_p50_ms" in data
            assert "query_latency_p95_ms" in data

            # Target label should mention emulated constrained target
            assert "emulated constrained target" in data["target_label"]

            # Values should be real (not constants like 0 or fixed numbers)
            # RAM should be > 0 for a running process
            assert data["ram_mb"] > 0, "RAM should be > 0 for running process"
            # CPU can be 0 if idle, but shouldn't be a fake constant
            # Latency percentiles start at 0 before any queries
            assert data["query_latency_p50_ms"] >= 0
            assert data["query_latency_p95_ms"] >= 0
        finally:
            os.chdir(old)


# --------------------------------------------------------------------------- #
# 4. Endpoint shapes match API.md
# --------------------------------------------------------------------------- #
def test_devices_endpoint_shape():
    """GET /devices returns fleet state with correct shape (API.md §2)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)

            resp = client.get("/devices")
            assert resp.status_code == 200
            data = resp.json()
            assert "devices" in data
            assert isinstance(data["devices"], list)
        finally:
            os.chdir(old)


def test_device_detail_endpoint_shape():
    """GET /devices/{id} returns device detail with correct shape (API.md §2)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)
            device_id = "dev_detail"
            _capture_urgent(client, device_id, "k", "test fact")

            resp = client.get(f"/devices/{device_id}")
            assert resp.status_code == 200
            data = resp.json()
            # Check Device shape (API.md §1)
            assert "id" in data
            assert "name" in data
            assert "connectivity" in data
            assert "memory" in data
            assert "used" in data["memory"]
            assert "cap" in data["memory"]
            assert "last_sync_at" in data
            assert "trust" in data
            assert "activity_sparkline" in data
        finally:
            os.chdir(old)


def test_memory_list_endpoint_shape():
    """GET /devices/{id}/memory returns MemoryBrowser shape (API.md §5)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)
            device_id = "dev_memory"
            _capture_urgent(client, device_id, "k1", "gas leak at zone A")
            _capture_urgent(client, device_id, "k2", "fire at zone B")

            resp = client.get(f"/devices/{device_id}/memory")
            assert resp.status_code == 200
            data = resp.json()
            assert "points" in data
            assert "total" in data
            assert len(data["points"]) == 2
            # Check MemoryPoint shape
            p = data["points"][0]
            assert "id" in p
            assert "value" in p
            assert "modality" in p
            assert "sync_state" in p
            assert "model" in p
            assert "model_version" in p
            assert "created_at" in p
        finally:
            os.chdir(old)


def test_memory_point_detail_endpoint_shape():
    """GET /devices/{id}/memory/{point_id} returns full detail (API.md §5)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)
            device_id = "dev_memory_detail"
            cap = _capture_urgent(client, device_id, "k1", "gas leak at zone A")
            pid = cap["id"]

            resp = client.get(f"/devices/{device_id}/memory/{pid}")
            assert resp.status_code == 200
            data = resp.json()
            assert "point" in data
            assert "decision" in data
            assert "activity" in data
            assert "consensus" in data
        finally:
            os.chdir(old)


def test_sync_status_endpoint_shape():
    """GET /devices/{id}/sync returns sync status (API.md §7)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)
            device_id = "dev_sync"
            _capture_urgent(client, device_id, "k", "test")

            resp = client.get(f"/devices/{device_id}/sync")
            assert resp.status_code == 200
            data = resp.json()
            assert "pending" in data
            assert "URGENT" in data["pending"]
            assert "ROUTINE" in data["pending"]
            assert "HELD" in data["pending"]
            assert "last_attempt_at" in data
            assert "last_success_at" in data
            assert "consecutive_failures" in data
            assert "next_backoff_ms" in data
            assert "last_push" in data
            assert "last_pull" in data
        finally:
            os.chdir(old)


def test_activity_endpoint_shape():
    """GET /devices/{id}/activity returns ring buffer (API.md §6)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)
            device_id = "dev_activity"
            _capture_urgent(client, device_id, "k", "test fact")

            resp = client.get(f"/devices/{device_id}/activity")
            assert resp.status_code == 200
            data = resp.json()
            assert "entries" in data
            assert isinstance(data["entries"], list)
            if data["entries"]:
                e = data["entries"][0]
                assert "device_id" in e
                assert "kind" in e
                assert "detail" in e
                assert "point_id" in e
                assert "timestamp" in e
        finally:
            os.chdir(old)


def test_cloud_state_endpoint_shape():
    """GET /cloud/state returns merged trusted picture (API.md §8)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)

            resp = client.get("/cloud/state")
            assert resp.status_code == 200
            data = resp.json()
            assert "facts" in data
            assert "device_trust" in data
            assert isinstance(data["facts"], list)
            assert isinstance(data["device_trust"], dict)
        finally:
            os.chdir(old)


# --------------------------------------------------------------------------- #
# 5. Isolation: query/answer path never touches network layer (Invariant 8)
# --------------------------------------------------------------------------- #
def test_retrieval_answer_isolation():
    """Static import check: retrieval and answer modules import no transport/sim."""
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    for mod in ("edge_node.retrieval", "edge_node.answer"):
        probe = (
            f"import sys; import {mod};"
            "assert 'edge_node.network' not in sys.modules, f'{mod} imports network';"
            "assert 'edge_node.sync_transport' not in sys.modules, f'{mod} imports sync_transport';"
            "print('ISOLATED')"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, env=env
        )
        assert out.returncode == 0, f"{mod} isolation broken:\n{out.stdout}\n{out.stderr}"
        assert "ISOLATED" in out.stdout


# --------------------------------------------------------------------------- #
# 6. Query latency is recorded for telemetry percentiles
# --------------------------------------------------------------------------- #
def test_query_latency_recorded_for_telemetry():
    """Query latency is recorded and appears in telemetry percentiles."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            client = TestClient(main.app)
            device_id = "dev_latency"

            _capture_urgent(client, device_id, "k", "structural collapse at zone A")

            # Run a few queries
            for _ in range(5):
                client.post(f"/devices/{device_id}/query", json={"text": "structural collapse", "answer": False})

            # Check telemetry has percentiles
            resp = client.get(f"/devices/{device_id}/telemetry")
            data = resp.json()
            # After queries, p50/p95 should be > 0
            assert data["query_latency_p50_ms"] > 0, "p50 should be recorded"
            assert data["query_latency_p95_ms"] > 0, "p95 should be recorded"
            assert data["query_latency_p95_ms"] >= data["query_latency_p50_ms"]
        finally:
            os.chdir(old)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])