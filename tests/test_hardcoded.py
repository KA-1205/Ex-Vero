"""C5 — hardcoded-value sweep + CI gate (acceptance tests).

Proves two things the brief (§6) requires:
  1. `tools/check_hardcoded.py` passes on the current tree AND actually fails
     when a forbidden literal reappears (so the gate is not a no-op).
  2. The UI numbers that used to be fabricated constants now come from real
     sources: the activity sparkline from the activity ring buffer, the sync
     backoff computed from the real consecutive-failure count, the last-push
     summary from the real network push log, and device trust from the
     hub-derived fold (None when the hub has not computed one).
"""

import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
from edge_node import network  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_checker():
    path = os.path.join(REPO_ROOT, "tools", "check_hardcoded.py")
    spec = importlib.util.spec_from_file_location("check_hardcoded", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# 1. The CI gate passes on the tree and really detects a reintroduced literal.
# --------------------------------------------------------------------------- #
def test_check_hardcoded_passes_on_current_tree():
    checker = _load_checker()
    violations = checker.scan()
    assert violations == [], f"forbidden literals present: {violations}"


def test_check_hardcoded_detects_a_reintroduced_literal(tmp_path, monkeypatch):
    checker = _load_checker()
    # A throwaway module containing one forbidden literal, with no opt-out.
    bad = tmp_path / "bad_module.py"
    bad.write_text("sparkline = [3, 5, 2, 8, 1]\n", encoding="utf-8")
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        checker, "FORBIDDEN", {"bad_module.py": [r"\[3,\s*5,\s*2,\s*8,\s*1\]"]}
    )
    violations = checker.scan()
    assert violations, "checker failed to detect a reintroduced literal"
    assert violations[0][0] == "bad_module.py"


def test_check_hardcoded_respects_opt_out_marker(tmp_path, monkeypatch):
    checker = _load_checker()
    ok = tmp_path / "ok_module.py"
    ok.write_text("weight = 0.88  # hardcode-ok: scripted demo theater\n", encoding="utf-8")
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(checker, "FORBIDDEN", {"ok_module.py": [r"\b0\.88\b"]})
    assert checker.scan() == [], "opt-out marker was not respected"


# --------------------------------------------------------------------------- #
# 2. UI numbers come from real sources, not fabricated constants.
# --------------------------------------------------------------------------- #
def test_sparkline_reflects_real_activity_counts():
    main._activity_buffer.clear()
    # No activity -> all-zero sparkline (never the old [3,5,2,8,1]).
    assert main._activity_sparkline("dev-x", buckets=5) == [0, 0, 0, 0, 0]
    # Seven real entries across five buckets: counts sum to the real total.
    for i in range(7):
        main.log_activity("dev-x", "capture", f"captured {i}", point_id=i)
    spark = main._activity_sparkline("dev-x", buckets=5)
    assert sum(spark) == 7
    assert spark != [3, 5, 2, 8, 1]
    # A different device sees none of dev-x's activity.
    assert main._activity_sparkline("dev-y", buckets=5) == [0, 0, 0, 0, 0]


def test_next_backoff_is_computed_from_failure_count():
    main.SYNC_BACKOFF_BASE_MS = 1000
    main.SYNC_BACKOFF_CAP_MS = 60000
    assert main._next_backoff_ms(0) == 0
    assert main._next_backoff_ms(1) == 1000
    assert main._next_backoff_ms(2) == 2000
    assert main._next_backoff_ms(3) == 4000
    # Grows exponentially but is capped.
    assert main._next_backoff_ms(100) == 60000


def test_last_push_summary_reads_the_real_network_push_log():
    network.reset()
    network.set_mode(network.FULL)

    class _Inner:
        def push(self, device_id, envelopes):
            return {"acked_ids": [e["id"] for e in envelopes], "count": len(envelopes)}

    transport = network.NetworkTransport(_Inner())
    envelopes = [
        {"id": 1, "payload": {"_sync_meta": {"sync_priority": "URGENT"}}},
        {"id": 2, "payload": {"_sync_meta": {"sync_priority": "ROUTINE"}}},
    ]
    transport.push("dev-push", envelopes)

    summary = main._last_push_summary("dev-push")
    assert summary["points_attempted"] == 2
    assert summary["points_accepted"] == 2
    assert summary["points_failed"] == 0
    assert summary["bytes"] > 0  # real serialized payload size, not a zero placeholder
    assert summary["mode"] == network.FULL
    # A device that never pushed has no fabricated summary.
    assert main._last_push_summary("dev-never") == {}


def test_device_trust_is_hub_derived_or_none():
    main._last_hub_device_trust.clear()
    # Unknown device -> None, never a fabricated 0.42/0.88/0.92.
    assert main._device_trust("dev-unknown") is None
    main._last_hub_device_trust["dev-known"] = 0.73
    assert main._device_trust("dev-known") == 0.73
