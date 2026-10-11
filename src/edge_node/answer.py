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
import re

# Fewer-is-better preference order for the augment step: CONFIRMED first, then
# unknown/unlabelled, then DISPUTED last (still included, just de-prioritised and
# flagged). Anything unrecognised sorts with "unknown".
_STATE_RANK = {"CONFIRMED": 0, "RESOLVED_LWW": 0, None: 1, "UNKNOWN": 1, "DISPUTED": 2}
_DISPUTED_FLAG = "[DISPUTED] "

# Prompt-injection hardening (C3 / P2-4). Retrieved facts are UNTRUSTED data from
# field devices. Control bytes (NUL, ESC/ANSI, other C0 controls) are stripped
# and newlines are flattened so a fact can neither smuggle terminal escapes nor
# break out of its single context line to forge new prompt structure. The
# generators additionally delimit the context and instruct the model to ignore
# any instructions embedded in it (see `adapter.build_grounded_prompt`).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_fact(text: Any) -> str:
    """Neutralise a single untrusted fact value for safe inclusion in a prompt."""
    s = "" if text is None else str(text)
    s = s.replace("\r", " ").replace("\n", " ")  # no multi-line break-out
    s = _CONTROL_RE.sub("", s)                     # strip control / escape bytes
    return s.strip()


@dataclass
class AnswerResult:
    """What the Answer layer returns; `main.py` maps this onto the `/query`
    response shape in `docs/API.md` §4 (adding the measured `latency_ms`)."""

    answer: str
    answer_path: str          # "offline" | "online" | "extractive"
    model: str                # the serving generator's name
    sources: List[Dict[str, Any]]  # [{id, score, value, consensus_state, device, modality}, ...]
    # Cited answers (D6 / T4): per-sentence breakdown
    # [{text, citations:[n...], supported:bool}, ...]. None for the plain path.
    sentences: Optional[List[Dict[str, Any]]] = None


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
        value = _sanitize_fact(payload.get("value", ""))
        items.append(
            {
                "id": getattr(hit, "id", None),
                "score": float(getattr(hit, "score", 0.0) or 0.0),
                "value": value,
                "consensus_state": state,
                # Carry provenance for cited answers (D6 / T4).
                "device": payload.get("reporter_device_id"),
                "modality": payload.get("modality", "text"),
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
    sources = [_source_of(it) for it in top]

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


def _source_of(it: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": it["id"],
        "score": it["score"],
        "value": it["value"],
        "consensus_state": it["consensus_state"],
        "device": it.get("device"),
        "modality": it.get("modality", "text"),
    }


# Citation marker like "[3]" used by the cited-answer path (D6 / T4).
_CITATION_RE = re.compile(r"\[(\d+)\]")


def _cite_instruction(question: str) -> str:
    return (
        f"{question}\n\n"
        "After EACH sentence, cite the fact that supports it using its bracketed "
        "number from the context, e.g. [1]. Use only those numbered facts; if a "
        "sentence has no supporting fact, do not invent a citation."
    )


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p for p in parts if p.strip()]


def _parse_cited_sentences(text: str, n_sources: int) -> List[Dict[str, Any]]:
    """Split generated text into sentences and validate each sentence's citation
    markers against the retrieved set (1..n_sources). A sentence is `supported`
    only when it carries at least one marker and EVERY marker is in range; an
    out-of-range or missing marker makes it unsupported (D6 / T4)."""
    sentences: List[Dict[str, Any]] = []
    for part in _split_sentences(text):
        markers = [int(m) for m in _CITATION_RE.findall(part)]
        valid = [m for m in markers if 1 <= m <= n_sources]
        supported = bool(markers) and all(1 <= m <= n_sources for m in markers)
        clean = _CITATION_RE.sub("", part)
        clean = re.sub(r"\s+", " ", clean).strip()
        sentences.append({"text": clean, "citations": valid, "supported": supported})
    return sentences


def _extractive_cited(top: List[Dict[str, Any]], model: str) -> AnswerResult:
    """Build a cited answer by CONSTRUCTION from the numbered context, so every
    sentence is grounded and carries a valid citation (D6 / T4)."""
    sentences = [
        {"text": it["value"], "citations": [i], "supported": True}
        for i, it in enumerate(top, 1)
        if it["value"]
    ]
    if sentences:
        answer = "Based on local records: " + " ".join(
            f"{s['text']} [{s['citations'][0]}]" for s in sentences
        )
    else:
        answer = "No local records match that question."
    return AnswerResult(
        answer=answer, answer_path="extractive", model=model,
        sources=[_source_of(it) for it in top], sentences=sentences,
    )


def generate_cited(
    question: str,
    retrieved: List[Any],
    generators: List[Any],
    *,
    top_k: int = 5,
) -> AnswerResult:
    """Grounded answer WITH per-sentence citations (D6 / T4).

    The context is numbered `[1]…[n]`; a model generator is asked to cite each
    sentence with a bracketed number, and every marker is validated against the
    retrieved set (an out-of-range or missing marker marks that sentence
    `supported: false`). The extractive generator (and the fall-through when every
    model generator fails) cites by construction, so a cited answer is always
    available. Sources carry `device` + `modality` provenance.
    """
    top = select_context(retrieved, top_k)
    numbered = [f"[{i}] {it['context_line']}" for i, it in enumerate(top, 1)]
    sources = [_source_of(it) for it in top]

    last_error: Optional[Exception] = None
    for generator in generators:
        if getattr(generator, "answer_path", "") == "extractive":
            # Cite by construction — no model, no markers to validate.
            return _extractive_cited(top, getattr(generator, "name", "extractive"))
        try:
            text = generator.generate(_cite_instruction(question), numbered)
        except Exception as e:
            last_error = e
            continue
        sentences = _parse_cited_sentences(text, len(top))
        return AnswerResult(
            answer=text,
            answer_path=getattr(generator, "answer_path", "offline"),
            model=getattr(generator, "name", "unknown"),
            sources=sources,
            sentences=sentences,
        )

    # Every model generator failed and no extractive one was in the chain: build
    # the cited answer by construction rather than returning nothing.
    return _extractive_cited(top, "extractive")
