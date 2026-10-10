"""Phase 2 / B1 — device identity & HMAC authentication (P1-1).

Every write to the hub must be signed by a KNOWN device: an HMAC-SHA256 over
`"{timestamp}.{raw_body}"` with that device's shared secret, verified with
`hmac.compare_digest`. The gateway reads secrets from `GATEWAY_DEVICE_SECRETS`
(env/config, never the repo). With secrets configured:

  * an unsigned or wrong-signature ingest is rejected (401);
  * a device cannot submit a batch under another device's id;
  * fabricated device ids never enter the event log, so they cannot count
    toward consensus.

When no secrets are configured the gateway runs open (the other unit tests),
so this file sets a secret map on the module's env for its lifetime only.
"""

import hashlib
import hmac
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app as gateway_app

KEY = "zone_b.structural_status"
REAL_DEVICE = "dev-01"
REAL_SECRET = "s3cret-dev-01"


def _sign(secret, body_bytes):
    ts = "1700000000.0"
    msg = ts.encode("utf-8") + b"." + body_bytes
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {
        "X-Device-Id": REAL_DEVICE,
        "X-Timestamp": ts,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def _envelope(device_id, pid, value):
    return {
        "id": pid,
        "vector": [0.1] * 384,
        "sparse": None,
        "payload": {
            "value": value,
            "model": "text",
            "modality": "text",
            "corroboration_key": KEY,
            "reporter_device_id": device_id,
            "client_timestamp_ns": 1000,
        },
        "client_sequence": 1,
    }


@pytest.fixture
def authed_client():
    os.environ["QDRANT_URL"] = ":memory:"
    os.environ["GATEWAY_DEVICE_SECRETS"] = json.dumps({REAL_DEVICE: REAL_SECRET})
    try:
        with TestClient(gateway_app.app) as client:
            yield client
    finally:
        os.environ.pop("GATEWAY_DEVICE_SECRETS", None)


def test_unsigned_ingest_is_rejected(authed_client):
    body = {"device_id": REAL_DEVICE, "envelopes": [_envelope(REAL_DEVICE, 101, "collapse")]}
    r = authed_client.post("/ingest", json=body)
    assert r.status_code == 401


def test_wrong_signature_is_rejected(authed_client):
    payload = {"device_id": REAL_DEVICE, "envelopes": [_envelope(REAL_DEVICE, 101, "collapse")]}
    body = json.dumps(payload).encode("utf-8")
    headers = _sign("the-wrong-secret", body)
    r = authed_client.post("/ingest", content=body, headers=headers)
    assert r.status_code == 401


def test_valid_signature_is_accepted(authed_client):
    payload = {"device_id": REAL_DEVICE, "envelopes": [_envelope(REAL_DEVICE, 101, "collapse")]}
    body = json.dumps(payload).encode("utf-8")
    headers = _sign(REAL_SECRET, body)
    r = authed_client.post("/ingest", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_device_cannot_submit_under_another_id(authed_client):
    # Signed correctly by dev-01, but the body claims to be dev-02.
    payload = {"device_id": "dev-02", "envelopes": [_envelope("dev-02", 102, "collapse")]}
    body = json.dumps(payload).encode("utf-8")
    headers = _sign(REAL_SECRET, body)  # X-Device-Id = dev-01
    r = authed_client.post("/ingest", content=body, headers=headers)
    assert r.status_code == 401


def test_fabricated_ids_do_not_count_toward_consensus(authed_client):
    # One real, signed report.
    payload = {"device_id": REAL_DEVICE, "envelopes": [_envelope(REAL_DEVICE, 101, "all clear near exit B")]}
    body = json.dumps(payload).encode("utf-8")
    assert authed_client.post("/ingest", content=body, headers=_sign(REAL_SECRET, body)).status_code == 200

    # Five fabricated devices try to pile on the opposite value — all unsigned /
    # unknown, so all rejected and none reach the log.
    for i in range(5):
        fake = f"ghost-{i}"
        fbody = {"device_id": fake, "envelopes": [_envelope(fake, 200 + i, "structural collapse near exit B")]}
        r = authed_client.post("/ingest", json=fbody)
        assert r.status_code == 401

    out = authed_client.get(f"/consensus/{KEY}").json()
    # Only the one real device is in the fold; the ghosts never counted.
    assert out["live_device_count"] == 1
    assert out["value"] == "all clear near exit B"


def test_admin_endpoint_requires_token(monkeypatch):
    os.environ["QDRANT_URL"] = ":memory:"
    os.environ["GATEWAY_ADMIN_TOKEN"] = "admin-sekret"
    # A5: the demo surface is off unless explicitly enabled; enable it here so
    # this test exercises the admin-token layer, not the demo gate.
    os.environ["ENABLE_DEMO_ENDPOINTS"] = "1"
    try:
        with TestClient(gateway_app.app) as client:
            payload = {"corroboration_key": "k", "assignments": {"dev-01": "x", "dev-02": "x"}}
            # No admin token -> rejected.
            assert client.post("/demo/inject-conflict", json=payload).status_code == 401
            # Correct admin token -> allowed.
            ok = client.post(
                "/demo/inject-conflict", json=payload,
                headers={"X-Admin-Token": "admin-sekret"},
            )
            assert ok.status_code == 200
    finally:
        os.environ.pop("GATEWAY_ADMIN_TOKEN", None)
        os.environ.pop("ENABLE_DEMO_ENDPOINTS", None)
