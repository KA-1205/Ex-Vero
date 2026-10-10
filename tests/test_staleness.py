"""Phase 2 / B4 — time-based staleness decay (P1-4).

A report's weight halves every half-life of REAL elapsed time (by the hub clock
`received_at_ns`), not by hub-seq distance. So two stale reports lose to one
fresh correction. The fold stays pure: `now` is injected by the caller.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))

from consensus_fold import OBSERVED, fold_consensus  # noqa: E402

THRESHOLD = 0.66
HALF_LIFE_NS = 3600 * 1_000_000_000  # 1 hour


def _ev(seq, device, value, received_at_ns):
    return {
        "corroboration_key": "zone_c.gas",
        "device_id": device,
        "value": value,
        "event_type": OBSERVED,
        "seq": seq,
        "client_timestamp_ns": received_at_ns,
        "received_at_ns": received_at_ns,
        "device_trust_at_report": 0.7,
    }


def test_two_stale_reports_lose_to_one_fresh_correction():
    now = 100 * HALF_LIFE_NS
    events = [
        # Two devices reported the OLD value ten half-lives ago (weight ~0.001).
        _ev(1, "dev_a", "road is clear", now - 10 * HALF_LIFE_NS),
        _ev(2, "dev_b", "road is clear", now - 10 * HALF_LIFE_NS),
        # One device just corrected it (fresh, full weight).
        _ev(3, "dev_c", "road is flooded", now),
    ]
    out = fold_consensus(events, threshold=THRESHOLD, now_ns=now, half_life_ns=HALF_LIFE_NS)
    assert out["value"] == "road is flooded", out

    # Sanity: WITHOUT time decay the two stale reports would win on raw count.
    stale_wins = fold_consensus(events, threshold=THRESHOLD)
    assert stale_wins["value"] == "road is clear"


def test_same_events_and_now_fold_identically():
    now = 50 * HALF_LIFE_NS
    events = [
        _ev(1, "dev_a", "value one", now - 2 * HALF_LIFE_NS),
        _ev(2, "dev_b", "value two", now - 1 * HALF_LIFE_NS),
        _ev(3, "dev_c", "value two", now),
    ]
    a = json.dumps(
        fold_consensus(events, threshold=THRESHOLD, now_ns=now, half_life_ns=HALF_LIFE_NS),
        sort_keys=True,
    )
    b = json.dumps(
        fold_consensus(list(reversed(events)), threshold=THRESHOLD, now_ns=now, half_life_ns=HALF_LIFE_NS),
        sort_keys=True,
    )
    assert a == b
