"""Benchmarks — measured numbers, never constants (Phase 9, backend.md §7.5/§9).

Both benchmarks in this module recompute from real data on every call:

  * **recall@5** — dense-only vs the fused hybrid (dense + BM25, RRF) over a
    labeled query set. The labeled records are indexed into a *real* Qdrant Edge
    shard, so the two legs rank against the same corpus a device actually holds.
    A hit means a known-relevant id came back in the top 5.
  * **resolver vs LWW** — the trust-weighted fold (`fold_consensus`, the exact
    module the Cloud Gateway runs) is scored against a last-write-wins baseline
    over the same labeled dispute scenarios.

The honesty rules that shape this file:

  * The recall benchmark owns a dedicated benchmark device shard. It never reads
    a device's live capture history, because those points carry no relevance
    labels — scoring against them would measure nothing.
  * The resolver baseline is a *real* LWW implementation (highest hub `seq`
    wins), not a stubbed constant. If the resolver and LWW score the same, this
    benchmark reports it rather than hiding it.
  * Nothing here is tuned to produce a headline number. Whatever the fold scores
    on the fixture is what gets reported.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Phase 9 fixtures live in the repo's test fixtures dir, not inside the
# installed package: they are test data, and the app must read the same files
# the acceptance tests score.
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
RECALL_FIXTURE = FIXTURES_DIR / "recall_queries.jsonl"
RECALL_CORPUS_FIXTURE = FIXTURES_DIR / "recall_corpus.jsonl"
CONFLICT_FIXTURE = FIXTURES_DIR / "conflict_facts.jsonl"

# The recall benchmark owns a dedicated device shard. Using a real device's live
# captures would score against points that carry no relevance labels.
BENCH_DEVICE = "__benchmark__"
RECALL_K = 5
# The fused leg prefetches wider than it returns so RRF has candidates to fuse.
RECALL_PREFETCH_K = 25


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read a JSONL fixture. A missing fixture is an error, not a silent zero —
    the phase requires fixtures to live in the repo."""
    if not path.exists():
        raise FileNotFoundError(f"benchmark fixture missing: {path}")
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Recall: dense-only vs hybrid
# ---------------------------------------------------------------------------

def recall_at_k(
    ranked_ids: List[int],
    relevant_ids: set,
    k: int = 5,
) -> bool:
    """A query counts as recalled when any known-relevant id lands in the top k.

    This is per-query recall@5: `True`/`False` per query, averaged by the caller.
    Checking for *any* relevant id in the window (rather than requiring all) is
    what makes dense and hybrid comparable on the same labeled set.
    """
    return any(pid in relevant_ids for pid in ranked_ids[:k])


def score_recall(
    records: List[Dict[str, Any]],
    dense_rank: Any,
    hybrid_rank: Any,
    k: int = 5,
) -> Dict[str, Any]:
    """Score both legs over one labeled set and return the measured numbers.

    `dense_rank` / `hybrid_rank` are callables taking a query text and returning
    the ranked point ids. Both legs must see the identical corpus and the
    identical query set, otherwise the comparison is meaningless.
    """
    dense_hits = 0
    hybrid_hits = 0
    total = 0
    by_shape: Dict[str, Dict[str, int]] = {}

    for rec in records:
        query = rec["query"]
        relevant = set(rec["relevant_ids"])
        if not query or not relevant:
            # A query with no ground truth cannot be scored; counting it as a
            # miss would quietly deflate both numbers.
            continue
        total += 1

        dense_ok = recall_at_k(dense_rank(query), relevant, k)
        hybrid_ok = recall_at_k(hybrid_rank(query), relevant, k)
        dense_hits += int(dense_ok)
        hybrid_hits += int(hybrid_ok)

        # Per-shape tallies. Fusion's advantage is not uniform across query
        # types, and reporting only the aggregate would hide that.
        shape = rec.get("shape", "unshaped")
        bucket = by_shape.setdefault(
            shape, {"queries": 0, "dense_hits": 0, "hybrid_hits": 0}
        )
        bucket["queries"] += 1
        bucket["dense_hits"] += int(dense_ok)
        bucket["hybrid_hits"] += int(hybrid_ok)

    for bucket in by_shape.values():
        n = bucket["queries"]
        bucket["dense_recall_at_5"] = round(bucket["dense_hits"] / n, 4)
        bucket["hybrid_recall_at_5"] = round(bucket["hybrid_hits"] / n, 4)

    if total == 0:
        raise ValueError("no scorable queries in the labeled recall fixture")

    return {
        "k": k,
        "labeled_queries": total,
        "dense_hits": dense_hits,
        "hybrid_hits": hybrid_hits,
        "dense_recall_at_5": round(dense_hits / total, 4),
        "hybrid_recall_at_5": round(hybrid_hits / total, 4),
        # Queries where only one leg found a relevant id — the evidence behind
        # the headline. A fusion layer that merely tied would show neither.
        "hybrid_only_hits": hybrid_hits - dense_hits,
        "by_shape": by_shape,
    }


