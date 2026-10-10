"""Phase 2 / D1 (T10) — crash safety.

Captured facts must survive a hard crash (SIGKILL), not just a graceful exit.
This drives the real harness in `tools/crash_test.py`: a subprocess edge node
captures facts against a temp shard dir, is SIGKILLed, restarts against the same
dir, and we assert every fact came back. It fails on the pre-fix code (optimize()
alone did not persist through SIGKILL) and passes once writes are flushed.

Verify-first (D1): the shard dir is `EDGE_SHARD_DIR`-overridable, so the harness
uses a throwaway temp dir and never touches the repo's ./shards.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from crash_test import run_crash_test  # noqa: E402


def test_captured_facts_survive_a_sigkill():
    result = run_crash_test(n=6, quiet=True)
    assert result["captured"] == 6
    assert result["recovered"] == result["captured"], result
    assert result["lost"] == 0
    assert result["durable"] is True
