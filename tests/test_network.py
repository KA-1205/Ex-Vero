"""Phase 6 — Network layer (real intermittent connectivity) acceptance tests.

Acceptance criterion (docs/30-phases.md, Phase 6):
  A transport interceptor with three global modes (offline/degraded/full) wraps
  ONLY the sync client used by push/pull, never the query/answer path. Effects
  are REAL, not labels:
    * offline  — the connection is refused immediately; sync does not reach the
                 hub, but the device stays fully queryable.
    * degraded — real injected latency, a token-bucket byte cap over the ACTUAL
                 payload bytes, and a real failure rate; URGENT drains first.
    * full     — no injection.

This file proves, with no real network:
  1. Isolation (AGENTS.md §5 invariant 8, backend.md §2.5): the retrieval /
     answer path imports neither the sync transport nor the network simulator —
     a static import-graph check in a clean subprocess.
  2. Query latency under `offline` is unchanged (the sim never gates search).
  3. Under `degraded`, real added latency AND at least one genuinely failed
     request are observed.
  4. Mark-after-ack (invariant 1) holds under a real network failure: a dropped
     push leaves the point pending and on the device.
  5. The byte cap is a real wait (token bucket) and URGENT drains before ROUTINE.
"""

import os
import subprocess
import sys
import tempfile
import time

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
import edge_node.network as network  # noqa: E402
from edge_node.registry import load_adapters  # noqa: E402
from qdrant_edge import (  # noqa: E402
    EdgeConfig,
    EdgeVectorParams,
    Distance,
    EdgeSparseVectorParams,
    Modifier,
    Bm25,
)
from fastapi.testclient import TestClient  # noqa: E402
from edge_node.decision_engine import DecisionEngine, clear_feed  # noqa: E402

from sync_helpers import StubTransport  # noqa: E402


def _init(inner_transport):
    """Fresh app state with the sync transport wrapped by the network layer."""
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
    policy_config = full_config.get("policy", {})
    main.decision_engine = DecisionEngine(policy_config)
    main.TRUST_DECAY = float(policy_config.get("trust_decay", 0.0))
    main.CONFLICT_THRESHOLD = float(full_config.get("conflict", {}).get("similarity_threshold", 0.5))
    main.bm25 = Bm25()
    # The runtime egress: the network interceptor wraps the (stub) sync client.
    main.sync_transport = network.NetworkTransport(inner_transport)
    network.reset()  # back to FULL, default config, empty log
    clear_feed()
    from edge_node.conflicts import clear_conflicts

    clear_conflicts()


def _synced_flag(device_id, point_id):
    shard = main.device_shards[device_id]["mutable"]
    rec = shard.retrieve([point_id], with_payload=True, with_vector=False)
    assert rec, f"point {point_id} vanished from the device"
    return rec[0].payload["_sync_meta"]["synced"]


def _capture_urgent(client, device_id, key, value):
    r = client.post(
        f"/devices/{device_id}/capture",
        json={"device_id": device_id, "corroboration_key": key, "value": value},
    )
    assert r.status_code == 200
    return r.json()


# --- 1. Isolation: the query/answer path imports no transport, no simulator ---

def test_retrieval_path_does_not_import_network_or_transport():
    """Invariant 8, proven architecturally: importing the retrieval module must
    not drag in the network simulator or the sync transport. Checked in a clean
    subprocess so nothing else in this test session pollutes sys.modules."""
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    probe = (
        "import sys; import edge_node.retrieval;"
        "assert 'edge_node.network' not in sys.modules, 'retrieval imports the network simulator';"
        "assert 'edge_node.sync_transport' not in sys.modules, 'retrieval imports the sync transport';"
        "print('ISOLATED')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env
    )
    assert out.returncode == 0, f"isolation broken:\n{out.stdout}\n{out.stderr}"
    assert "ISOLATED" in out.stdout


# --- 2. Query works and is not gated by the sim under offline ---

