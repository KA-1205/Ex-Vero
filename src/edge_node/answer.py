"""Answer layer — on-device RAG orchestration (Phase 7, backend.md §8).

The device's core promise is that it can *answer*, not just retrieve, and it can
do so offline. So this module is deliberately isolated: it imports **only** the
standard library. It never imports `qdrant_edge`, the sync transport, or the
network simulator (AGENTS.md §5 invariant 8, backend.md §2.5) — a static
import-graph test enforces that, the same way it does for `retrieval.py`.

The pipeline is retrieve → augment → generate:

  * retrieve — done by the caller (the hybrid query in `retrieval.py`); the
    already-ranked hits are passed in, so this module stays free of the store.
  * augment  — `select_context` picks the top-k hits and puts ONLY their values
    into the prompt context. CONFIRMED facts are promoted ahead of uncorroborated
    ones and DISPUTED facts are surfaced with a visible flag rather than silently
    dropped (backend.md §8). No outside text ever enters the context — this is
    what makes the answer grounded.
  * generate — try each generator in the chain, head-first; the first that
    returns (without raising) serves the answer. Generators raise when their
    model daemon is unreachable, so the chain falls through to the always-
    available `ExtractiveGenerator`. The serving generator's `answer_path`
    (`offline`/`online`/`extractive`) and `name` are reported back.

Consensus state travels on each hit's payload (`consensus_state`), so this module
never has to reach the gateway fold — keeping the answer path offline-clean.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Fewer-is-better preference order for the augment step: CONFIRMED first, then
# unknown/unlabelled, then DISPUTED last (still included, just de-prioritised and
# flagged). Anything unrecognised sorts with "unknown".
_STATE_RANK = {"CONFIRMED": 0, "RESOLVED_LWW": 0, None: 1, "UNKNOWN": 1, "DISPUTED": 2}
_DISPUTED_FLAG = "[DISPUTED] "


@dataclass
class AnswerResult:
    """What the Answer layer returns; `main.py` maps this onto the `/query`
    response shape in `docs/API.md` §4 (adding the measured `latency_ms`)."""

    answer: str
    answer_path: str          # "offline" | "online" | "extractive"
    model: str                # the serving generator's name
    sources: List[Dict[str, Any]]  # [{id, score, value, consensus_state}, ...]


def select_context(retrieved: List[Any], top_k: int) -> List[Dict[str, Any]]:
    """Pick the top-k facts for the prompt and order them for the generator.

    Returns per-hit dicts carrying the raw `value` (for citation), the
    presentation `context_line` (DISPUTED-flagged), and the metadata used in the
    `sources` list. Ranking is (consensus rank, then descending retrieval score),
    a stable sort so within a consensus tier the retrieval order is preserved.
    """
    items: List[Dict[str, Any]] = []
    for hit in retrieved:
        payload = getattr(hit, "payload", None) or {}
        state = payload.get("consensus_state")
        value = payload.get("value", "")
        items.append(
            {
                "id": getattr(hit, "id", None),
                "score": float(getattr(hit, "score", 0.0) or 0.0),
                "value": value,
                "consensus_state": state,
            }
        )

    items.sort(key=lambda it: (_STATE_RANK.get(it["consensus_state"], 1), -it["score"]))
    top = items[:top_k]

    for it in top:
        line = it["value"]
        if it["consensus_state"] == "DISPUTED":
            # Flag it in the prompt so the model (and the extractive stitch)
            # surface the dispute instead of asserting it as fact.
            line = _DISPUTED_FLAG + line
        it["context_line"] = line
    return top


def answer_question(
    question: str,
    retrieved: List[Any],
    generators: List[Any],
    *,
    top_k: int = 5,
) -> AnswerResult:
    """Augment the retrieved hits into context and generate a grounded answer.

    `generators` is the config-selected chain (see `registry.build_generators`);
    it must end in a generator that cannot fail (the `ExtractiveGenerator`), so a
    reachable answer is guaranteed. The serving generator's path/name are echoed
    back, and `sources` cites the exact retrieved point ids that fed the context.
    """
    top = select_context(retrieved, top_k)
    context = [it["context_line"] for it in top]
    sources = [
        {
            "id": it["id"],
            "score": it["score"],
            "value": it["value"],
            "consensus_state": it["consensus_state"],
        }
        for it in top
    ]

    last_error: Optional[Exception] = None
    for generator in generators:
        try:
            text = generator.generate(question, context)
        except Exception as e:  # unreachable model daemon, HTTP error, timeout…
            last_error = e
            continue
        return AnswerResult(
            answer=text,
            answer_path=getattr(generator, "answer_path", "extractive"),
            model=getattr(generator, "name", "unknown"),
            sources=sources,
        )

    # Only reached if the caller passed a chain with no always-available
    # fallback and every generator failed. Fail loudly rather than inventing an
    # answer — the runtime chain always ends in the extractive generator.
    raise RuntimeError(f"no generator could answer (last error: {last_error})")
