"""Phase 3 — Real Qdrant Server hub + shard-view outbox (acceptance tests).

Acceptance criterion (docs/30-phases.md, Phase 3):
  With the transport forced to fail mid-push, the point is still `synced == 0`
  and still retrievable on the device; then let the push succeed and the server
  collection contains it.

These tests use `StubTransport` (a test double, never wired into the runtime
path) so the mark-after-ack rule can be forced without a network. The real
Qdrant Server path runs via docker-compose and is exercised by the
integration-marked test at the bottom, which skips when the server is absent.

Invariants covered (AGENTS.md §5):
  1. Mark after push, never before — forced failure leaves the point pending.
  9. Every point records its embedding model + version — asserted on the
     envelope that reaches the hub.
Plus the Phase 0 boolean-filter trap: the outbox stores `synced` as Integer 0/1
so the `scroll(synced == 0)` view actually drains.
"""

import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
from edge_node.registry import load_adapters  # noqa: E402
from edge_node.outbox import SYNCED_KEY, get_outbox  # noqa: E402
from qdrant_edge import (  # noqa: E402
    EdgeConfig,
    EdgeVectorParams,
    Distance,
    EdgeSparseVectorParams,
    Modifier,
    Bm25,
    Filter,
    FieldCondition,
    MatchValue,
    ScrollRequest,
)
from fastapi.testclient import TestClient  # noqa: E402
from edge_node.decision_engine import DecisionEngine, clear_feed  # noqa: E402

from sync_helpers import StubTransport  # noqa: E402


def _init(stub):
    """Fresh app state with a stub transport injected (no network)."""
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
    main.sync_transport = stub
    clear_feed()
    from edge_node.conflicts import clear_conflicts

    clear_conflicts()


def _synced_flag(device_id, point_id):
    """Read `_sync_meta.synced` for a point straight from the mutable shard."""
    shard = main.device_shards[device_id]["mutable"]
    rec = shard.retrieve([point_id], with_payload=True, with_vector=False)
    assert rec, f"point {point_id} vanished from the device"
    return rec[0].payload["_sync_meta"]["synced"]


def test_failed_push_leaves_point_pending_and_on_device():
    """Invariant 1: a forced mid-push failure must not mark the point synced,
    and the fact must still be on the device and queryable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_fail"

            cap = client.post(
                f"/devices/{device_id}/capture",
                json={
                    "device_id": device_id,
                    "corroboration_key": "collapse1",
                    "value": "structural collapse at main street",
                },
            )
            assert cap.status_code == 200
            pid = cap.json()["id"]
            assert cap.json()["verdict"] == "QUEUE_HIGH"  # syncable

            # Force the transport to fail this push.
            stub.fail = True
            push = client.post(f"/devices/{device_id}/push")
            assert push.status_code == 200
            data = push.json()
            assert data["pushed_count"] == 0
            assert data["errors"], "a failed push must report the error"

            # The point is still pending on the device (mark-after-ack).
            assert _synced_flag(device_id, pid) == 0
            # And nothing reached the hub.
            assert not stub.server_has(device_id, pid)
            assert stub.server_count(device_id) == 0

            # And it is still fully retrievable offline.
            q = client.post(
                f"/devices/{device_id}/query",
                json={"device_id": device_id, "text": "structural collapse"},
            )
            assert q.status_code == 200
            assert any(r["id"] == pid for r in q.json()["results"])
        finally:
            os.chdir(old)


def test_successful_push_marks_synced_and_reaches_server():
    """After the transport recovers, the push marks the point synced, the hub
    holds it, and the envelope carries the model version (Invariant 9)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_ok"

            cap = client.post(
                f"/devices/{device_id}/capture",
                json={
                    "device_id": device_id,
                    "corroboration_key": "collapse2",
                    "value": "gas leak reported at zone c",
                },
            )
            pid = cap.json()["id"]

            # First attempt fails -> still pending.
            stub.fail_next = True
            first = client.post(f"/devices/{device_id}/push").json()
            assert first["pushed_count"] == 0
            assert _synced_flag(device_id, pid) == 0

            # Retry succeeds -> synced and on the hub.
            second = client.post(f"/devices/{device_id}/push").json()
            assert second["pushed_count"] == 1
            assert second["errors"] == []
            assert _synced_flag(device_id, pid) == 1
            assert stub.server_has(device_id, pid)

            # Invariant 9: the pushed envelope records the embedding model+version.
            env = stub.store[device_id][pid]
            assert env["payload"]["model"] == main.adapters[0].name
            assert env["payload"]["model_version"] == main.adapters[0].version
        finally:
            os.chdir(old)


