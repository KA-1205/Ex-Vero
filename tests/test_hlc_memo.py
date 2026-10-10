"""D12 (T11) — the HLC decision memo must exist and walk the three required
cases with concrete event sequences.

This is a documentation deliverable, so the test guards *structure*: that the
memo is present, covers the three ordering scenarios the task names, grounds
each in a concrete event sequence, and reaches a cost + recommendation. It
fails before the memo exists and passes once it does.
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMO = os.path.join(REPO_ROOT, "docs", "decisions", "hlc.md")


def _memo_text():
    assert os.path.isfile(MEMO), "docs/decisions/hlc.md is missing"
    with open(MEMO, "r", encoding="utf-8") as fh:
        return fh.read().lower()


def test_memo_exists():
    assert os.path.isfile(MEMO)


def test_memo_covers_the_three_cases():
    text = _memo_text()
    # Case A: delayed observe after a retract.
    assert "retract" in text and "delayed" in text
    # Case B: long-offline reconnect.
    assert "reconnect" in text and "offline" in text
    # Case C: clock skew.
    assert "skew" in text


def test_memo_uses_concrete_event_sequences():
    text = _memo_text()
    # Concrete sequences reference the real ordering fields and event types.
    assert "observed" in text and "retracted" in text
    assert "client_sequence" in text
    assert "seq" in text
    # At least one fenced example block.
    assert "```" in text


def test_memo_states_cost_and_recommendation():
    text = _memo_text()
    assert "cost" in text
    assert "recommend" in text
    # It compares the status quo against an HLC rather than only describing one.
    assert "hlc" in text
