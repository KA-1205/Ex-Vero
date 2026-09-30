"""Phase 4 — Partial-snapshot pull (acceptance tests).

Acceptance criterion (docs/30-phases.md, Phase 4):
  Device A captures + pushes fact X; device B (a separate shard set, having
  never captured X) pulls; X is retrievable on B **and** came from B's immutable
  shard (queried directly, not the union) — proving cross-device learning.

The pull path must (Phase 4 build steps + guardrails):
  * flush the outbox to the hub first so the snapshot isn't stale;
  * populate the immutable shard ONLY via `update_from_snapshot` (never a local
    write);
  * dedupe the mutable shard by `client_timestamp_ns <= sync_timestamp` so a
    pushed-then-pulled point isn't duplicated — but NEVER delete a pending point;
  * the query still reads both shards and dedupes by id.

These tests use `StubTransport` (a test double, never wired into the runtime
path). Its `pull_snapshot` builds a REAL Edge snapshot from the hub's facts, so
the restore below exercises the genuine `update_from_snapshot` API with no
network. The real Qdrant Server path is covered by the integration-marked test.
"""

import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
from edge_node.registry import load_adapters  # noqa: E402
from qdrant_edge import (  # noqa: E402
    Bm25,
    Distance,
    EdgeConfig,
    EdgeSparseVectorParams,
    EdgeVectorParams,
    Modifier,
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


def _immutable_has(device_id, point_id):
    """Retrieve a point straight from the device's IMMUTABLE shard (not union)."""
    shard = main.device_shards[device_id]["immutable"]
    rec = shard.retrieve([point_id], with_payload=True, with_vector=False)
    return bool(rec)


def _mutable_has(device_id, point_id):
    shard = main.device_shards[device_id]["mutable"]
    rec = shard.retrieve([point_id], with_payload=True, with_vector=False)
    return bool(rec)


def _capture(client, device_id, key, value):
    r = client.post(
        f"/devices/{device_id}/capture",
        json={"device_id": device_id, "corroboration_key": key, "value": value},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_device_b_learns_fact_it_never_captured():
    """THE acceptance test: A captures + pushes fact X; B (a separate shard set)
    pulls and can answer with X, sourced from B's immutable shard."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)

            # Device A captures a high-urgency fact and pushes it to the hub.
            cap = _capture(client, "dev_a", "collapseX",
                           "structural collapse at main street with people trapped")
            assert cap["verdict"] == "QUEUE_HIGH"  # syncable
            xid = cap["id"]
            pushed = client.post("/devices/dev_a/push").json()
            assert pushed["pushed_count"] == 1
            assert stub.server_has("dev_a", xid)

            # Device B has never seen X. Its shards start empty.
            client.post("/devices/dev_b/query",
                        json={"device_id": "dev_b", "text": "hello"})  # materialize B's shards
            assert not _mutable_has("dev_b", xid)
            assert not _immutable_has("dev_b", xid)

            # B pulls: it learns X from the fleet.
            pulled = client.post("/devices/dev_b/pull").json()
            assert pulled["errors"] == [], pulled
            assert pulled["pulled_count"] >= 1

            # X arrived in B's IMMUTABLE shard (queried directly), and B never
            # captured it, so it is NOT in B's mutable shard — proving the fact
            # was learned from another device, not authored locally.
            assert _immutable_has("dev_b", xid)
            assert not _mutable_has("dev_b", xid)

            # And B can actually answer with it via the normal query path.
            q = client.post("/devices/dev_b/query",
                            json={"device_id": "dev_b", "text": "structural collapse main street"})
            assert q.status_code == 200
            assert any(r["id"] == xid for r in q.json()["results"])
        finally:
            os.chdir(old)


def test_pull_flushes_outbox_before_snapshot():
    """A pull must push pending syncable facts to the hub first, so the snapshot
    the device pulls back reflects its own latest reports (not a stale hub)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)

            cap = _capture(client, "dev_flush", "collapseF",
                           "structural collapse near the river with people trapped")
            xid = cap["id"]
            assert cap["verdict"] == "QUEUE_HIGH"
            # No explicit push. Pull must flush first.
            assert not stub.server_has("dev_flush", xid)

            client.post("/devices/dev_flush/pull")
            assert stub.server_has("dev_flush", xid), "pull did not flush the outbox first"
        finally:
            os.chdir(old)


def test_pull_dedupes_mutable_by_timestamp():
    """A pushed-then-pulled point must not live in both shards: after the pull
    its synced copy is removed from the mutable shard (dedupe by timestamp),
    yet it is still retrievable exactly once (now from the immutable shard)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)

            cap = _capture(client, "dev_dedup", "collapseD",
                           "structural collapse downtown with people trapped")
            xid = cap["id"]
            client.post("/devices/dev_dedup/push")
            assert _mutable_has("dev_dedup", xid)  # still on mutable pre-pull

            client.post("/devices/dev_dedup/pull")

            # Deduped out of the mutable shard, present in the immutable one.
            assert not _mutable_has("dev_dedup", xid)
            assert _immutable_has("dev_dedup", xid)

            # The query returns it exactly once (union + dedupe by id).
            q = client.post("/devices/dev_dedup/query",
                            json={"device_id": "dev_dedup", "text": "structural collapse downtown"})
            ids = [r["id"] for r in q.json()["results"]]
            assert ids.count(xid) == 1
        finally:
            os.chdir(old)


def test_pull_dedup_never_deletes_pending_points():
    """Dedupe must only drop SYNCED points. A pending (KEEP_LOCAL) fact must
    survive a pull on the mutable shard — deleting it would be silent data loss
    (AGENTS.md §5 invariant 1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)

            # A syncable fact (pushed) and a KEEP_LOCAL fact (PII -> never syncs).
            synced = _capture(client, "dev_keep", "collapseK",
                              "structural collapse at the school with people trapped")
            assert synced["verdict"] == "QUEUE_HIGH"
            local = _capture(client, "dev_keep", "localK",
                             "resident contact contact@example.com noted here")
            assert local["verdict"] == "KEEP_LOCAL"
            client.post("/devices/dev_keep/push")

            client.post("/devices/dev_keep/pull")

            # The pending local fact is untouched on the mutable shard.
            assert _mutable_has("dev_keep", local["id"]), "pull deleted a PENDING point"
            # The synced fact was deduped into the immutable shard.
            assert _immutable_has("dev_keep", synced["id"])
        finally:
            os.chdir(old)


def test_query_dedupes_across_shards_by_id():
    """Guardrail: the same id present in BOTH shards is returned once.

    We force the genuine both-shards case: push+pull moves X into the immutable
    shard (and dedupes it out of the mutable one), then we re-capture the same
    fact so the mutable shard holds X again. Now X lives in both shards and the
    query must fold it to a single result — this fails if the union isn't
    deduped by id.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)

            cap = _capture(client, "dev_dup", "collapseU",
                           "structural collapse at the bridge with people trapped")
            xid = cap["id"]
            client.post("/devices/dev_dup/push")
            client.post("/devices/dev_dup/pull")
            # X is now in the immutable shard only.
            assert _immutable_has("dev_dup", xid)
            assert not _mutable_has("dev_dup", xid)

            # Re-capture the identical fact: deterministic id -> mutable holds X
            # again, so X is genuinely present in both shards.
            recap = _capture(client, "dev_dup", "collapseU",
                             "structural collapse at the bridge with people trapped")
            assert recap["id"] == xid
            assert _mutable_has("dev_dup", xid)
            assert _immutable_has("dev_dup", xid)

            q = client.post("/devices/dev_dup/query",
                            json={"device_id": "dev_dup", "text": "structural collapse bridge"})
            ids = [r["id"] for r in q.json()["results"]]
            assert ids.count(xid) == 1
        finally:
            os.chdir(old)


@pytest.mark.integration
def test_real_gateway_partial_snapshot_roundtrip():
    """Integration: A pushes to the real Cloud Gateway (Qdrant Server), then a
    fresh device B pulls a real partial snapshot and learns A's fact. Skipped
    unless GATEWAY_URL is set and reachable (docker-compose up)."""
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
            suffix = f"{os.getpid()}_{int(__import__('time').time())}"
            dev_a = f"dev_a_{suffix}"
            dev_b = f"dev_b_{suffix}"
            cap = _capture(client, dev_a, f"int_{suffix}",
                           "structural collapse verified on site with people trapped")
            xid = cap["id"]
            assert client.post(f"/devices/{dev_a}/push").json()["pushed_count"] == 1
            pulled = client.post(f"/devices/{dev_b}/pull").json()
            assert pulled["errors"] == [], pulled
            assert _immutable_has(dev_b, xid)
        finally:
            os.chdir(old)
