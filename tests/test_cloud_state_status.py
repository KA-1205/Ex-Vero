"""Phase 2 / B3 — the dashboard follows the fold's status (P1-3).

/cloud/state used to re-derive CONFIRMED/DISPUTED from the confidence number,
so a SINGLE uncorroborated report (fold status LWW, confidence 0.7) rendered as
CONFIRMED — the hub claiming a lone, unverified report as confirmed truth. The
state must now pass the fold's own status through, with LWW surfaced as a
distinct UNCORROBORATED display state and never as CONFIRMED.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app as gateway_app  # noqa: E402


@pytest.fixture
def gateway_client():
    os.environ["QDRANT_URL"] = ":memory:"
    with TestClient(gateway_app.app) as client:
        yield client


def _ingest(client, device, pid, key, value):
    env = {
        "id": pid,
        "vector": [0.1] * 384,
        "sparse": None,
        "payload": {
            "value": value,
            "model": "text",
            "modality": "text",
            "corroboration_key": key,
            "reporter_device_id": device,
            "client_timestamp_ns": pid,
        },
        "client_sequence": 1,
    }
    r = client.post("/ingest", json={"device_id": device, "envelopes": [env]})
    assert r.status_code == 200


def test_single_report_renders_uncorroborated_not_confirmed(gateway_client):
    key = "zone_q.lone"
    _ingest(gateway_client, "dev-01", 9001, key, "possible gas leak")

    facts = gateway_client.get("/cloud/state").json()["facts"]
    fact = next(f for f in facts if f["corroboration_key"] == key)
    assert fact["state"] == "UNCORROBORATED"
    assert fact["state"] != "CONFIRMED"


def test_corroborated_reports_still_confirm(gateway_client):
    key = "zone_q.many"
    _ingest(gateway_client, "dev-01", 9101, key, "gas leak zone q")
    _ingest(gateway_client, "dev-02", 9102, key, "gas leak zone q")
    _ingest(gateway_client, "dev-03", 9103, key, "gas leak zone q")

    facts = gateway_client.get("/cloud/state").json()["facts"]
    fact = next(f for f in facts if f["corroboration_key"] == key)
    # The fold's status passes through unchanged — three agreeing devices confirm.
    assert fact["state"] == "CONFIRMED"