def test_query_latency_unchanged_under_offline():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init(StubTransport())
            client = TestClient(main.app)
            device_id = "dev_offline_q"
            cap = _capture_urgent(client, device_id, "collapse", "structural collapse on 5th street")
            pid = cap["id"]

            # Baseline query under FULL.
            full_q = client.post(
                f"/devices/{device_id}/query",
                json={"device_id": device_id, "text": "structural collapse"},
            ).json()
            assert any(r["id"] == pid for r in full_q["results"])

            # Flip the wire fully offline with a large injected latency: if query
            # were routed through the sim it would inherit that latency.
            client.post("/network/mode", json={"mode": "degraded"})  # arm config
            network.configure(latency_min_ms=800, latency_max_ms=800, failure_rate=0.0)
            client.post("/network/mode", json={"mode": "offline"})
            assert client.get("/network/mode").json()["mode"] == "offline"

            off_q = client.post(
                f"/devices/{device_id}/query",
                json={"device_id": device_id, "text": "structural collapse"},
            ).json()
            # Still fully queryable offline, and the 800 ms wire latency did NOT
            # bleed into local search.
            assert any(r["id"] == pid for r in off_q["results"])
            assert off_q["latency_ms"] < 400, "query latency inflated by the network sim"
        finally:
            os.chdir(old)


# --- 3. Degraded: real injected latency ---

def test_degraded_injects_real_latency():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_lat"
            _capture_urgent(client, device_id, "k", "gas leak at the north tower")

            client.post("/network/mode", json={"mode": "degraded"})
            # Deterministic, no failures, no byte cap: isolate the latency effect.
            network.configure(latency_min_ms=250, latency_max_ms=250,
                              failure_rate=0.0, byte_rate_bytes_per_sec=0)

            t0 = time.perf_counter()
            resp = client.post(f"/devices/{device_id}/push").json()
            elapsed = time.perf_counter() - t0

            assert resp["pushed_count"] == 1  # succeeds, just slowly
            # At least one injected 250 ms delay actually happened on the wire.
            assert elapsed >= 0.25, f"no real latency injected (elapsed={elapsed:.3f}s)"

            log = network.get_push_log()
            assert log, "a push under degraded must be logged"
            assert log[-1]["mode"] == "degraded"
            assert log[-1]["bytes"] > 0
            assert log[-1]["duration_ms"] >= 250
        finally:
            os.chdir(old)


# --- 3b. Degraded: at least one genuinely failed request (probabilistic wire) ---

def test_degraded_produces_real_failures_over_many_attempts():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_mix"

            client.post("/network/mode", json={"mode": "degraded"})
            # A real coin-flip wire (seeded so the suite is not flaky), no latency
            # so the loop is fast. Some pushes genuinely drop, some genuinely land.
            network.configure(latency_min_ms=0, latency_max_ms=0,
                              failure_rate=0.5, byte_rate_bytes_per_sec=0, seed=1234)

            successes = 0
            failures = 0
            pids = []
            for i in range(25):
                cap = _capture_urgent(client, device_id, f"k{i}", f"structural collapse at site {i}")
                pids.append(cap["id"])
                resp = client.post(f"/devices/{device_id}/push").json()
                if resp["errors"]:
                    failures += 1
                else:
                    successes += 1

            assert failures >= 1, "degraded must genuinely fail some requests"
            assert successes >= 1, "degraded must genuinely let some requests through"
            # Mark-after-ack consistency under a probabilistic wire: a point is
            # marked synced on the device IFF it actually reached the hub. A
            # dropped push never marks, and a delivered one is never left pending.
            for pid in pids:
                on_hub = stub.server_has(device_id, pid)
                marked = _synced_flag(device_id, pid) == 1
                assert on_hub == marked, f"point {pid}: hub={on_hub} but synced={marked}"
        finally:
            os.chdir(old)


# --- 4. Mark-after-ack under a real network failure (invariant 1) ---

