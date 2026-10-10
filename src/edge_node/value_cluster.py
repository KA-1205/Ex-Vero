"""Semantic value clustering helpers (B5 / P1-5) — pure, dependency-free.

Two reports of "the same" real-world fact should fold together even when their
wording differs (paraphrases), but must NOT be merged when they actually
disagree. Embedding similarity alone cannot tell "5 bar" from "50 bar" or "safe"
from "not safe" — the vectors are near-identical — so a hard **contradiction
guard** runs first: differing numbers or opposite negation polarity force
different clusters no matter how similar the text looks.

This module is importable by both the edge (`decision_engine`) and the Cloud
Gateway (`app.py`), so the same guard protects novelty/REJECT on the edge and
value grouping on the hub. It is pure (no deps, no clock) by design.
"""

import hashlib
import re
from typing import Any, Optional, Tuple

# Default cosine above which two non-contradicting values are the same cluster.
DEFAULT_CLUSTER_SIM = 0.86

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# Negation / polarity markers. "safe" vs "not safe" and "clear" vs "not clear"
# must land in different clusters, so a flipped polarity is a contradiction.
_NEG_RE = re.compile(
    r"\b(?:not|no|never|without|cannot|can't|won't|don't|didn't|isn't|aren't|"
    r"unsafe|absent|clear|negative)\b",
    re.IGNORECASE,
)


def extract_numbers(value: Optional[str]) -> Tuple[float, ...]:
    """The sorted set of numbers mentioned, so '5 bar' and '50 bar' differ."""
    return tuple(sorted(float(m) for m in _NUM_RE.findall(value or "")))


def is_negated(value: Optional[str]) -> bool:
    """Whether the value carries a negation/absence marker ('not safe')."""
    return bool(_NEG_RE.search(value or ""))


def signature(value: Optional[str]) -> Tuple[Tuple[float, ...], bool]:
    """The contradiction signature: (numbers, negation polarity)."""
    return (extract_numbers(value), is_negated(value))


def contradicts(a: Optional[str], b: Optional[str]) -> bool:
    """True when two values disagree on a number or on negation polarity — they
    can never share a cluster, however similar their embeddings are."""
    return signature(a) != signature(b)


def cluster_id(corroboration_key: Any, representative_value: Optional[str]) -> str:
    """Deterministic cluster id from the key + the cluster's representative value
    (never `hash()`), so the same cluster is stable across processes and runs."""
    raw = f"{corroboration_key}\x00{representative_value}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def cosine(a, b) -> float:
    """Plain cosine similarity for two equal-length dense vectors."""
    if not a or not b:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))
