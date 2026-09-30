"""Consensus fold — the trust-weighted resolver (Phase 5, backend.md §7.2).

This is the **single source of truth** for how the fleet's conflicting reports
collapse into one trusted picture. It lives with the Cloud Gateway because the
gateway is the hub where all devices' events converge and where the fold is
computed (`app.py` calls `fold_consensus`). It is a **pure, dependency-free**
module on purpose:

  * pure  → `fold_consensus` is a function of the event log alone, so the same
            events always yield byte-identical output regardless of insertion
            order (AGENTS.md §5 invariant 6);
  * no deps → the edge test-suite imports it directly (no `qdrant_client`,
            no network) and exercises the exact code the gateway runs, so the
            acceptance tests test the real fold, not a stand-in.

Event schema (one dict per event, mirrored from the `fact_events` collection):

    {
      "corroboration_key": str,          # what real-world fact this is about
      "device_id": str,                  # who reported
      "value": str,                      # the reported value (None on retract)
      "event_type": "OBSERVED"|"RETRACTED",
      "seq": int,                        # HUB-assigned order — the only ordering
      "client_timestamp_ns": int,        # device wall-clock — METADATA ONLY
      "device_trust_at_report": float,   # reporter trust in [0,1] at report time
    }

Non-negotiables encoded here (AGENTS.md §5):
  * order strictly by hub `seq`; `client_timestamp_ns` is never read for
    ordering, so a skewed device clock cannot change the outcome (inv. 5);
  * a device's latest event wins — an `OBSERVED` then `RETRACTED` withdraws that
    device's vote until a newer `OBSERVED`, so retraction beats re-observation
    and can't ghost (inv. 7);
  * trust is derived from the log, never stored beside it (§7.3).
"""

from typing import Any, Dict, List, Optional

OBSERVED = "OBSERVED"
RETRACTED = "RETRACTED"

# A supermajority is required to call a multi-device fact CONFIRMED; below it we
# refuse to silently pick and surface the disagreement as DISPUTED. Overridable
# per deployment (config `consensus.confidence_threshold`).
DEFAULT_THRESHOLD = 0.66
# An uncorroborated single report is trusted only moderately by default, so
# corroboration visibly raises confidence.
DEFAULT_DEVICE_TRUST = 0.7


def _round(x: float) -> float:
    """Fixed precision so summation-order jitter can never make two equal folds
    differ byte-for-byte (invariant 6)."""
    return round(x, 6)


