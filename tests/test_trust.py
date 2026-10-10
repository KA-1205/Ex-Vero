"""Phase 2 / B2 — real, unforgeable, hub-derived trust (P1-2).

Trust must be a function of the event log, computed on the hub, and must drive
the fold rather than being a display-only number a client can forge:

  * `derive_device_trust` is pure over the log — identical regardless of how
    devices or values are spelled, so no device gains trust from the fold's
    alphabetical tie-break and an exact tie grants nobody trust;
  * a repeatedly-outvoted device ends with measurably lower trust, and that trust
    is used as its weight in the fold (lower weight);
  * a Beta prior and a clamp are available (config);
  * the hub stamps `device_trust_at_report` itself and ignores any client value.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app as gateway_app  # noqa: E402
from consensus_fold import derive_device_trust, fold_consensus, OBSERVED  # noqa: E402

THRESHOLD = 0.66


def _ev(seq, device, value, key, trust=0.7):
    return {
        "corroboration_key": key,
        "device_id": device,
        "value": value,
        "event_type": OBSERVED,
        "seq": seq,
        "client_timestamp_ns": seq * 1_000_000_000,
        "device_trust_at_report": trust,
    }


def test_outvoted_device_has_lower_trust_and_lower_weight():
    # dev_c is outvoted by dev_a + dev_b + dev_d across three keys. Three agreeing
    # devices clear the confidence threshold, so each key folds to CONFIRMED and
    # therefore adjudicates agreement (only CONFIRMED keys count toward trust).
    events = []
    seq = 0
    for i, key in enumerate(("k1", "k2", "k3")):
        seq += 1; events.append(_ev(seq, "dev_a", "real", key))
        seq += 1; events.append(_ev(seq, "dev_b", "real", key))
        seq += 1; events.append(_ev(seq, "dev_d", "real", key))
        seq += 1; events.append(_ev(seq, "dev_c", "bogus", key))

    trust = derive_device_trust(events, threshold=THRESHOLD)
    assert trust["dev_c"] < trust["dev_a"]
    assert trust["dev_c"] < trust["dev_b"]
    assert trust["dev_c"] < trust["dev_d"]

    # Feeding that trust back as the fold weight, dev_c's lone "bogus" value gets
    # near-zero support — its weight really did drop, it is not display-only.
    one_key = [e for e in events if e["corroboration_key"] == "k1"]
    folded = fold_consensus(one_key, threshold=THRESHOLD, device_trust=trust)
    bogus = next(v for v in folded["values"] if v["value"] == "bogus")
    real = next(v for v in folded["values"] if v["value"] == "real")
    assert bogus["support"] < real["support"]
    assert folded["value"] == "real"


def test_disputed_key_grants_no_trust():
    # A 2-vs-1 key folds to DISPUTED (confidence below threshold): the fleet has
    # not corroborated a verdict, so it tells us nothing about who is right and
    # must credit/penalise no device (B2 / P1-2 "exclude DISPUTED/LWW").
    events = [
        _ev(1, "dev_a", "real", "k1"),
        _ev(2, "dev_b", "real", "k1"),
        _ev(3, "dev_c", "bogus", "k1"),
    ]
    # Precondition: this scenario really is DISPUTED, not CONFIRMED.
    assert fold_consensus(events, threshold=THRESHOLD)["status"] == "DISPUTED"
    trust = derive_device_trust(events, threshold=THRESHOLD)
    assert trust == {}


def test_trust_is_independent_of_device_and_value_spelling():
    base = [
        _ev(1, "dev_a", "x", "k1"),
        _ev(2, "dev_b", "x", "k1"),
        _ev(3, "dev_e", "x", "k1"),
        _ev(4, "dev_c", "y", "k1"),
    ]
    # Rename devices and re-spell both values, keeping the agreement STRUCTURE
    # (three devices agree, one dissents → CONFIRMED) identical.
    relabeled = [
        _ev(1, "zzz", "alpha", "k1"),
        _ev(2, "mmm", "alpha", "k1"),
        _ev(3, "qqq", "alpha", "k1"),
        _ev(4, "aaa", "omega", "k1"),
    ]
    t1 = derive_device_trust(base, threshold=THRESHOLD)
    t2 = derive_device_trust(relabeled, threshold=THRESHOLD)
    assert t1  # non-empty: a CONFIRMED key really did adjudicate agreement
    assert sorted(t1.values()) == sorted(t2.values())


def test_an_exact_tie_grants_no_device_trust():
    # One key, a 1-1 tie: unadjudicated, so neither device earns trust (nobody
    # may gain trust from the alphabetical tie-break).
    events = [
        _ev(1, "dev_a", "left", "k1"),
        _ev(2, "dev_b", "right", "k1"),
    ]
    trust = derive_device_trust(events, threshold=THRESHOLD)
    assert trust == {}


def test_beta_prior_and_clamp_are_applied():
    # dev_a agrees on two multi-device keys: agree=2, total=2.
    events = [
        _ev(1, "dev_a", "v", "k1"), _ev(2, "dev_b", "v", "k1"),
        _ev(3, "dev_a", "w", "k2"), _ev(4, "dev_b", "w", "k2"),
    ]
    smoothed = derive_device_trust(events, threshold=THRESHOLD, alpha=1.0, beta=1.0)
    # (2 + 1) / (2 + 1 + 1) = 0.75, down from the un-smoothed 1.0.
    assert smoothed["dev_a"] == pytest.approx(0.75, abs=1e-6)
    clamped = derive_device_trust(
        events, threshold=THRESHOLD, alpha=1.0, beta=1.0, trust_ceiling=0.5
    )
    assert clamped["dev_a"] == pytest.approx(0.5, abs=1e-6)


@pytest.fixture
def gateway_client():
    os.environ["QDRANT_URL"] = ":memory:"
    with TestClient(gateway_app.app) as client:
        yield client


def test_hub_ignores_client_supplied_trust(gateway_client):
    key = "zone_x.gas"
    env = {
        "id": 4242,
        "vector": [0.1] * 384,
        "sparse": None,
        "payload": {
            "value": "gas leak",
            "model": "text",
            "modality": "text",
            "corroboration_key": key,
            "reporter_device_id": "dev-01",
            "client_timestamp_ns": 1000,
            # A forged, inflated trust the hub must NOT believe.
            "device_trust_at_report": 0.99,
        },
        "client_sequence": 1,
    }
    r = gateway_client.post("/ingest", json={"device_id": "dev-01", "envelopes": [env]})
    assert r.status_code == 200

    # The stored event carries the HUB's value (default for a first report),
    # never the client's 0.99.
    flt = gateway_app.models.Filter(
        must=[gateway_app.models.FieldCondition(
            key="point_id", match=gateway_app.models.MatchValue(value=4242))]
    )
    pts, _ = gateway_app.client.scroll(
        gateway_app.EVENTS_COLLECTION, scroll_filter=flt, limit=10,
        with_payload=True, with_vectors=False,
    )
    assert pts
    assert pts[0].payload["device_trust_at_report"] != 0.99
    assert pts[0].payload["device_trust_at_report"] == pytest.approx(
        gateway_app.DEFAULT_DEVICE_TRUST, abs=1e-6
    )
