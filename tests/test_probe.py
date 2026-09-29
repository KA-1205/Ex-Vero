"""Phase 0 acceptance test — the Edge API probe.

Phase 0 is a probe-only "reconcile" phase: we verify, against the pinned
`qdrant-edge-py==0.8.0`, the exact API behaviours that phases 3–7 will lean on,
so no later phase builds on a guessed signature or a silently-wrong call.

This test drives `tools/probe_edge_api.run_probe()` (which imports nothing from
`src/edge_node`) and asserts the *specific* behaviours it must report. It is
deliberately not trivial: it uses four decidable documents, proves RRF fusion
actually reorders relative to each single leg, and pins the boolean-filter trap
that would otherwise make the outbox silently sync nothing.
"""

import ast
import importlib.util
import os

TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "tools")
PROBE_PATH = os.path.join(TOOLS_DIR, "probe_edge_api.py")


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_edge_api", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_imports_nothing_from_src():
    """Guardrail: the probe is a fact-printer that must not couple to the app.

    Phase 0 says the probe "imports nothing from src/edge_node". We prove it
    statically so the probe can never accidentally validate our own wrapper
    instead of the real Edge API.
    """
    with open(PROBE_PATH) as fh:
        tree = ast.parse(fh.read())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any("edge_node" in name for name in imported), imported


def test_probe_runs_and_reports_four_checks():
    probe = _load_probe()
    results = probe.run_probe()
    # One structured result per Phase 0 check.
    for key in ("shard_setup", "hybrid", "sync", "facet_scroll"):
        assert key in results, f"probe missing check: {key}"


def test_check1_shard_with_dense_and_bm25_and_multiple_docs():
    """Check 1: a real shard with a dense field + a text_bm25 sparse field and
    3–4 docs (not a single doc, which would make ranking undecidable)."""
    results = _load_probe().run_probe()
    setup = results["shard_setup"]
    assert setup["dense_field"] == "text_dense"
    assert setup["sparse_field"] == "text_bm25"
    assert setup["doc_count"] >= 3


def test_check2_rrf_fusion_actually_blends_both_legs():
    """Check 2: the core reconcile question. `Prefetch`+`Fusion.Rrf` must
    genuinely blend the dense and BM25 rankings, not silently return one leg.

    We assert the fused order differs from *both* single-leg orders and that the
    fused result contains ids that came from each leg — the definition of a real
    blend rather than a pass-through."""
    hybrid = _load_probe().run_probe()["hybrid"]
    dense = hybrid["dense_ids"]
    bm25 = hybrid["bm25_ids"]
    fused = hybrid["fused_ids"]

    assert dense and bm25 and fused, (dense, bm25, fused)
    # A pass-through leg would make fused identical to one of the legs.
    assert fused != dense, "fusion silently returned the dense leg"
    assert fused != bm25, "fusion silently returned the BM25 leg"
    # A genuine blend surfaces ids that each leg ranked.
    assert set(fused) & set(dense)
    assert set(fused) & set(bm25)
    assert hybrid["fusion_blends"] is True


def test_check3_sync_api_signatures_and_round_trip():
    """Check 3: the sync surface phases 3–5 depend on, confirmed by callability
    and shape rather than assumption."""
    sync = _load_probe().run_probe()["sync"]
    # snapshot_manifest() returns a dict keyed by segment id.
    assert sync["manifest_type"] == "dict"
    assert sync["manifest_segment_count"] >= 1
    # update_from_snapshot takes a filesystem PATH, not bytes/handle.
    assert sync["update_from_snapshot_args"][0] == "snapshot_path"
    assert sync["unpack_snapshot_args"] == ["snapshot_path", "target_path"]
    # create() refuses a populated dir; load() reopens it (AGENTS.md gotcha).
    assert sync["create_on_populated_raises"] is True
    assert sync["load_reopens"] is True
    # The mutating filter ops actually take effect.
    assert sync["delete_by_filter_works"] is True
    assert sync["set_payload_by_filter_works"] is True


def test_check4_facet_and_scroll_and_the_bool_filter_trap():
    """Check 4: facet on the boolean `synced` field and scroll-with-Filter, plus
    the trap that decides the Phase 3 payload schema.

    Verified behaviour: a Python-`bool` field filtered via `MatchValue` matches
    *nothing* in scroll/count (a silent zero), even though `facet` still counts
    it. An integer 0/1 field filters correctly. The outbox must therefore store
    `synced` as an integer, or it would sync nothing and never notice."""
    fs = _load_probe().run_probe()["facet_scroll"]
    # facet works on the boolean field and counts both buckets.
    assert fs["facet_bucket_count"] == 2
    assert fs["scroll_accepts_filter"] is True
    # scroll can sort, but only on a range-indexed key.
    assert fs["scroll_order_by_needs_range_index"] is True
    # The trap: bool filter is silently empty; the integer field is the fix.
    assert fs["bool_filter_ids"] == []
    assert fs["bool_filter_silently_empty"] is True
    assert sorted(fs["int_filter_ids"]) == fs["expected_unsynced_ids"]