def test_repush_is_idempotent_via_delta():
    """A second push after a successful one sends nothing (delta handshake:
    only rows above the hub's max client_sequence go), and never duplicates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_delta"

            client.post(
                f"/devices/{device_id}/capture",
                json={"device_id": device_id, "corroboration_key": "k1",
                      "value": "structural collapse near bridge"},
            )
            first = client.post(f"/devices/{device_id}/push").json()
            assert first["pushed_count"] == 1
            assert stub.server_count(device_id) == 1

            # Nothing new captured -> delta push moves zero rows.
            second = client.post(f"/devices/{device_id}/push").json()
            assert second["pushed_count"] == 0
            assert stub.server_count(device_id) == 1  # no duplicate
        finally:
            os.chdir(old)


def test_keep_local_never_leaves_the_device():
    """A KEEP_LOCAL fact (PII, no urgency) is not syncable: it must never be
    in the outbox nor reach the hub, even across pushes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_local"

            cap = client.post(
                f"/devices/{device_id}/capture",
                json={"device_id": device_id, "corroboration_key": "local1",
                      "value": "resident contact contact@example.com noted"},
            )
            pid = cap.json()["id"]
            assert cap.json()["verdict"] == "KEEP_LOCAL"

            # The outbox view must not include a non-syncable point.
            shard = main.device_shards[device_id]["mutable"]
            outbox_ids = [i for i, _ in get_outbox(shard, limit=100)]
            assert pid not in outbox_ids

            client.post(f"/devices/{device_id}/push")
            assert not stub.server_has(device_id, pid)
            assert stub.server_count(device_id) == 0
        finally:
            os.chdir(old)


def test_urgent_drains_before_routine():
    """Priority ordering: an URGENT fact is pushed ahead of a ROUTINE one."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_prio"

            # ROUTINE first (complete, low-priority), URGENT second.
            routine = client.post(
                f"/devices/{device_id}/capture",
                json={"device_id": device_id, "corroboration_key": "routine1",
                      "value": "a quiet observation about the weather today"},
            )
            urgent = client.post(
                f"/devices/{device_id}/capture",
                json={"device_id": device_id, "corroboration_key": "urgent1",
                      "value": "structural collapse with people trapped"},
            )
            r_id = routine.json()["id"]
            u_id = urgent.json()["id"]
            assert routine.json()["verdict"] == "QUEUE_LOW"
            assert urgent.json()["verdict"] == "QUEUE_HIGH"

            client.post(f"/devices/{device_id}/push")
            # The gateway recorded events in push order: URGENT before ROUTINE.
            order = [e["point_id"] for e in stub.events if e["device_id"] == device_id]
            assert order.index(u_id) < order.index(r_id)
        finally:
            os.chdir(old)


@pytest.mark.integration
def test_real_gateway_and_qdrant_server_roundtrip():
    """Integration: push to the real Cloud Gateway backed by a real Qdrant
    Server (docker-compose). Skipped unless GATEWAY_URL is set and reachable."""
    gateway_url = os.environ.get("GATEWAY_URL")
    if not gateway_url:
        pytest.skip("set GATEWAY_URL (docker-compose up) to run the integration test")
    import httpx

    from edge_node.sync_transport import GatewayTransport

    try:
        httpx.get(f"{gateway_url}/health", timeout=2.0)
    except Exception:
        pytest.skip(f"gateway not reachable at {gateway_url}")

    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init(GatewayTransport(gateway_url))
            client = TestClient(main.app)
            # Unique per run so the delta handshake doesn't dedupe a re-run
            # against a persistent server.
            device_id = f"dev_integration_{os.getpid()}_{int(__import__('time').time())}"
            cap = client.post(
                f"/devices/{device_id}/capture",
                json={"device_id": device_id, "corroboration_key": "int1",
                      "value": "structural collapse verified on site"},
            )
            pid = cap.json()["id"]
            pushed = client.post(f"/devices/{device_id}/push").json()
            assert pushed["pushed_count"] == 1
            assert _synced_flag(device_id, pid) == 1
            # The real server holds it: delta now reports our client_sequence.
            assert main.sync_transport.get_delta(device_id) >= 0
        finally:
            os.chdir(old)
