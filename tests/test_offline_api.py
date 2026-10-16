"""Real shard and API wiring regressions with explicit hash-embedding test doubles."""

import sys
from pathlib import Path
import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import edge_node.main as main


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = yaml.safe_load(main.DEFAULT_CONFIG_PATH.read_text())
    config["models"] = {
        "embedders": [
            {
                "name": "text",
                "modality": "text",
                "provider": "fake",
                "dim": 64,
                "version": "test-v1",
            }
        ]
    }
    config["hub"] = {}
    config["network"] = {"mode": "full"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(main, "SHARD_BASE_PATH", str(tmp_path / "shards"))
    monkeypatch.setattr(main, "device_shards", {})
    monkeypatch.setattr(main, "event_logs", {})
    monkeypatch.setattr(main, "_bench_seeded", set())
    with TestClient(main.app) as api:
        yield api
    main.device_shards.clear()
    main.event_logs.clear()


def test_capture_search_views_and_offline_mode(client):
    assert client.get("/health").json()["status"] == "ok"
    payload = {
        "corroboration_key": "shelter",
        "value": "structural collapse at north shelter",
    }
    r = client.post("/devices/test/capture", json=payload)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert (
        client.post(
            "/devices/test/query", json={"text": payload["value"], "answer": False}
        ).status_code
        == 200
    )
    assert client.get("/devices/test/memory").json()["total"] == 1
    assert client.get(f"/devices/test/memory/{pid}").status_code == 200
    for path in ["feed", "sync", "activity", "telemetry", "conflicts", "trend"]:
        r = client.get("/devices/test/" + path)
        assert r.status_code == 200, (path, r.text)
    assert client.get("/devices").status_code == 200
    assert client.get("/devices/test").status_code == 200
    assert client.post("/network/mode", json={"mode": "offline"}).status_code == 200
    assert client.get("/network/mode").json()["mode"] == "offline"
    assert (
        client.post(
            "/devices/test/query", json={"text": payload["value"], "answer": False}
        ).status_code
        == 200
    )


def test_missing_hub_errors_and_demo_gate(client):
    for action in ["push", "pull"]:
        r = client.post("/devices/test/" + action)
        assert r.status_code == 200
        assert r.json()["errors"]
    r = client.post(
        "/demo/inject-conflict",
        json={"corroboration_key": "t", "assignments": {"d": "v"}},
    )
    assert r.status_code == 404


def test_resolver_reports_consistent_counts(client):
    r = client.get("/benchmark/resolver-vs-lww")
    assert r.status_code == 200
    j = r.json()
    assert j["resolver_accuracy"] == pytest.approx(
        j["resolver_correct"] / j["scenarios"], abs=1e-4
    )


def test_retry_acknowledgement_and_snapshot_pull(client, monkeypatch):
    from sync_helpers import StubTransport

    stub = StubTransport()
    stub.fail_next = True
    monkeypatch.setattr(main, "sync_transport", stub)
    r = client.post(
        "/devices/source/capture",
        json={
            "corroboration_key": "sync",
            "value": "structural collapse at sync shelter",
        },
    )
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.post("/devices/source/push").json()["errors"]
    assert not stub.server_has("source", pid)
    r = client.post("/devices/source/push")
    assert r.json()["pushed_count"] == 1, r.text
    assert stub.server_has("source", pid)
    assert client.post("/devices/source/push").json()["pushed_count"] == 0
    r = client.post("/devices/receiver/pull")
    assert r.status_code == 200 and not r.json()["errors"], r.text
    assert client.get("/devices/receiver/memory").json()["total"] >= 1
