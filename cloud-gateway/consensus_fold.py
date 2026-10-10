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
RESOLVED = "RESOLVED"

# A supermajority is required to call a multi-device fact CONFIRMED; below it we
# refuse to silently pick and surface the disagreement as DISPUTED. Overridable
# per deployment (config `consensus.confidence_threshold`).
DEFAULT_THRESHOLD = 0.66
# An uncorroborated single report is trusted only moderately by default, so
# corroboration visibly raises confidence.
DEFAULT_DEVICE_TRUST = 0.7

# Trust smoothing (B2 / P1-2). A Beta(alpha, beta) prior keeps a device with few
# observations from swinging to 0.0/1.0 on a single (dis)agreement; the clamp
# keeps derived trust inside a sane band. Defaults are identity (alpha=beta=0,
# band [0,1]) so trust is unchanged until a deployment configures smoothing.
DEFAULT_TRUST_ALPHA = 0.0
DEFAULT_TRUST_BETA = 0.0
DEFAULT_TRUST_FLOOR = 0.0
DEFAULT_TRUST_CEILING = 1.0

# Time-based staleness (B4 / P1-4). A report's weight halves every half-life of
# real elapsed time (by `received_at_ns`, the hub clock), so two stale reports
# lose to one fresh correction. The half-life is a per-vertical tunable
# (config `consensus.half_life_s`); the default is a non-zero 6 hours. The fold
# stays pure — `now` is injected by the caller, never read inside here.
DEFAULT_HALF_LIFE_S = 6 * 3600.0

# Cross-modal corroboration (B6 / P1-6). When the winning value is backed by two
# or more modalities, one extra independent supporting leg of this weight joins
# the noisy-OR — a vote, not a flat bump. Config `consensus.cross_modal_weight`.
DEFAULT_CROSS_MODAL_WEIGHT = 0.3


def _round(x: float) -> float:
    """Fixed precision so summation-order jitter can never make two equal folds
    differ byte-for-byte (invariant 6)."""
    return round(x, 6)


def _event_order_key(e: Dict[str, Any]):
    """Order one device's events deterministically (P0-3).

    Within a device the persistent `client_sequence` is authoritative, so a
    delayed or retried event (which keeps its client_sequence) can never override
    a newer one just because it reached the hub later and got a higher hub `seq`.
    The hub `seq` is the tie-break, and the fallback for legacy events that carry
    no client_sequence. Across devices, recency still uses the hub `seq`.
    """
    cs = e.get("client_sequence")
    primary = cs if cs is not None else e["seq"]
    return (primary, e["seq"])


