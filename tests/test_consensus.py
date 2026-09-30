"""Phase 5 — Multi-device consensus fold (acceptance tests).

Acceptance criteria (docs/30-phases.md, Phase 5):
  * two devices report different values for one key → after reconnect the fold
    yields DISPUTED with BOTH values and confidence < threshold;
  * three agreeing devices → CONFIRMED with confidence rising vs one device;
  * shuffle event insertion order → byte-identical fold output (invariant 6);
  * skew one device's client_timestamp_ns by an hour → resolved value unchanged
    (invariant 5 — the fold orders by hub seq, never the wall-clock);
  * observe → retract → re-observe an identical fact → it stays absent until the
    newer observe (invariant 7 — retraction beats re-observation, can't ghost).

The fold lives in the Cloud Gateway as a pure, dependency-free module
(`cloud-gateway/consensus_fold.py`), so these tests import and exercise the
EXACT code the gateway runs — no network, no stand-in.
"""

import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))

from consensus_fold import (  # noqa: E402
    OBSERVED,
    RETRACTED,
    derive_device_trust,
    fold_consensus,
)

THRESHOLD = 0.66


def _ev(seq, device, value, event_type=OBSERVED, key="zone_c_gas", trust=0.7, ts=None):
    """Build one event. `seq` is the hub-assigned order; `ts` (wall-clock) is
    metadata the fold must ignore."""
    return {
        "corroboration_key": key,
        "device_id": device,
        "value": value,
        "event_type": event_type,
        "seq": seq,
        "client_timestamp_ns": seq * 1_000_000_000 if ts is None else ts,
        "device_trust_at_report": trust,
    }


def test_two_devices_disagree_yields_disputed_with_both_values():
    events = [
        _ev(1, "dev_a", "leak in building 4"),
        _ev(2, "dev_b", "leak in building 7"),
    ]
    out = fold_consensus(events, threshold=THRESHOLD)

    assert out["status"] == "DISPUTED"
    assert out["confidence"] < THRESHOLD
    # Both conflicting values are surfaced — the resolver never silently picks.
    surfaced = {v["value"] for v in out["values"]}
    assert surfaced == {"leak in building 4", "leak in building 7"}
    assert out["live_device_count"] == 2


def test_three_agreeing_devices_confirmed_and_more_confident_than_one():
    one = fold_consensus([_ev(1, "dev_a", "gas leak zone c")], threshold=THRESHOLD)
    three = fold_consensus(
        [
            _ev(1, "dev_a", "gas leak zone c"),
            _ev(2, "dev_b", "gas leak zone c"),
            _ev(3, "dev_c", "gas leak zone c"),
        ],
        threshold=THRESHOLD,
    )

    assert one["status"] == "LWW"  # single device → last-write-wins fast path
    assert three["status"] == "CONFIRMED"
    assert three["value"] == "gas leak zone c"
    # Corroboration must visibly raise confidence.
    assert three["confidence"] > one["confidence"]
    assert three["confidence"] >= THRESHOLD


def test_fold_is_pure_shuffle_order_identical():
    events = [
        _ev(1, "dev_a", "gas leak zone c"),
        _ev(2, "dev_b", "gas leak zone c"),
        _ev(3, "dev_c", "leak in building 7"),
        _ev(4, "dev_a", "gas leak zone c"),  # dev_a re-observes (higher seq)
        _ev(5, "dev_d", "gas leak zone c"),
    ]
    canonical = json.dumps(fold_consensus(events, threshold=THRESHOLD), sort_keys=True)

    rng = random.Random(1234)
    for _ in range(20):
        shuffled = events[:]
        rng.shuffle(shuffled)
        assert (
            json.dumps(fold_consensus(shuffled, threshold=THRESHOLD), sort_keys=True)
            == canonical
        ), "fold output changed with insertion order (invariant 6 broken)"


def test_fold_ignores_device_clock_skew():
    """Give one device two conflicting reports whose hub-seq order and wall-clock
    order DISAGREE: the earlier-seq report carries a clock an hour in the future.
    A wall-clock-ordered resolver would pick the stale value; a seq-ordered fold
    must pick the newer-seq value (invariant 5). This is what makes the test a
    real guard rather than a no-op."""
    hour_ns = 3600 * 1_000_000_000
    events = [
        # seq 1, but its wall-clock is an hour AHEAD of seq 2's.
        _ev(1, "dev_a", "stale value", ts=10 * hour_ns),
        # seq 2 is the genuinely newer report, but its clock reads earlier.
        _ev(2, "dev_a", "current value", ts=1 * hour_ns),
    ]
    out = fold_consensus(events, threshold=THRESHOLD)
    # Ordered by hub seq, dev_a's latest report is "current value".
    assert out["value"] == "current value", out

    # And re-skewing dev_a's clock further must not move the result at all.
    reskewed = [dict(e) for e in events]
    reskewed[0]["client_timestamp_ns"] += 5 * hour_ns
    assert json.dumps(fold_consensus(reskewed, threshold=THRESHOLD), sort_keys=True) == (
        json.dumps(out, sort_keys=True)
    )