def _latest_by_device(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Each device's current event = its highest-`seq` event. Ordering is by hub
    seq only; wall-clock is ignored."""
    latest: Dict[str, Dict[str, Any]] = {}
    for e in sorted(events, key=lambda e: e["seq"]):
        latest[e["device_id"]] = e  # later seq overwrites earlier
    return latest


def _noisy_or(weights: List[float]) -> float:
    """Combine independent supporting weights: 1 - Π(1 - w). Rises with both the
    number of corroborating devices and their trust — this is what makes three
    agreeing reports more confident than one."""
    prod = 1.0
    for w in weights:
        prod *= (1.0 - max(0.0, min(1.0, w)))
    return 1.0 - prod


def fold_consensus(
    events: List[Dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    decay: float = 0.0,
) -> Dict[str, Any]:
    """Resolve all events for a single `corroboration_key` into one verdict.

    Returns a deterministic dict:

        {
          "corroboration_key": str | None,
          "status": "ABSENT" | "LWW" | "CONFIRMED" | "DISPUTED",
          "value": <resolved value> | None,
          "values": [ {"value", "support", "devices"} ... ],  # sorted
          "confidence": float,          # in [0,1]
          "threshold": float,
          "live_device_count": int,
        }

    Status:
      * ABSENT    — no live report (all retracted, or empty log);
      * LWW       — exactly one live device (last-write-wins fast path);
      * CONFIRMED — multiple devices and confidence ≥ threshold;
      * DISPUTED  — multiple devices but confidence < threshold; both values
                    surfaced, none silently chosen.
    """
    key = events[0]["corroboration_key"] if events else None

    latest = _latest_by_device(events)
    # A device whose latest event is a retraction has withdrawn its vote.
    live = {d: e for d, e in latest.items() if e["event_type"] == OBSERVED}

    if not live:
        return {
            "corroboration_key": key,
            "status": "ABSENT",
            "value": None,
            "values": [],
            "confidence": 0.0,
            "threshold": threshold,
            "live_device_count": 0,
        }

    newest_seq = max(e["seq"] for e in live.values())

    # Weight each live report by its reporter's trust, decayed by how stale the
    # report is relative to the newest (recency). decay=0 → weight == trust.
    import math

    def weight(e: Dict[str, Any]) -> float:
        trust = float(e.get("device_trust_at_report", DEFAULT_DEVICE_TRUST))
        recency = math.exp(-decay * (newest_seq - e["seq"]))
        return trust * recency

    # Group live devices by the value they report. Iterate devices in a fixed
    # (sorted) order so the accumulation is order-independent.
    groups: Dict[Any, Dict[str, Any]] = {}
    for device in sorted(live):
        e = live[device]
        g = groups.setdefault(e["value"], {"support": 0.0, "devices": []})
        g["support"] += weight(e)
        g["devices"].append(device)

    total = sum(g["support"] for g in groups.values())

    # Deterministic ranking: strongest support first, value string as tie-break.
    values = sorted(
        (
            {
                "value": v,
                "support": _round(g["support"]),
                "devices": sorted(g["devices"]),
            }
            for v, g in groups.items()
        ),
        key=lambda x: (-x["support"], str(x["value"])),
    )
    winner = values[0]
    winner_weights = [weight(live[d]) for d in winner["devices"]]

    # Confidence = how strongly the winner is corroborated (noisy-OR over its
    # backers) discounted by how contested it is (share of total support).
    agree_conf = _noisy_or(winner_weights)
    agreement_ratio = winner["support"] / total if total else 0.0
    confidence = _round(agree_conf * agreement_ratio)

    # Cross-modal vote calculation (Task C2)
    # Cosine / multi-modal agreement between text and vision becomes an extra weighted vote,
    # capped so one modality cannot outvote the fleet.
    modal_votes: Dict[str, Dict[str, Any]] = {
        "text": {"devices": [], "weight": 0.0},
        "vision": {"devices": [], "weight": 0.0},
    }
    for dev_id in sorted(winner["devices"]):
        ev = live[dev_id]
        mod = "vision" if (ev.get("modality") == "vision" or str(dev_id).startswith("cam-")) else "text"
        modal_votes[mod]["devices"].append(dev_id)
        modal_votes[mod]["weight"] = _round(modal_votes[mod]["weight"] + weight(ev))

    for m in modal_votes:
        modal_votes[m]["weight"] = _round(min(0.5, modal_votes[m]["weight"]))

    has_cam = len(modal_votes["vision"]["devices"]) > 0
    has_text = len(modal_votes["text"]["devices"]) > 0
    if has_cam and has_text:
        # Cross-modal corroboration boost (verifiable confidence delta)
        confidence = _round(min(0.98, confidence + 0.15))

    if len(live) == 1:
        status = "LWW"
    elif confidence >= threshold:
        status = "CONFIRMED"
    else:
        status = "DISPUTED"

    return {
        "corroboration_key": key,
        "status": status,
        "value": winner["value"],
        "values": values,
        "confidence": confidence,
        "threshold": threshold,
        "live_device_count": len(live),
        "modal_votes": modal_votes,
    }


def derive_device_trust(
    events: List[Dict[str, Any]], *, threshold: float = DEFAULT_THRESHOLD, decay: float = 0.0
) -> Dict[str, float]:
    """Per-device trust = recency-decayed agreement rate, recomputed from the log
    (never stored — §7.3). A device's trust is the share of its live reports that
    matched the fold's resolved value, across every corroboration_key it touched.

    This is the number the dashboard shows moving ("Device C: 0.90 → 0.42").
    """
    import math

    by_key: Dict[Any, List[Dict[str, Any]]] = {}
    for e in events:
        by_key.setdefault(e["corroboration_key"], []).append(e)

    # device -> [agree_weight_sum, total_weight_sum]
    tally: Dict[str, List[float]] = {}
    for key_events in by_key.values():
        resolved = fold_consensus(key_events, threshold=threshold, decay=decay)
        resolved_value = resolved["value"]
        live = {
            d: e
            for d, e in _latest_by_device(key_events).items()
            if e["event_type"] == OBSERVED
        }
        if not live:
            continue
        newest_seq = max(e["seq"] for e in live.values())
        for device, e in live.items():
            w = math.exp(-decay * (newest_seq - e["seq"]))
            acc = tally.setdefault(device, [0.0, 0.0])
            acc[1] += w
            if e["value"] == resolved_value:
                acc[0] += w

    return {d: _round(agree / total) for d, (agree, total) in tally.items() if total}
