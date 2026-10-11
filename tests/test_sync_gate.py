"""D3 (T1) — value-per-KB sync gate (pure selection tests).

`sync_gate.select_batch` is a pure function of its inputs: given the pending
envelopes (each with a byte size, a value, an age, an URGENT flag and a
client_sequence) and a byte budget, it decides which to push now and which to
defer. These tests pin the exact selected set and order, the oversize escape
hatch, the "deferred points eventually send" property, and value-per-KB ranking.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from edge_node.sync_gate import fact_score, select_batch  # noqa: E402


def _c(id, bytes, value, *, urgent=False, seq=0, age_s=0.0):
    return {"id": id, "bytes": bytes, "value": value, "urgent": urgent,
            "client_sequence": seq, "age_s": age_s}


def test_exact_selected_set_and_order_under_budget():
    cands = [
        _c("A", 100, 1.0, urgent=True, seq=5),
        _c("B", 100, 0.9, seq=2),
        _c("C", 100, 0.9, seq=1),   # ties with B on score → lower seq wins
        _c("D", 100, 0.5, seq=3),
    ]
    selected, deferred = select_batch(cands, budget_bytes=250, aging_factor=0.0)
    # URGENT first, then score desc, ties by client_sequence asc.
    assert [c["id"] for c in selected] == ["A", "C"]
    assert {c["id"] for c in deferred} == {"B", "D"}


def test_oversize_single_envelope_is_always_sent():
    # One envelope larger than the whole budget can never fit — send it anyway.
    cands = [_c("BIG", 5000, 1.0, urgent=True, seq=1), _c("small", 100, 0.9, seq=2)]
    selected, deferred = select_batch(cands, budget_bytes=250, aging_factor=0.0)
    assert selected[0]["id"] == "BIG"
    # The oversize item consumes the budget, so the small one defers.
    assert {c["id"] for c in deferred} == {"small"}


def test_value_per_kb_small_valuable_beats_large_redundant():
    cands = [
        _c("big", 1000, 1.0, seq=1),    # score 0.001
        _c("small", 10, 0.5, seq=2),    # score 0.05 — denser value
    ]
    selected, deferred = select_batch(cands, budget_bytes=1000, aging_factor=0.0)
    assert selected[0]["id"] == "small"
    assert {c["id"] for c in deferred} == {"big"}


def test_deferred_points_eventually_send_next_round():
    cands = [
        _c("A", 100, 1.0, urgent=True, seq=1),
        _c("B", 100, 0.9, seq=2),
        _c("C", 100, 0.8, seq=3),
    ]
    selected, deferred = select_batch(cands, budget_bytes=150, aging_factor=0.0)
    assert [c["id"] for c in selected] == ["A"]
    # Next push with the same budget drains the deferred ones (not stranded).
    selected2, deferred2 = select_batch(deferred, budget_bytes=250, aging_factor=0.0)
    assert {c["id"] for c in selected2} == {"B", "C"}
    assert deferred2 == []


def test_lower_sequence_point_not_stranded_by_a_higher_one():
    # A low-seq, lower-value point deferred behind higher-value ones still goes
    # on a later push — reordering never strands it (pairs with the ack-set push).
    cands = [_c("hi", 100, 0.9, seq=9), _c("lo", 100, 0.4, seq=1)]
    first, deferred = select_batch(cands, budget_bytes=100, aging_factor=0.0)
    assert [c["id"] for c in first] == ["hi"]
    assert [c["id"] for c in deferred] == ["lo"]
    second, _ = select_batch(deferred, budget_bytes=100, aging_factor=0.0)
    assert [c["id"] for c in second] == ["lo"]


def test_aging_boost_lifts_a_long_deferred_fact():
    # Same value/bytes, but the older fact scores higher once aged.
    fresh = fact_score(0.5, age_s=0.0, nbytes=100, aging_factor=0.01)
    aged = fact_score(0.5, age_s=10_000.0, nbytes=100, aging_factor=0.01)
    assert aged > fresh


def test_non_positive_budget_means_no_limit():
    cands = [_c("A", 100, 1.0, seq=1), _c("B", 100, 0.9, seq=2)]
    selected, deferred = select_batch(cands, budget_bytes=0)
    assert {c["id"] for c in selected} == {"A", "B"}
    assert deferred == []