def test_observe_retract_reobserve_stays_absent_until_newer_observe():
    e_obs = _ev(1, "dev_a", "gas leak zone c")
    e_ret = _ev(2, "dev_a", None, event_type=RETRACTED)
    e_reobs = _ev(3, "dev_a", "gas leak zone c")

    # Observed → live.
    assert fold_consensus([e_obs], threshold=THRESHOLD)["status"] == "LWW"

    # Retracted (newer seq) → the fact is withdrawn, not lingering.
    retracted = fold_consensus([e_obs, e_ret], threshold=THRESHOLD)
    assert retracted["status"] == "ABSENT"
    assert retracted["value"] is None
    assert retracted["confidence"] == 0.0

    # Re-observed (newest seq) → live again.
    revived = fold_consensus([e_obs, e_ret, e_reobs], threshold=THRESHOLD)
    assert revived["status"] == "LWW"
    assert revived["value"] == "gas leak zone c"

    # Order independence holds through the retraction too.
    assert (
        json.dumps(fold_consensus([e_reobs, e_obs, e_ret], threshold=THRESHOLD), sort_keys=True)
        == json.dumps(revived, sort_keys=True)
    )


def test_derive_device_trust_penalizes_the_disagreeing_device():
    # dev_c disagrees with the corroborated majority across two keys; its derived
    # trust (agreement rate) must land below the agreeing devices'.
    events = [
        _ev(1, "dev_a", "gas leak zone c", key="k1"),
        _ev(2, "dev_b", "gas leak zone c", key="k1"),
        _ev(3, "dev_c", "no leak zone c", key="k1"),
        _ev(4, "dev_a", "fire on 3rd st", key="k2"),
        _ev(5, "dev_b", "fire on 3rd st", key="k2"),
        _ev(6, "dev_c", "quiet on 3rd st", key="k2"),
    ]
    trust = derive_device_trust(events, threshold=THRESHOLD)
    assert trust["dev_a"] == 1.0
    assert trust["dev_b"] == 1.0
    assert trust["dev_c"] < trust["dev_a"], trust


@pytest.mark.integration
def test_real_gateway_folds_two_disagreeing_devices_to_disputed():
    """Integration: two devices push different values for one corroboration_key
    to the real Cloud Gateway; GET /consensus/{key} must fold them to DISPUTED
    with both values. Skipped unless GATEWAY_URL is set (docker-compose up)."""
    gateway_url = os.environ.get("GATEWAY_URL")
    if not gateway_url:
        pytest.skip("set GATEWAY_URL (docker-compose up) to run the integration test")
    import tempfile
    import time

    import httpx

    try:
        httpx.get(f"{gateway_url}/health", timeout=2.0)
    except Exception:
        pytest.skip(f"gateway not reachable at {gateway_url}")

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import yaml

    import edge_node.main as main
    from edge_node.decision_engine import DecisionEngine, clear_feed
    from edge_node.registry import load_adapters
    from edge_node.sync_transport import GatewayTransport
    from fastapi.testclient import TestClient
    from qdrant_edge import (
        Bm25,
        Distance,
        EdgeConfig,
        EdgeSparseVectorParams,
        EdgeVectorParams,
        Modifier,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            cp = str(main.DEFAULT_CONFIG_PATH)
            main.adapters = load_adapters(cp)
            main.edge_config = EdgeConfig(
                vectors={a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine) for a in main.adapters},
                sparse_vectors={"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
                max_search_threads=2, search_pool_core=0,
            )
            main.device_shards = {}
            main.event_logs = {}
            fc = yaml.safe_load(open(cp))
            main.decision_engine = DecisionEngine(fc.get("policy", {}))
            main.TRUST_DECAY = 0.0
            main.CONFLICT_THRESHOLD = 0.01
            main.bm25 = Bm25()
            main.sync_transport = GatewayTransport(gateway_url)
            clear_feed()

            client = TestClient(main.app)
            key = f"zone_c_gas_{os.getpid()}_{int(time.time())}"
            for dev, val in (("dev_a", "structural collapse east with people trapped"),
                             ("dev_b", "structural collapse west with people trapped")):
                client.post(f"/devices/{dev}/capture",
                            json={"device_id": dev, "corroboration_key": key, "value": val})
                client.post(f"/devices/{dev}/push")

            out = httpx.get(f"{gateway_url}/consensus/{key}", timeout=5.0).json()
            assert out["status"] == "DISPUTED", out
            assert len(out["values"]) == 2
        finally:
            os.chdir(old)
