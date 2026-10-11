#!/usr/bin/env python3
"""C5 CI gate: fail if a fabricated/tunable literal reappears in logic modules.

The Ex-Vero brief (§6) and AGENTS.md forbid bare numeric/string literals in logic
modules: tunables belong in config under a named key, and anything shown to a
user must come from running code, not a hand-written constant.

This scanner greps the production logic modules for a curated list of literals
that were previously hardcoded (fabricated UI numbers, magic thresholds) and
exits non-zero if any reappears. A line may opt out with a trailing
``# hardcode-ok: <reason>`` marker for a legitimately scripted value (e.g. the
gated demo-theater endpoint), which keeps the exception explicit and auditable.

Run:  python tools/check_hardcoded.py
Exit: 0 = clean, 1 = a forbidden literal reappeared.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# (module path relative to repo root) -> list of forbidden literal regexes.
# Each regex targets a specific literal that must live in config / be computed.
FORBIDDEN: dict[str, List[str]] = {
    "src/edge_node/main.py": [
        r"\[3,\s*5,\s*2,\s*8,\s*1\]",          # fabricated activity_sparkline
        r"next_backoff_ms[\"']?\s*[:=]\s*\d+", # fixed backoff instead of computed
        r"0\.42\b",                             # fabricated device trust (rogue)
        r"0\.92\b",                             # fabricated device trust (camera)
        r"\b0\.88\b",                           # fabricated device trust (default)
        r"0\.91\b",                             # fabricated vision confidence
        r"\"bytes\"\s*:\s*0\b",                 # zero-placeholder push summary
    ],
    "src/edge_node/decision_engine.py": [
        r"-\s*1e-9\b",                          # inline float epsilon
        r"-\s*0\.02\b",                         # inline reject margin
        r"\breturn\s+0\.75\b",                  # inline novelty baseline
    ],
}

OPT_OUT = re.compile(r"#\s*hardcode-ok\b")


def scan() -> List[Tuple[str, int, str, str]]:
    """Return a list of (file, lineno, pattern, line) violations."""
    violations: List[Tuple[str, int, str, str]] = []
    for rel_path, patterns in FORBIDDEN.items():
        path = REPO_ROOT / rel_path
        if not path.exists():
            print(f"WARNING: {rel_path} not found; skipping", file=sys.stderr)
            continue
        compiled = [(p, re.compile(p)) for p in patterns]
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if OPT_OUT.search(line):
                continue
            for raw, rx in compiled:
                if rx.search(line):
                    violations.append((rel_path, lineno, raw, line.strip()))
    return violations


def main() -> int:
    violations = scan()
    if not violations:
        print("check_hardcoded: OK — no forbidden literals in logic modules")
        return 0
    print("check_hardcoded: FAIL — forbidden literals reappeared:\n")
    for rel_path, lineno, pattern, line in violations:
        print(f"  {rel_path}:{lineno}  /{pattern}/")
        print(f"      {line}")
    print(
        "\nMove the value into config under a named key (and read it with a "
        "documented default), or compute it from running code. If the literal is "
        "a legitimately scripted value, annotate the line with "
        "'# hardcode-ok: <reason>'."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
