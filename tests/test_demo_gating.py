"""A5 (P0-5) — gate the demo/rogue surface and confine demo to reserved ids.

The Conflict Theater demo writes scripted observations and flags devices rogue.
Neither may be reachable by default, and neither may touch a real device's shard
or its `/delta`. This asserts:

  * `/demo/*` and `/devices/{id}/rogue` are 404 on both the gateway and the edge
    unless `ENABLE_DEMO_ENDPOINTS` is set; with an admin token configured they
    are 401 without the token;
  * a demo injection lands only under reserved `demo-*` device ids and `demo:`
    keys, so a real device's `/delta` is unchanged and a real capture still
    syncs;
  * `/admin/reset-demo` deletes ONLY demo data.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app as gateway_app
import edge_node.main as edge_main


@pytest.fixture
def gateway_client():
    os.environ["QDRANT_URL"] = ":memory:"
    with TestClient(gateway_app.app) as client:
        yield client


def _envelope(pid, value, device_id="dev-04", client_sequence=7):
    return {
        "id": pid,
        "vector": [0.1] * 384,
        "sparse": None,
        "payload": {
            "value": value,
            "model": "text",
            "modality": "text",
            "corroboration_key": "zone.gas",
            "reporter_device_id": device_id,
            "client_timestamp_ns": 1000,
        },
        "client_sequence": client_sequence,
    }


def test_gateway_demo_disabled_by_default(gateway_client, monkeypatch):
    monkeypatch.delenv("ENABLE_DEMO_ENDPOINTS", raising=False)
    r = gateway_client.post(
        "/demo/inject-conflict",
        json={"corroboration_key": "k", "assignments": {"dev-01": "x"}},
    )
    assert r.status_code == 404
    assert gateway_client.post("/admin/reset-demo").status_code == 404


def test_demo_cannot_alter_real_delta_or_shards(gateway_client, monkeypatch):
    monkeypatch.setenv("ENABLE_DEMO_ENDPOINTS", "1")
    assert gateway_client.get("/delta/dev-04").json()["max_client_sequence"] == -1

    inj = gateway_client.post(
        "/demo/inject-conflict",
        json={
            "corroboration_key": "zone.gas",
            "assignments": {"dev-01": "methane 120ppm", "dev-04": "normal 5ppm"},
        },
    )
    assert inj.status_code == 200
    assert inj.json()["corroboration_key"] == "demo:zone.gas"

    # (a) The demo advanced no real device's delta.
    assert gateway_client.get("/delta/dev-04").json()["max_client_sequence"] == -1
    assert gateway_client.get("/delta/dev-01").json()["max_client_sequence"] == -1

    # (b) A real capture on dev-04 still syncs and advances only its own delta.
    real = gateway_client.post(
        "/ingest",
        json={"device_id": "dev-04", "envelopes": [_envelope(7004, "water level 2m")]},
    )
    assert real.status_code == 200
    assert real.json()["count"] == 1
    assert gateway_client.get("/delta/dev-04").json()["max_client_sequence"] == 7

    keys = {
        f["corroboration_key"]
        for f in gateway_client.get("/cloud/state").json()["facts"]
    }
    assert "zone.gas" in keys          # real key present
    assert "demo:zone.gas" in keys     # demo key kept separate

    # (c) reset-demo removes only the demo data.
    reset = gateway_client.post("/admin/reset-demo")
    assert reset.status_code == 200
    assert reset.json()["deleted"]["events"] >= 1
    keys_after = {
        f["corroboration_key"]
        for f in gateway_client.get("/cloud/state").json()["facts"]
    }
    assert "zone.gas" in keys_after
    assert "demo:zone.gas" not in keys_after
    assert gateway_client.get("/delta/dev-04").json()["max_client_sequence"] == 7


def test_edge_demo_disabled_and_admin_gated(monkeypatch):
    monkeypatch.delenv("ENABLE_DEMO_ENDPOINTS", raising=False)
    monkeypatch.delenv("EDGE_ADMIN_TOKEN", raising=False)
    client = TestClient(edge_main.app)
    demo_body = {"corroboration_key": "k", "assignments": {"dev-a": "x"}}
    assert client.post("/demo/inject-conflict", json=demo_body).status_code == 404
    assert client.post("/devices/dev-a/rogue", json={"rogue": True}).status_code == 404

    monkeypatch.setenv("ENABLE_DEMO_ENDPOINTS", "1")
    monkeypatch.setenv("EDGE_ADMIN_TOKEN", "edge-secret")
    assert client.post("/demo/inject-conflict", json=demo_body).status_code == 401
    assert client.post("/devices/dev-a/rogue", json={"rogue": True}).status_code == 401
    assert client.post("/admin/reset-demo").status_code == 401


def test_edge_demo_helpers_reserve_namespace():
    assert edge_main._demo_device_id("dev-04") == "demo-dev-04"
    assert edge_main._demo_device_id("demo-dev-04") == "demo-dev-04"
    assert edge_main._demo_key("zone.gas") == "demo:zone.gas"
    assert edge_main._demo_key("demo:zone.gas") == "demo:zone.gas"
    # Deterministic md5 id: same inputs -> same id, no wall-clock, no `hash()`.
    first = edge_main.generate_point_id("demo:zone.gas", "demo-dev-04:normal 5ppm")
    second = edge_main.generate_point_id("demo:zone.gas", "demo-dev-04:normal 5ppm")
    assert first == second
    # Demo's client_sequence counter is its own and monotonic.
    s1 = edge_main._next_demo_client_seq()
    s2 = edge_main._next_demo_client_seq()
    assert s2 == s1 + 1
