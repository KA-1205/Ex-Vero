"""A2 (P0-2) — end-to-end retraction acceptance tests.

Three scenarios from the Fix Plan:
  (a) online retract -> fact gone from the hub fold, /snapshot, and a second
      device's search after pull;
  (b) offline retract queued, then applied when the link returns;
  (c) retract with no resolvable key (no local observation) -> 404.

Scenario (a) drives the REAL `GatewayTransport` against the gateway's ASGI app
(TestClient + QDRANT_URL=":memory:") so a signature mismatch cannot hide behind
a stub. Scenario (b) uses `StubTransport` so the offline/online toggle is
deterministic without a network.
"""

import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))

import edge_node.main as main  # noqa: E402
import edge_node.sync_transport as sync_transport_mod  # noqa: E402
from edge_node.registry import load_adapters  # noqa: E402
from edge_node.outbox import get_pending_retractions  # noqa: E402
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


def _init(transport):
    """Fresh edge app state with a transport injected."""
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
    main.sync_transport = transport
    clear_feed()
    from edge_node.conflicts import clear_conflicts

    clear_conflicts()


def _capture(client, device_id, key, value):
    r = client.post(
        f"/devices/{device_id}/capture",
        json={"device_id": device_id, "corroboration_key": key, "value": value},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _query_ids(client, device_id, text):
    r = client.post(f"/devices/{device_id}/query", json={"device_id": device_id, "text": text})
    assert r.status_code == 200, r.text
    return {str(res["id"]) for res in r.json()["results"]}


class _ASGIHttpShim:
    """Routes GatewayTransport's module-level httpx.get/post calls to the gateway
    ASGI app via its TestClient, stripping the fake base URL to a path."""

    def __init__(self, client, base_url: str):
        self._client = client
        self._base = base_url

    def _path(self, url: str) -> str:
        return url[len(self._base):] if url.startswith(self._base) else url

    def get(self, url, **kw):
        kw.pop("timeout", None)
        return self._client.get(self._path(url), **kw)

    def post(self, url, **kw):
        kw.pop("timeout", None)
        return self._client.post(self._path(url), **kw)


def test_online_retract_removes_fact_from_hub_and_second_device(monkeypatch):
    """(a) A captures+pushes; B pulls and finds it; A retracts; B pulls again and
    the fact is gone from B's search and from the hub snapshot/fold."""
    os.environ["QDRANT_URL"] = ":memory:"
    import app as gateway_app

    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            with TestClient(gateway_app.app) as gw_client:  # runs gateway lifespan
                base = "http://gw"
                monkeypatch.setattr(
                    sync_transport_mod, "httpx", _ASGIHttpShim(gw_client, base)
                )
                _init(sync_transport_mod.GatewayTransport(base))
                edge = TestClient(main.app)

                cap = _capture(edge, "dev_a", "collapseA",
                               "structural collapse at main street with people trapped")
                assert cap["verdict"] == "QUEUE_HIGH"
                pid = int(cap["id"])
                assert edge.post("/devices/dev_a/push").json()["pushed_count"] == 1

                # B learns the fact.
                edge.post("/devices/dev_b/query", json={"device_id": "dev_b", "text": "hello"})
                pulled = edge.post("/devices/dev_b/pull").json()
                assert pulled["errors"] == [], pulled
                assert str(pid) in _query_ids(edge, "dev_b", "structural collapse main street")

                # Hub fold confirms it is present before the retract.
                assert gw_client.get("/consensus/collapseA").json()["status"] != "ABSENT"

                # A retracts.
                r = edge.post(f"/devices/dev_a/retract/{pid}")
                assert r.status_code == 200, r.text

                # Hub folds the key to ABSENT and tombstones the fact.
                assert gw_client.get("/consensus/collapseA").json()["status"] == "ABSENT"
                assert pid in gw_client.get("/retracted/dev_a").json()["retracted_ids"]

                # B pulls again: the retraction propagates and the fact disappears.
                pulled2 = edge.post("/devices/dev_b/pull").json()
                assert pid in pulled2["retracted_ids"], pulled2
                assert str(pid) not in _query_ids(edge, "dev_b", "structural collapse main street")
        finally:
            os.chdir(old)


def test_offline_retract_is_queued_then_applied():
    """(b) Retracting while the link is down queues the retraction (the fact is
    gone locally); when the link returns, the push drains it to the hub."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            edge = TestClient(main.app)

            cap = _capture(edge, "dev_off", "collapseO",
                           "structural collapse near the river with people trapped")
            pid = int(cap["id"])
            assert edge.post("/devices/dev_off/push").json()["pushed_count"] == 1
            assert stub.server_has("dev_off", pid)

            # Link goes down; retract is made offline.
            stub.fail = True
            r = edge.post(f"/devices/dev_off/retract/{pid}")
            assert r.status_code == 200, r.text

            # The fact is withdrawn locally, and the retraction is queued pending.
            assert str(pid) not in _query_ids(edge, "dev_off", "structural collapse river")
            mutable = main.device_shards["dev_off"]["mutable"]
            assert len(get_pending_retractions(mutable)) == 1
            assert stub.server_has("dev_off", pid), "retract reached hub while offline"

            # Link returns; a push drains the queued retraction to the hub.
            stub.fail = False
            edge.post("/devices/dev_off/push")
            assert len(get_pending_retractions(mutable)) == 0
            assert not stub.server_has("dev_off", pid)
            assert pid in stub.get_retracted("dev_off")
        finally:
            os.chdir(old)


def test_retract_unknown_point_returns_404():
    """(c) Retracting a point the device never held cannot resolve a key -> 404."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            edge = TestClient(main.app)
            # Materialize the device's shards, then retract an id it never held.
            edge.post("/devices/dev_404/query", json={"device_id": "dev_404", "text": "hello"})
            r = edge.post("/devices/dev_404/retract/9999999999")
            assert r.status_code == 404, r.text
        finally:
            os.chdir(old)


def test_resolve_supersede_removes_losing_value_on_pull(monkeypatch):
    """D7 (T3): two devices report contradictory values for one key; an operator
    resolve on the hub supersedes the loser, and after a pull the losing value is
    gone from the second device's search and the hub reports RESOLVED."""
    os.environ["QDRANT_URL"] = ":memory:"
    import app as gateway_app

    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            with TestClient(gateway_app.app) as gw_client:  # runs gateway lifespan
                base = "http://gw"
                monkeypatch.setattr(
                    sync_transport_mod, "httpx", _ASGIHttpShim(gw_client, base)
                )
                _init(sync_transport_mod.GatewayTransport(base))
                edge = TestClient(main.app)

                key = "zone_c_status"
                winner_text = "structural collapse at main street with people trapped"
                loser_text = "zone c is all clear nothing wrong no damage"

                cap_a = _capture(edge, "dev_a", key, winner_text)
                cap_b = _capture(edge, "dev_b", key, loser_text)
                assert edge.post("/devices/dev_a/push").json()["pushed_count"] == 1
                assert edge.post("/devices/dev_b/push").json()["pushed_count"] == 1
                loser_pid = int(cap_b["id"])

                # dev_b pulls and now holds the loser (its own) + the winner.
                pulled = edge.post("/devices/dev_b/pull").json()
                assert pulled["errors"] == [], pulled
                assert str(loser_pid) in _query_ids(edge, "dev_b", "all clear nothing wrong")

                # Operator resolves to dev_a's value.
                rr = gw_client.post(
                    "/admin/resolve",
                    json={"corroboration_key": key, "value": winner_text, "resolver_id": "op-1"},
                )
                assert rr.status_code == 200, rr.text
                assert loser_pid in rr.json()["superseded_ids"]

                # The hub now reports the key as RESOLVED with the winner value.
                folded = gw_client.get(f"/consensus/{key}").json()
                assert folded["status"] == "RESOLVED"
                assert folded["value"] == winner_text

                # dev_b pulls again: the superseded loser is deleted locally.
                pulled2 = edge.post("/devices/dev_b/pull").json()
                assert loser_pid in pulled2["superseded_ids"], pulled2
                assert str(loser_pid) not in _query_ids(edge, "dev_b", "all clear nothing wrong")
        finally:
            os.chdir(old)
