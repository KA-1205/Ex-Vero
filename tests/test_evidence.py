"""D10 (T12) — hash-sealed evidence packet + verifier (acceptance tests).

The packet is a reproducible, self-sealing record of how one corroboration_key
folded to its verdict. These tests exercise the REAL gateway endpoint
(`GET /evidence/{key}`), the pure canonical-JSON/sealing module
(`cloud-gateway/evidence.py`), and the standalone verifier
(`tools/verify_packet.py`) — the exact code a judge would run.
"""

import importlib.util
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app as gateway_app  # noqa: E402
import evidence  # noqa: E402


def _load_verifier():
    """Import tools/verify_packet.py by path (it is a script, not a package)."""
    path = os.path.join(os.path.dirname(__file__), "..", "tools", "verify_packet.py")
    spec = importlib.util.spec_from_file_location("verify_packet", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gateway_client():
    os.environ["QDRANT_URL"] = ":memory:"
    with TestClient(gateway_app.app) as client:
        yield client


def _ingest_three_vs_one(gateway_client, key):
    data = [
        ("dev-p1", "methane 120ppm"),
        ("dev-p2", "methane 120ppm"),
        ("dev-p3", "methane 120ppm"),
        ("dev-p4", "all clear"),
    ]
    for i, (dev, val) in enumerate(data, start=1):
        env = {
            "id": 840000 + i,
            "vector": [0.01 * i] * 384,
            "sparse": None,
            "payload": {
                "value": val,
                "model": "text",
                "modality": "text",
                "corroboration_key": key,
                "reporter_device_id": dev,
                "client_timestamp_ns": i,
            },
            "client_sequence": 1,
        }
        r = gateway_client.post("/ingest", json={"device_id": dev, "envelopes": [env]})
        assert r.status_code == 200, r.text


# --- pure canonicalization -----------------------------------------------------

def test_canonical_json_is_sorted_separated_and_float_rounded():
    a = {"b": 1, "a": 0.1234567891, "c": [3.0, {"z": 2.0, "y": 1.0}]}
    b = {"c": [3.0, {"y": 1.0, "z": 2.0}], "a": 0.1234567891, "b": 1}
    # Key order of the input never matters.
    assert evidence.canonical_json(a) == evidence.canonical_json(b)
    # Sorted keys, compact separators, floats to 6 dp.
    assert evidence.canonical_json({"a": 0.1234567891}) == b'{"a":0.123457}'


def test_canonical_bytes_are_stable_across_repeated_builds():
    """Reproducibility: the same inputs yield byte-identical canonical output
    every time (the cross-Python-version guarantee rests on this determinism)."""
    obj = {"x": [1.0000001, 2.0, {"k": "v"}], "y": "héllo"}
    first = evidence.canonical_json(obj)
    for _ in range(100):
        assert evidence.canonical_json(obj) == first


# --- endpoint + verifier -------------------------------------------------------

def test_evidence_endpoint_packet_verifies(gateway_client):
    """D10: a freshly fetched packet passes every verifier check."""
    key = "zone_p.gas"
    _ingest_three_vs_one(gateway_client, key)

    packet = gateway_client.get(f"/evidence/{key}").json()
    assert packet["schema_version"] == evidence.SCHEMA_VERSION
    assert packet["fold"]["status"] in ("CONFIRMED", "DISPUTED")
    assert "hash" in packet

    verifier = _load_verifier()
    results = {c: ok for c, ok, _ in verifier.verify(packet)}
    assert results["SEAL"] is True
    assert results["FOLD"] is True
    assert results["TRUST"] is True
    assert results["MODULE"] is True


def test_tampering_one_byte_event_or_param_is_detected(gateway_client):
    """D10 done-when: changing one byte / event / param breaks verification."""
    key = "zone_p.tamper"
    _ingest_three_vs_one(gateway_client, key)
    verifier = _load_verifier()

    base = gateway_client.get(f"/evidence/{key}").json()
    assert all(ok for _, ok, _ in verifier.verify(base))

    # (a) one byte in the fold value → SEAL fails.
    p = json.loads(json.dumps(base))
    p["fold"]["value"] = p["fold"]["value"] + "X"
    assert dict((c, ok) for c, ok, _ in verifier.verify(p))["SEAL"] is False

    # (b) drop an event → SEAL fails (and the fold would differ too).
    p = json.loads(json.dumps(base))
    p["events"] = p["events"][:-1]
    assert dict((c, ok) for c, ok, _ in verifier.verify(p))["SEAL"] is False

    # (c) nudge a fold parameter → SEAL fails.
    p = json.loads(json.dumps(base))
    p["fold_params"]["threshold"] = p["fold_params"]["threshold"] + 0.01
    assert dict((c, ok) for c, ok, _ in verifier.verify(p))["SEAL"] is False

    # (d) forge a verdict in the fold while leaving the hash stale → FOLD fails
    # even if an attacker also recomputes the SEAL over the forged core.
    p = json.loads(json.dumps(base))
    p["fold"]["status"] = "CONFIRMED" if p["fold"]["status"] != "CONFIRMED" else "DISPUTED"
    p["fold"]["value"] = "fabricated winner"
    p["hash"] = evidence.recompute_hash(p)  # attacker re-seals the forged core
    checks = dict((c, ok) for c, ok, _ in verifier.verify(p))
    assert checks["SEAL"] is True   # the re-seal is internally consistent...
    assert checks["FOLD"] is False  # ...but the events do not produce that verdict.


def test_signature_present_and_verifies_when_admin_secret_set(gateway_client, monkeypatch):
    """D10: with admin keys the packet carries an HMAC signature that verifies;
    the bare hash alone only detects accidental change."""
    monkeypatch.setenv("GATEWAY_ADMIN_TOKEN", "pkt-secret")
    key = "zone_p.signed"
    _ingest_three_vs_one(gateway_client, key)

    packet = gateway_client.get(f"/evidence/{key}").json()
    assert "signature" in packet

    verifier = _load_verifier()
    results = dict((c, ok) for c, ok, _ in verifier.verify(packet, secret="pkt-secret"))
    assert results["SIGN"] is True

    # A wrong secret fails the signature check.
    bad = dict((c, ok) for c, ok, _ in verifier.verify(packet, secret="wrong"))
    assert bad["SIGN"] is False