def test_mark_after_ack_holds_under_real_network_failure():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_drop"
            cap = _capture_urgent(client, device_id, "k", "structural collapse, people trapped")
            pid = cap["id"]

            client.post("/network/mode", json={"mode": "degraded"})
            # Force the wire to drop every request — a real raised failure, not a
            # label. The push must NOT mark the point synced.
            network.configure(latency_min_ms=0, latency_max_ms=0,
                              failure_rate=1.0, byte_rate_bytes_per_sec=0)

            resp = client.post(f"/devices/{device_id}/push").json()
            assert resp["pushed_count"] == 0
            assert resp["errors"], "a dropped push must report the error"

            # Pending on the device, absent from the hub.
            assert _synced_flag(device_id, pid) == 0
            assert not stub.server_has(device_id, pid)

            # Still fully retrievable offline.
            q = client.post(
                f"/devices/{device_id}/query",
                json={"device_id": device_id, "text": "structural collapse"},
            ).json()
            assert any(r["id"] == pid for r in q["results"])

            # Recover the wire -> the same point now lands (idempotent re-push).
            network.set_mode("full")
            resp2 = client.post(f"/devices/{device_id}/push").json()
            assert resp2["pushed_count"] == 1
            assert _synced_flag(device_id, pid) == 1
            assert stub.server_has(device_id, pid)
        finally:
            os.chdir(old)


# --- 4b. Offline refuses the connection: sync never reaches the hub ---

def test_offline_refuses_and_outbox_accumulates():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_off"
            cap = _capture_urgent(client, device_id, "k", "gas leak, evacuate zone b")
            pid = cap["id"]

            client.post("/network/mode", json={"mode": "offline"})
            resp = client.post(f"/devices/{device_id}/push").json()
            assert resp["pushed_count"] == 0
            assert resp["errors"]
            assert _synced_flag(device_id, pid) == 0
            assert stub.server_count(device_id) == 0
        finally:
            os.chdir(old)


# --- 5. URGENT drains before ROUTINE under degraded ---

def test_urgent_drains_before_routine_under_degraded():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_prio"

            routine = _capture_urgent(client, device_id, "r", "a quiet note about the weather today")
            urgent = _capture_urgent(client, device_id, "u", "structural collapse with people trapped")
            assert routine["verdict"] == "QUEUE_LOW"
            assert urgent["verdict"] == "QUEUE_HIGH"

            client.post("/network/mode", json={"mode": "degraded"})
            network.configure(latency_min_ms=0, latency_max_ms=0,
                              failure_rate=0.0, byte_rate_bytes_per_sec=0)
            client.post(f"/devices/{device_id}/push")

            order = [e["point_id"] for e in stub.events if e["device_id"] == device_id]
            assert order.index(urgent["id"]) < order.index(routine["id"])
        finally:
            os.chdir(old)


# --- 5b. Token bucket byte cap is a real wait ---

def test_token_bucket_is_a_real_byte_cap():
    """Draining more bytes than the bucket holds must block for a measurable,
    proportional time — the byte cap is real, not a recorded number."""
    bucket = network.TokenBucket(bytes_per_sec=1000, capacity=1000)
    # First 1000 bytes are free (bucket starts full).
    t0 = time.perf_counter()
    bucket.consume(1000)
    assert time.perf_counter() - t0 < 0.1
    # Another 500 bytes must wait ~0.5s for the bucket to refill.
    t1 = time.perf_counter()
    bucket.consume(500)
    waited = time.perf_counter() - t1
    assert 0.4 <= waited <= 1.5, f"byte cap wait not proportional (waited={waited:.3f}s)"


def test_network_mode_endpoint_roundtrip_and_rejects_bad_mode():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init(StubTransport())
            client = TestClient(main.app)
            assert client.get("/network/mode").json()["mode"] == "full"
            for mode in ("offline", "degraded", "full"):
                r = client.post("/network/mode", json={"mode": mode})
                assert r.status_code == 200
                assert r.json()["mode"] == mode
            bad = client.post("/network/mode", json={"mode": "banana"})
            assert bad.status_code == 400
        finally:
            os.chdir(old)