# ---------------------------------------------------------------------------
# Resolver vs last-write-wins
# ---------------------------------------------------------------------------

def lww_resolve(events: List[Dict[str, Any]]) -> Optional[str]:
    """A real last-write-wins baseline: the newest event by hub `seq` wins.

    This is the baseline the resolver has to beat, so it is implemented honestly
    rather than stubbed. Note it is blind to corroboration and to trust — a lone
    late report from a distrusted device beats a majority of earlier ones.
    """
    observed = [e for e in events if e.get("event_type") == "OBSERVED"]
    if not observed:
        return None
    newest = max(observed, key=lambda e: e["seq"])
    return newest.get("value")


def _load_fold():
    """Import the Cloud Gateway's consensus fold by path.

    `fold_consensus` is the single source of truth for conflict resolution, so the
    benchmark must score the exact code the gateway runs — not a local copy that
    could drift. The gateway lives beside `src/`, outside the installed package,
    so it is loaded explicitly rather than assumed on `sys.path`.
    """
    import importlib.util

    if "consensus_fold" in sys.modules:
        return sys.modules["consensus_fold"]

    fold_path = Path(__file__).resolve().parents[2] / "cloud-gateway" / "consensus_fold.py"
    if not fold_path.exists():
        raise FileNotFoundError(f"consensus fold module missing: {fold_path}")

    spec = importlib.util.spec_from_file_location("consensus_fold", fold_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["consensus_fold"] = module
    spec.loader.exec_module(module)
    return module


def resolver_resolve(events: List[Dict[str, Any]], threshold: float) -> Optional[str]:
    """The trust-weighted fold's pick. Delegates to the real fold module."""
    fold = _load_fold()
    out = fold.fold_consensus(events, threshold=threshold)
    # A DISPUTED outcome deliberately resolves to nothing — the resolver refuses
    # to silently pick a contested value. Scoring that as "no answer" is correct:
    # an operator would rather see no value than a coin-flip.
    if out["status"] in ("CONFIRMED", "LWW"):
        return out.get("value")
    return None


def build_scenario(
    fact: Dict[str, Any],
    scenario_index: int,
) -> List[Dict[str, Any]]:
    """Turn one labeled fact into a real event log for the resolver.

    The fixture's `shape` picks the dispute pattern, so the benchmark measures
    the fold across situations it genuinely finds hard — not one rigged
    arrangement repeated. Reported values arrive in the order shown, so the
    highest-`seq` event is the last one written.

      minority_late_wrong — several truthful devices report first, then one
        low-trust device reports a wrong value LAST. LWW takes the late report;
        the fold backs the corroborated truth. This is LWW's real failure mode.
      contested — truthful and wrong reports are evenly and equally trusted, so
        there is no majority. The fold should abstain rather than coin-flip.
      late_correction — the newest report is the correct one, so recency was
        right and LWW gets it right. The fold's corroboration signal is weaker
        here, and it may decline to answer. Included because a benchmark where
        the fold wins every single scenario would be rigged.
      corroboration_foiled — two late, higher-trust wrong reports outweigh one
        truthful one, so the fold CONFIRMS the wrong value. This is a genuine
        failure of trust-weighted corroboration, and it is in the fixture on
        purpose: the fold does not get to look perfect.
    """
    key = fact["corroboration_key"]
    truth = fact["correct_value"]
    decoy = fact["decoy_value"]
    shape = fact.get("shape", "minority_late_wrong")

    if shape == "minority_late_wrong":
        plan = [(truth, 0.8)] * 3 + [(decoy, 0.3)]
    elif shape == "contested":
        # Equal trust, equal counts — no majority to weight toward.
        plan = [(truth, 0.6), (decoy, 0.6), (truth, 0.6), (decoy, 0.6)]
    elif shape == "late_correction":
        # Stale wrong reports first, the correction last: recency is correct.
        plan = [(decoy, 0.3), (decoy, 0.3), (truth, 0.8)]
    elif shape == "corroboration_foiled":
        # A wrong pair that is both later and more trusted than the truth.
        plan = [(truth, 0.4), (decoy, 0.9), (decoy, 0.9)]
    else:
        raise ValueError(f"unknown scenario shape: {shape}")

    events: List[Dict[str, Any]] = []
    for seq, (val, trust) in enumerate(plan, start=1):
        events.append(
            {
                "corroboration_key": key,
                "device_id": f"dev-s{scenario_index}-{seq}",
                "value": val,
                "event_type": "OBSERVED",
                "seq": seq,
                "client_timestamp_ns": seq * 1_000_000_000,
                "device_trust_at_report": trust,
            }
        )
    return events


def score_resolver_vs_lww(
    facts: List[Dict[str, Any]],
    threshold: float,
) -> Dict[str, Any]:
    """Score the fold and the LWW baseline over the same scenarios.

    Accuracy = the fraction of scenarios in which the strategy's chosen value
    equals the fixture's known-correct value.
    """
    resolver_correct = 0
    lww_correct = 0
    total = 0
    unresolved = 0
    by_shape: Dict[str, Dict[str, int]] = {}

    for i, fact in enumerate(facts):
        expected = fact.get("correct_value")
        if expected is None:
            continue
        total += 1

        events = build_scenario(fact, i)
        resolver_pick = resolver_resolve(events, threshold)
        lww_pick = lww_resolve(events)

        # EXACT equality, never `expected in pick`: the ground truth is a full
        # event value, and these values overlap as substrings ("passable" is a
        # substring of "impassable"). Substring matching scored LWW correct on
        # scenarios it actually got wrong.
        r_ok = resolver_pick == expected
        l_ok = lww_pick == expected
        resolver_correct += int(r_ok)
        lww_correct += int(l_ok)

        if resolver_pick is None:
            unresolved += 1

        # Per-shape tallies, so a regression can be traced to a dispute pattern
        # rather than just a number moving.
        shape = fact.get("shape", "unshaped")
        bucket = by_shape.setdefault(
            shape, {"scenarios": 0, "resolver_correct": 0, "lww_correct": 0}
        )
        bucket["scenarios"] += 1
        bucket["resolver_correct"] += int(r_ok)
        bucket["lww_correct"] += int(l_ok)

    if total == 0:
        raise ValueError("no scorable conflict facts in the fixture")

    return {
        "scenarios": total,
        "resolver_correct": resolver_correct,
        "lww_correct": lww_correct,
        "resolver_accuracy": round(resolver_correct / total, 4),
        "lww_accuracy": round(lww_correct / total, 4),
        "accuracy_delta": round((resolver_correct - lww_correct) / total, 4),
        # Scenarios where the fold abstained (DISPUTED/ABSENT). Abstaining beats
        # a confident wrong answer, so it is counted as "no answer", not a hit.
        "resolver_abstained": unresolved,
        "by_shape": by_shape,
    }
