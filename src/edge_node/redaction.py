"""PII redaction (D5 / T7) — pure text redaction, no I/O.

`redact(text, patterns)` finds every substring matching any PII pattern and
replaces it with a fixed placeholder, returning the redacted text and the spans
it removed. It is used on the push path so a fact leaves the device with its PII
stripped from EVERY field the hub sees — value, dense vector (re-embedded from
the redacted text) and sparse BM25 (recomputed from the redacted text). The raw
copy stays on the device only.

Design notes:
  * replacement is RIGHT-TO-LEFT (highest offset first) so each edit never shifts
    the offsets of the matches still to be applied — a left-to-right pass would
    corrupt every subsequent span once the placeholder length differs from the
    match length;
  * overlapping matches (e.g. two patterns hitting the same run) are resolved to
    one span so a region is never double-replaced;
  * pure and deterministic: the same text + patterns always yield the same
    redacted text and spans, so the re-embedded vectors are reproducible.
"""

import re
from typing import Any, Dict, List, Tuple

# What a redacted span is replaced with. A fixed, non-sensitive marker.
REDACTION_PLACEHOLDER = "[REDACTED]"


def redact(
    text: str,
    patterns: List[Any],
    placeholder: str = REDACTION_PLACEHOLDER,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Return `(redacted_text, spans)`.

    `patterns` may be regex strings or pre-compiled patterns. `spans` is the list
    of removed regions in ORIGINAL-text coordinates, each
    `{"start", "end", "original", "placeholder"}`, left-to-right. The `original`
    is kept for the device's LOCAL redaction record only and must never be put on
    an outbound envelope.
    """
    if not text or not patterns:
        return text, []

    compiled = [
        re.compile(p, re.IGNORECASE) if isinstance(p, str) else p for p in patterns
    ]

    raw_matches: List[Tuple[int, int, str]] = []
    for rx in compiled:
        for m in rx.finditer(text):
            if m.end() > m.start():  # ignore zero-width matches
                raw_matches.append((m.start(), m.end(), m.group(0)))
    if not raw_matches:
        return text, []

    # Resolve overlaps: longest match first at each start, then skip any span that
    # begins inside one already accepted.
    raw_matches.sort(key=lambda t: (t[0], -t[1]))
    resolved: List[Tuple[int, int, str]] = []
    last_end = -1
    for s, e, g in raw_matches:
        if s >= last_end:
            resolved.append((s, e, g))
            last_end = e

    # Replace right-to-left so earlier offsets remain valid as we edit.
    out = text
    for s, e, _g in sorted(resolved, key=lambda t: t[0], reverse=True):
        out = out[:s] + placeholder + out[e:]

    spans = [
        {"start": s, "end": e, "original": g, "placeholder": placeholder}
        for s, e, g in sorted(resolved, key=lambda t: t[0])
    ]
    return out, spans
