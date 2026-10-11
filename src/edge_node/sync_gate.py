"""Value-per-KB sync gate (D3 / T1) — pure batch selection under a byte budget.

A constrained node cannot push its whole outbox over a thin link in one go. This
module decides, deterministically and without any I/O, WHICH pending envelopes to
send now so the most valuable information-per-byte goes first and nothing is
starved forever:

  * URGENT facts (a gas alert) always drain ahead of ROUTINE ones (a photo);
  * within a priority bucket, facts are ranked by value density — an age-boosted
    `value / bytes` — so a small, valuable fact beats a large, redundant one and
    a long-deferred fact rises over time rather than being stranded;
  * the batch fills up to `budget_bytes`; the rest are deferred to the next push
    (they stay pending on the device — never dropped);
  * a single envelope larger than the whole budget can never fit any batch, so it
    is sent anyway rather than deferred forever (oversize escape hatch).

The caller (the edge push path) does all the I/O: it reads the pending points,
measures each envelope's byte size, reads the novelty/urgency persisted at
capture, and marks points synced only on the hub's ack. This module is a pure
function of its inputs, so the selection is reproducible and unit-testable with
no shard and no network.
"""

from typing import Any, Dict, List, Tuple

# How fast a deferred fact's value grows with its age (per second), so a fact the
# gate keeps deferring eventually outranks fresh arrivals instead of starving.
# A named tunable (config `sync.aging_factor`), never a bare literal in the score.
DEFAULT_AGING_FACTOR = 0.01


def fact_score(value: float, age_s: float, nbytes: int, *, aging_factor: float) -> float:
    """Age-boosted value density: ``value × (1 + aging_factor × age) / bytes``.

    Higher is better. `bytes` is floored at 1 so a zero-byte estimate cannot
    divide by zero or make the score explode.
    """
    nbytes = max(1, int(nbytes))
    boost = 1.0 + aging_factor * max(0.0, float(age_s))
    return float(value) * boost / nbytes


def select_batch(
    candidates: List[Dict[str, Any]],
    budget_bytes: int,
    *,
    aging_factor: float = DEFAULT_AGING_FACTOR,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Choose which pending envelopes to push now under a byte budget.

    Each candidate is a dict with:
        id             — the point id (opaque here)
        bytes          — serialized envelope size in bytes
        value          — intrinsic value (e.g. novelty + urgency), higher = better
        age_s          — seconds since capture (drives the aging boost)
        urgent         — bool; URGENT facts drain before ROUTINE
        client_sequence— per-device order, the deterministic tie-break

    Returns `(selected, deferred)`. `selected` is in push order: URGENT first,
    then by score descending, ties broken by `client_sequence` ascending. The
    batch fills to `budget_bytes`; an envelope that cannot fit is deferred —
    EXCEPT one larger than the entire budget, which is sent anyway so it is never
    stranded. A non-positive budget means "no limit": everything is selected.
    """
    def sort_key(c: Dict[str, Any]):
        score = fact_score(
            c.get("value", 0.0), c.get("age_s", 0.0), c.get("bytes", 1),
            aging_factor=aging_factor,
        )
        return (0 if c.get("urgent") else 1, -score, c.get("client_sequence", 0))

    ordered = sorted(candidates, key=sort_key)

    if budget_bytes is None or budget_bytes <= 0:
        return ordered, []

    selected: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    used = 0
    for c in ordered:
        nbytes = max(1, int(c.get("bytes", 1)))
        if nbytes > budget_bytes:
            # Larger than the whole budget: it can never fit a batch, so sending
            # it now is the only way it ever syncs (e.g. an URGENT oversize alert).
            selected.append(c)
            used += nbytes
        elif used + nbytes <= budget_bytes:
            selected.append(c)
            used += nbytes
        else:
            deferred.append(c)
    return selected, deferred
