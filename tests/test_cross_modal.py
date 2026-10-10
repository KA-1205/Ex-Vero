"""Phase 2 / B6 — cross-modal corroboration as an independent vote (P1-6).

Agreement across modalities (a text report and a vision report of the same
claim) adds ONE extra independent supporting leg inside the noisy-OR — a vote,
not a flat +0.15 bump — so confidence rises proportionally and stays bounded. A
single modality on its own earns no bonus and cannot outvote the fleet. Modality
comes only from the event payload; the old `cam-` device-id sniff is gone.
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))

from consensus_fold import OBSERVED, fold_consensus  # noqa: E402

THRESHOLD = 0.66


def _ev(seq, device, value, modality, cluster="CL1", trust=0.7):
    return {
        "corroboration_key": "zone_c.gas",
        "device_id": device,
        "value": value,
        "value_cluster": cluster,
        "modality": modality,
        "event_type": OBSERVED,
        "seq": seq,
        "client_timestamp_ns": seq,
        "device_trust_at_report": trust,
    }


def test_cross_modal_raises_confidence_proportionally():
    same_modality = fold_consensus(
        [_ev(1, "dev_a", "fire in sector 4", "text"),
         _ev(2, "dev_b", "fire in sector 4", "text")],
        threshold=THRESHOLD,
    )
    cross_modality = fold_consensus(
        [_ev(1, "dev_a", "fire in sector 4", "text"),
         _ev(2, "cam_1", "fire in sector 4", "vision")],
        threshold=THRESHOLD,
    )
    assert same_modality["cross_modal"] is False
    assert cross_modality["cross_modal"] is True
    # The extra cross-modal leg genuinely raises confidence.
    assert cross_modality["confidence"] > same_modality["confidence"]
    # ...but stays a probability.
    assert cross_modality["confidence"] <= 1.0


def test_a_single_modality_cannot_outvote_the_fleet():
    events = [
        _ev(1, "dev_a", "all clear", "text", cluster="CLEAR"),
        _ev(2, "dev_b", "all clear", "text", cluster="CLEAR"),
        _ev(3, "dev_c", "all clear", "text", cluster="CLEAR"),
        # One lone vision report of a different claim.
        _ev(4, "cam_9", "major fire", "vision", cluster="FIRE"),
    ]
    out = fold_consensus(events, threshold=THRESHOLD)
    assert out["value"] == "all clear"
    # The winning (text) cluster is single-modality, so it earns no cross-modal
    # bonus — the lone vision claim neither wins nor boosts the loser.
    assert out["cross_modal"] is False


def test_modality_is_not_sniffed_from_device_id():
    # A device named 'cam-7' that reports TEXT must be treated as text — the old
    # startswith('cam-') sniff would have mislabelled it vision and faked a
    # cross-modal boost.
    out = fold_consensus(
        [_ev(1, "cam-7", "smoke rising", "text"),
         _ev(2, "dev_b", "smoke rising", "text")],
        threshold=THRESHOLD,
    )
    assert out["cross_modal"] is False


def test_cross_modal_fold_is_deterministic():
    events = [
        _ev(1, "dev_a", "fire in sector 4", "text"),
        _ev(2, "cam_1", "fire in sector 4", "vision"),
        _ev(3, "dev_c", "fire in sector 4", "text"),
    ]
    canonical = json.dumps(fold_consensus(events, threshold=THRESHOLD), sort_keys=True)
    rng = random.Random(7)
    for _ in range(20):
        shuffled = events[:]
        rng.shuffle(shuffled)
        assert json.dumps(fold_consensus(shuffled, threshold=THRESHOLD), sort_keys=True) == canonical
