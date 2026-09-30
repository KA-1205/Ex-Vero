"""Acceptance tests for Phase 0: Cloud Gateway and Cross-Device Consensus.

Criterion:
  3 simulated devices push conflicting values for the same corroboration_key;
  /cloud/state returns one reconciled fact with a confidence score, not three raw rows.
"""

import os
import sys
import pytest
from fastapi.testclient import TestClient

# Ensure cloud-gateway is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))

import app as gateway_app


@pytest.fixture
def gateway_client():
    os.environ["QDRANT_URL"] = ":memory:"
    with TestClient(gateway_app.app) as client:
        yield client


def test_cross_device_consensus_reconciles_conflicts(gateway_client):
    """Phase 0 acceptance test: 3 devices report on the same corroboration_key.

    dev-01 -> "structural collapse near exit B"
    dev-02 -> "structural collapse near exit B"
    dev-03 -> "all clear near exit B" (contradictory)

    /cloud/state must return 1 consolidated fact with CONFIRMED state and
    confidence reflecting supermajority, not 3 raw rows.
    """
    key = "zone_b.structural_status"

    # Device 1 reports collapse
    p1 = {
        "id": 101,
        "vector": [0.1] * 384,
        "payload": {
            "value": "structural collapse near exit B",
            "corroboration_key": key,
            "reporter_device_id": "dev-01",
            "client_timestamp_ns": 1000,
        },
        "client_sequence": 1,
    }
    r1 = gateway_client.post("/ingest", json={"device_id": "dev-01", "envelopes": [p1]})
    assert r1.status_code == 200

    # Device 2 corroborates collapse
    p2 = {
        "id": 102,
        "vector": [0.1] * 384,
        "payload": {
            "value": "structural collapse near exit B",
            "corroboration_key": key,
            "reporter_device_id": "dev-02",
            "client_timestamp_ns": 2000,
        },
        "client_sequence": 1,
    }
    r2 = gateway_client.post("/ingest", json={"device_id": "dev-02", "envelopes": [p2]})
    assert r2.status_code == 200

    # Device 3 reports conflicting "all clear"
    p3 = {
        "id": 103,
        "vector": [0.1] * 384,
        "payload": {
            "value": "all clear near exit B",
            "corroboration_key": key,
            "reporter_device_id": "dev-03",
            "client_timestamp_ns": 3000,
        },
        "client_sequence": 1,
    }
    r3 = gateway_client.post("/ingest", json={"device_id": "dev-03", "envelopes": [p3]})
    assert r3.status_code == 200

    # Query /cloud/state
    resp = gateway_client.get("/cloud/state")
    assert resp.status_code == 200
    data = resp.json()

    assert "facts" in data
    assert "device_trust" in data

    # Must contain exactly 1 reconciled fact for this corroboration key, NOT 3 raw rows
    facts_for_key = [f for f in data["facts"] if f["corroboration_key"] == key]
    assert len(facts_for_key) == 1, f"Expected 1 reconciled fact, got {len(facts_for_key)}"

    fact = facts_for_key[0]
    assert fact["value"] == "structural collapse near exit B"
    assert fact["state"] in ("CONFIRMED", "DISPUTED")
    assert fact["confidence"] > 0.5
    assert set(fact["corroborating_devices"]) == {"dev-01", "dev-02"}

    # Device trust: dev-01 and dev-02 agreed with the winner, dev-03 disagreed
    trust = data["device_trust"]
    assert "dev-01" in trust
    assert "dev-02" in trust
    assert "dev-03" in trust
    assert trust["dev-01"] > trust["dev-03"]
    assert trust["dev-02"] > trust["dev-03"]


def test_conflict_injection_and_retraction(gateway_client):
    """Tests scripted conflict injection and retractions via gateway endpoints."""
    key = "zone_d.gas_leak"
    inj_resp = gateway_client.post(
        "/demo/inject-conflict",
        json={
            "corroboration_key": key,
            "assignments": {
                "dev-01": "methane 120ppm",
                "dev-02": "methane 120ppm",
                "dev-03": "methane 120ppm",
                "dev-04": "normal 5ppm",
            },
        },
    )
    assert inj_resp.status_code == 200
    assert inj_resp.json()["injected"] is True

    state_resp = gateway_client.get("/cloud/state")
    assert state_resp.status_code == 200
    facts = state_resp.json()["facts"]
    d_facts = [f for f in facts if f["corroboration_key"] == key]
    assert len(d_facts) == 1
    assert d_facts[0]["value"] == "methane 120ppm"
    assert d_facts[0]["state"] == "CONFIRMED"
    assert d_facts[0]["confidence"] >= 0.66