def _latest_by_device(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Each device's current event = its highest-ordered event (client_sequence
    first, then hub seq). Pure and order-independent: we take the max by key, so
    insertion order of the event list never changes the result (invariant 6)."""
    latest: Dict[str, Dict[str, Any]] = {}
    best_key: Dict[str, Any] = {}
    for e in events:
        d = e["device_id"]
        k = _event_order_key(e)
        if d not in best_key or k > best_key[d]:
            best_key[d] = k
            latest[d] = e
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
    device_trust: Optional[Dict[str, float]] = None,
    now_ns: Optional[int] = None,
    half_life_ns: Optional[float] = None,
    cross_modal_weight: float = DEFAULT_CROSS_MODAL_WEIGHT,
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

    `device_trust`, when supplied, is the hub-derived per-device trust used as
    each reporter's weight (B2 / P1-2) — unforgeable, since the hub computes it
    from the log rather than reading a client-supplied value. When omitted, the
    per-event `device_trust_at_report` is used (keeps the pure fold self-contained
    for the unit tests).
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
    # report is. With a time half-life (B4) staleness is real elapsed time via
    # the injected `now_ns` and each event's `received_at_ns`; otherwise it falls
    # back to the hub-seq-distance decay. decay=0 and no half-life → weight==trust.
    import math

    def _recency(e: Dict[str, Any]) -> float:
        if now_ns is not None and half_life_ns:
            received = e.get("received_at_ns")
            if received is None:
                return 1.0
            age = max(0.0, float(now_ns) - float(received))
            return 0.5 ** (age / float(half_life_ns))
        return math.exp(-decay * (newest_seq - e["seq"]))

    def weight(e: Dict[str, Any]) -> float:
        if device_trust is not None:
            trust = float(device_trust.get(e["device_id"], DEFAULT_DEVICE_TRUST))
        else:
            trust = float(e.get("device_trust_at_report", DEFAULT_DEVICE_TRUST))
        return trust * _recency(e)

    # Group live devices by the SEMANTIC value cluster they report (B5), falling
    # back to the raw value when no cluster id is present. Paraphrases carrying
    # the same `value_cluster` fold together; a contradicting report (different
    # number/negation) was given a different cluster at ingest and stays separate.
    # Iterate devices in a fixed (sorted) order so accumulation is order-independent.
    groups: Dict[Any, Dict[str, Any]] = {}
    for device in sorted(live):
        e = live[device]
        ck = e.get("value_cluster") or e["value"]
        g = groups.setdefault(ck, {"support": 0.0, "devices": [], "value": e["value"]})
        g["support"] += weight(e)
        g["devices"].append(device)
        # Representative display value: the smallest value string in the cluster,
        # chosen deterministically so the output never depends on event order.
        if str(e["value"]) < str(g["value"]):
            g["value"] = e["value"]

    total = sum(g["support"] for g in groups.values())

    # Deterministic ranking: strongest support first, value string as tie-break.
    values = sorted(
        (
            {
                "value": g["value"],
                "support": _round(g["support"]),
                "devices": sorted(g["devices"]),
            }
            for g in groups.values()
        ),
        key=lambda x: (-x["support"], str(x["value"])),
    )
    winner = values[0]
    winner_weights = [weight(live[d]) for d in winner["devices"]]

    # Group the winner's backers by their reported modality (B6 / P1-6). Modality
    # comes from the event payload ONLY — never a `cam-` device-id sniff. The same
    # claim seen across modalities shares a value_cluster (B5), so those reports
    # already landed in this one winning group.
    modal_votes: Dict[str, Dict[str, Any]] = {}
    for dev_id in sorted(winner["devices"]):
        ev = live[dev_id]
        mod = ev.get("modality") or "text"
        mv = modal_votes.setdefault(mod, {"devices": [], "weight": 0.0})
        mv["devices"].append(dev_id)
        mv["weight"] = _round(mv["weight"] + weight(ev))

    # Cross-modal corroboration is an INDEPENDENT vote inside the noisy-OR, not a
    # flat additive bump: when the winner is backed by two or more modalities we
    # add one extra supporting leg of `cross_modal_weight`. Because it rides the
    # noisy-OR it raises confidence proportionally and can never exceed 1.0, and
    # it only fires with genuine cross-modal agreement — a single modality on its
    # own gets no bonus and cannot outvote the fleet.
    cross_modal = len(modal_votes) >= 2
    conf_weights = list(winner_weights)
    if cross_modal:
        conf_weights.append(cross_modal_weight)

    # Confidence = how strongly the winner is corroborated (noisy-OR over its
    # backers, plus any cross-modal leg) discounted by how contested it is.
    agree_conf = _noisy_or(conf_weights)
    agreement_ratio = winner["support"] / total if total else 0.0
    confidence = _round(agree_conf * agreement_ratio)

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
        "cross_modal": cross_modal,
        "modal_votes": modal_votes,
    }


def _resolved_ground_truth(key_events: List[Dict[str, Any]]) -> Optional[Any]:
    """The winning value of the latest RESOLVED event for a key, if any (D7 hook).

    A human/admin RESOLVED decision is authoritative ground truth: agreement is
    scored against it regardless of the fold's own status. Ordered by hub `seq`.
    """
    resolved = [e for e in key_events if e.get("event_type") == RESOLVED]
    if not resolved:
        return None
    winner = max(resolved, key=lambda e: e.get("seq", 0))
    return winner.get("value")


def derive_device_trust(
    events: List[Dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    decay: float = 0.0,
    alpha: float = DEFAULT_TRUST_ALPHA,
    beta: float = DEFAULT_TRUST_BETA,
    trust_floor: float = DEFAULT_TRUST_FLOOR,
    trust_ceiling: float = DEFAULT_TRUST_CEILING,
) -> Dict[str, float]:
    """Per-device trust = recency-decayed agreement rate, recomputed from the log
    (never stored, never client-supplied — §7.3 / P1-2). Unforgeable: it is a
    function of the event log alone.

    A device's trust is the share of its live reports that matched the resolved
    value, across the keys that can actually adjudicate agreement:

      * a lone (LWW, single live device) key never grants free self-trust;
      * an exact tie credits no device (so no one gains trust from the fold's
        alphabetical tie-break);
      * a RESOLVED decision is authoritative ground truth.

    A Beta(alpha, beta) prior smooths sparse histories and the result is clamped
    to [trust_floor, trust_ceiling] (all config, identity by default).
    """
    import math

    by_key: Dict[Any, List[Dict[str, Any]]] = {}
    for e in events:
        by_key.setdefault(e["corroboration_key"], []).append(e)

    # device -> [agree_weight_sum, total_weight_sum]
    tally: Dict[str, List[float]] = {}
    for key_events in by_key.values():
        live = {
            d: e
            for d, e in _latest_by_device(key_events).items()
            if e["event_type"] == OBSERVED
        }
        if not live:
            continue

        ground_truth = _resolved_ground_truth(key_events)
        if ground_truth is not None:
            resolved_value = ground_truth
        else:
            # A single live device agrees only with itself — not evidence of
            # trustworthiness, so it earns no agreement credit.
            if len(live) < 2:
                continue
            resolved = fold_consensus(key_events, threshold=threshold, decay=decay)
            vals = resolved.get("values", [])
            # An exact tie between the top two values is unadjudicated: credit no
            # device, so the fold's alphabetical tie-break never hands out trust.
            if len(vals) >= 2 and vals[0]["support"] == vals[1]["support"]:
                continue
            resolved_value = resolved["value"]

        newest_seq = max(e["seq"] for e in live.values())
        # Agreement is by semantic cluster (B5): a paraphrase in the winning
        # cluster counts as agreeing even if its exact wording differs. The
        # winning cluster is the one whose representative is `resolved_value`.
        def _cluster(ev: Dict[str, Any]):
            return ev.get("value_cluster") or ev["value"]

        winner_cluster = None
        for ev in live.values():
            if ev["value"] == resolved_value:
                winner_cluster = _cluster(ev)
                break
        if winner_cluster is None:
            winner_cluster = resolved_value  # RESOLVED ground truth with no live match

        for device, e in live.items():
            w = math.exp(-decay * (newest_seq - e["seq"]))
            acc = tally.setdefault(device, [0.0, 0.0])
            acc[1] += w
            if _cluster(e) == winner_cluster or e["value"] == resolved_value:
                acc[0] += w

    out: Dict[str, float] = {}
    for d, (agree, total) in tally.items():
        denom = total + alpha + beta
        if denom <= 0:
            continue
        trust = (agree + alpha) / denom
        trust = max(trust_floor, min(trust_ceiling, trust))
        out[d] = _round(trust)
    return out

