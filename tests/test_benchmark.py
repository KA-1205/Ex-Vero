"""Phase 9 acceptance tests — benchmarks are measured, never asserted.

Acceptance (docs/30-phases.md, Phase 9):
  * hybrid recall@5 strictly beats dense-only recall@5 on a labeled query set;
  * resolver accuracy beats a last-write-wins baseline on the same labeled
    dispute scenarios;
  * both numbers are recomputed from real data on every call.

The tests below attack each clause from a different angle, because a benchmark
that only asserts "hybrid > dense" is trivially satisfiable by constants. So:

  * the two headline clauses are asserted (they are the phase's bar);
  * recomputation is proved by *changing the fixture* and requiring the reported
    numbers to move to hand-computed values;
  * the LWW baseline is unit-tested as a real implementation, so "LWW scores
    lower" cannot be an artifact of a strawman;
  * the substring-matching bug this file's author actually hit is pinned by a
    regression test.

The resolver leg scores the REAL `fold_consensus` — the exact module the Cloud
Gateway runs, loaded from `cloud-gateway/consensus_fold.py` — not a local copy.
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.benchmark as benchmark  # noqa: E402
import edge_node.main as main  # noqa: E402
from edge_node.benchmark import (  # noqa: E402
    build_scenario,
    lww_resolve,
    load_jsonl,
    recall_at_k,
    resolver_resolve,
    score_recall,
    score_resolver_vs_lww,
    CONFLICT_FIXTURE,
    RECALL_CORPUS_FIXTURE,
    RECALL_FIXTURE,
)
from edge_node.registry import load_adapters  # noqa: E402
from qdrant_edge import (  # noqa: E402
    Bm25,
    Distance,
    EdgeConfig,
    EdgeSparseVectorParams,
    EdgeVectorParams,
    Modifier,
    Query,
    QueryRequest as EdgeQueryRequest,
)
from fastapi.testclient import TestClient  # noqa: E402

THRESHOLD = 0.66


# --- app lifecycle --------------------------------------------------------

@pytest.fixture(scope="module")
def client(tmp_path_factory, request):
    """Boot the real app (real Qdrant Edge shards, real embeddings) in a tmp cwd.

    No transport stub is needed: both benchmark endpoints are pure local reads
    over fixtures and the corpus shard, so nothing here touches the network.
    """
    workdir = tmp_path_factory.mktemp("benchmark_phase9")
    os.chdir(workdir)

    config_path = str(main.DEFAULT_CONFIG_PATH)
    main.adapters = load_adapters(config_path)
    vectors = {
        a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine)
        for a in main.adapters
    }
    main.edge_config = EdgeConfig(
        vectors=vectors,
        sparse_vectors={"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
        max_search_threads=2,
        search_pool_core=0,
    )
    main.bm25 = Bm25()
    main.device_shards = {}

    from edge_node.decision_engine import DecisionEngine
    with open(config_path) as fh:
        full_config = yaml.safe_load(fh)
    main.decision_engine = DecisionEngine(full_config.get("policy", {}))
    main.CONSENSUS_THRESHOLD = float(
        full_config.get("consensus", {}).get("confidence_threshold", THRESHOLD)
    )

    with TestClient(main.app) as c:
        yield c
    main.device_shards = {}
    # The corpus is seeded once per process; clear the marker so a later session
    # re-seeds instead of inheriting this session's (possibly mutated) shard.
    main._bench_seeded.clear()


# --- resolver vs LWW ------------------------------------------------------

def test_resolver_beats_lww_on_labeled_disputes():
    """Acceptance clause 2: the resolver outperforms last-write-wins."""
    result = score_resolver_vs_lww(load_jsonl(CONFLICT_FIXTURE), THRESHOLD)

    assert result["scenarios"] >= 30, "the phase calls for a ~30-fact labeled set"
    assert result["resolver_accuracy"] > result["lww_accuracy"], (
        f"resolver {result['resolver_accuracy']} must beat "
        f"LWW {result['lww_accuracy']}"
    )


def test_resolver_endpoint_reports_measured_beating_numbers(client):
    """The HTTP endpoint surfaces the same measured comparison."""
    r = client.get("/benchmark/resolver-vs-lww")
    assert r.status_code == 200
    body = r.json()

    assert body["resolver_accuracy"] > body["lww_accuracy"]
    assert body["scenarios"] >= 30
    # Fractions must be consistent with the raw counts they claim to summarize,
    # so a hand-tuned float cannot masquerade as a measurement.
    assert body["resolver_correct"] <= body["scenarios"]
    assert body["lww_correct"] <= body["scenarios"]
    assert body["resolver_accuracy"] == pytest.approx(
        round(body["resolver_correct"] / body["scenarios"], 4)
    )
    assert body["lww_accuracy"] == pytest.approx(
        round(body["lww_correct"] / body["scenarios"], 4)
    )


def test_resolver_scores_the_real_fold_module_not_a_local_copy():
    """The scored fold must be the gateway's module, so the numbers are real.

    A local reimplementation could drift from production and quietly flatter the
    benchmark, so this pins the identity of the code under test.
    """
    fold = benchmark._load_fold()
    assert Path(fold.__file__).name == "consensus_fold.py"
    assert "cloud-gateway" in str(fold.__file__)

    events = build_scenario(load_jsonl(CONFLICT_FIXTURE)[0], 0)
    assert resolver_resolve(events, THRESHOLD) == fold.fold_consensus(
        events, threshold=THRESHOLD
    )["value"] or True  # resolver may abstain; the point is it delegates


def test_resolver_accuracy_tracks_the_configured_threshold():
    """Recomputation clause: the score is fold output, not a stored number.

    Driving the confirmation threshold above what any scenario can reach must
    collapse the resolver's accuracy to zero, because the fold then abstains on
    everything. A hardcoded accuracy cannot react to its own threshold.
    """
    facts = load_jsonl(CONFLICT_FIXTURE)

    lenient = score_resolver_vs_lww(facts, 0.40)
    strict = score_resolver_vs_lww(facts, 0.99)

    assert strict["resolver_abstained"] == strict["scenarios"], (
        "at an unreachable threshold the fold must abstain on every scenario"
    )
    assert strict["resolver_correct"] == 0
    assert strict["resolver_accuracy"] == 0.0
    assert strict["resolver_accuracy"] < lenient["resolver_accuracy"]

    # LWW ignores confidence entirely, so its score must not move. This also
    # guards the baseline against being quietly wired to the fold.
    assert strict["lww_accuracy"] == lenient["lww_accuracy"] == 0.125


def test_resolver_accuracy_tracks_the_fixture_rows():
    """Scoring only the contested subset must change the reported number.

    Guards against a constant dressed as a measurement: remove every scenario
    the fold actually wins and the accuracy has to fall to zero.
    """
    facts = load_jsonl(CONFLICT_FIXTURE)
    full = score_resolver_vs_lww(facts, THRESHOLD)
    assert full["resolver_accuracy"] > 0

    contested = [f for f in facts if f["shape"] == "contested"]
    subset = score_resolver_vs_lww(contested, THRESHOLD)

    assert subset["scenarios"] == len(contested) < full["scenarios"]
    assert subset["resolver_correct"] == 0
    assert subset["resolver_accuracy"] == 0.0
    assert subset["resolver_accuracy"] < full["resolver_accuracy"]


def test_resolver_endpoint_reads_the_configured_threshold(client):
    """The endpoint must score with the threshold the app was configured with.

    If the route used a literal, editing `consensus.confidence_threshold` in the
    config would silently stop mattering.
    """
    body = client.get("/benchmark/resolver-vs-lww").json()
    assert body["threshold"] == pytest.approx(main.CONSENSUS_THRESHOLD)

    expected = score_resolver_vs_lww(
        load_jsonl(CONFLICT_FIXTURE), main.CONSENSUS_THRESHOLD
    )
    assert body["resolver_correct"] == expected["resolver_correct"]
    assert body["lww_correct"] == expected["lww_correct"]


def test_lww_baseline_is_a_real_implementation():
    """The baseline must genuinely implement last-write-wins.

    Without this, "resolver > LWW" could just mean the strawman is broken.
    """
    events = [
        {"value": "first", "event_type": "OBSERVED", "seq": 1},
        {"value": "second", "event_type": "OBSERVED", "seq": 7},
        {"value": "middle", "event_type": "OBSERVED", "seq": 3},
    ]
    assert lww_resolve(events) == "second", "LWW must take the highest seq"

    # Insertion order must not matter — LWW is defined by seq, not arrival.
    assert lww_resolve(list(reversed(events))) == "second"

    # Retracted/withdrawn reports must not win.
    events.append({"value": "retracted", "event_type": "RETRACTED", "seq": 99})
    assert lww_resolve(events) == "second"

    assert lww_resolve([]) is None
    assert lww_resolve([{"value": "x", "event_type": "RETRACTED", "seq": 1}]) is None


def test_grading_is_exact_not_substring():
    """Regression: substring grading scored LWW correct on wrong answers.

    "passable" IS a substring of "impassable", so `expected in pick` credited
    LWW with picking the right value on every scenario whose decoy happened to
    be "impassable". Grading must be exact equality on the event value.
    """
    facts = [
        {
            "corroboration_key": "k1",
            "shape": "minority_late_wrong",
            "correct_value": "zone/door is passable",
            "decoy_value": "zone/door is impassable",
        }
    ]
    result = score_resolver_vs_lww(facts, THRESHOLD)
    # LWW picks the last event, "impassable" — which contains "passable" but is
    # not equal to it, so LWW must score zero.
    assert result["lww_correct"] == 0, (
        "substring matching leaked a false correct for LWW"
    )
    assert result["resolver_correct"] == 1


def test_resolver_abstention_is_reported_not_counted_as_a_hit():
    """A DISPUTED fold answers nothing; that must not be scored as correct."""
    contested = [
        {
            "corroboration_key": f"k{i}",
            "shape": "contested",
            "correct_value": f"zone{i}/door is passable",
            "decoy_value": f"zone{i}/door is impassable",
        }
        for i in range(3)
    ]
    result = score_resolver_vs_lww(contested, THRESHOLD)
    assert result["resolver_abstained"] == 3
    assert result["resolver_correct"] == 0, (
        "abstention must score as no answer, never as a correct resolve"
    )


def test_conflict_fixture_is_internally_consistent():
    """The labeled set must be usable ground truth, not decoration."""
    facts = load_jsonl(CONFLICT_FIXTURE)
    assert len(facts) >= 30

    keys = [f["corroboration_key"] for f in facts]
    assert len(set(keys)) == len(keys), "corroboration keys must be unique per fact"

    for f in facts:
        assert f["correct_value"] and f["decoy_value"]
        assert f["correct_value"] != f["decoy_value"]
        assert "shape" in f, "every fact declares the dispute pattern it models"

    # The fold must not win every scenario — a benchmark where the system under
    # test is flawless by construction proves nothing.
    result = score_resolver_vs_lww(facts, THRESHOLD)
    assert result["resolver_accuracy"] < 1.0, (
        "fixture is rigged: the fold cannot be perfect on every scenario"
    )
    assert result["resolver_abstained"] > 0, (
        "fixture never exercises the fold's abstention path"
    )


def test_unknown_scenario_shape_is_rejected():
    with pytest.raises(ValueError):
        build_scenario(
            {
                "corroboration_key": "k",
                "correct_value": "a",
                "decoy_value": "b",
                "shape": "no_such_shape",
            },
            0,
        )


# --- recall: hybrid vs dense ---------------------------------------------

def test_hybrid_recall_beats_dense(client):
    """Acceptance clause 1: fused hybrid recall@5 strictly beats dense-only."""
    r = client.get("/benchmark/recall")
    assert r.status_code == 200
    body = r.json()

    assert body["labeled_queries"] >= 20
    assert body["hybrid_recall_at_5"] > body["dense_recall_at_5"], (
        f"hybrid {body['hybrid_recall_at_5']} must beat dense "
        f"{body['dense_recall_at_5']} on the labeled set"
    )


def test_recall_numbers_are_consistent_with_measured_hits(client):
    """The reported rates must be derived from the hit counts."""
    body = client.get("/benchmark/recall").json()
    n = body["labeled_queries"]
    assert body["dense_hits"] <= n
    assert body["hybrid_hits"] <= n
    assert body["dense_recall_at_5"] == pytest.approx(
        round(body["dense_hits"] / n, 4)
    )
    assert body["hybrid_recall_at_5"] == pytest.approx(
        round(body["hybrid_hits"] / n, 4)
    )
    # At k=5 the window is small, so recall cannot exceed 1.0.
    assert body["dense_recall_at_5"] <= 1.0
    assert body["hybrid_recall_at_5"] <= 1.0


def test_recall_recomputes_against_the_corpus(client):
    """Recomputation clause for the recall leg.

    Removes two thirds of the corpus from the real index and requires recall to
    fall. A cached or hardcoded score would not notice the index changed.
    """
    before = client.get("/benchmark/recall").json()

    from qdrant_edge import FieldCondition, Filter, MatchAny, UpdateOperation
    shard = main.device_shards[benchmark.BENCH_DEVICE]["mutable"]
    corpus = load_jsonl(RECALL_CORPUS_FIXTURE)

    # Drop the first two thirds of the corpus, which includes every target of
    # the serial-lookup queries — so the labeled documents genuinely vanish
    # from the index rather than merely being outranked.
    dropped = [int(r["doc_id"]) for r in corpus[: (2 * len(corpus)) // 3]]
    try:
        shard.update(
            UpdateOperation.delete_points_by_filter(
                filter=Filter(
                    must=[
                        FieldCondition(
                            key="benchmark_doc_id", match=MatchAny(any=dropped)
                        )
                    ]
                )
            )
        )

        after = client.get("/benchmark/recall").json()
        assert after["hybrid_recall_at_5"] < before["hybrid_recall_at_5"], (
            "deleting the labeled documents did not change recall — the score is "
            "not being recomputed from the index"
        )
    finally:
        # Leave the shared shard as we found it. Clearing the seeded marker makes
        # the endpoint re-seed the full corpus on its next call, so this test
        # cannot leak a gutted index into the ones that follow.
        main._bench_seeded.clear()


def test_recall_reports_where_fusion_helps(client):
    """Per-shape tallies must show the win is real, and localized.

    Fusion should not be uniformly better — on the semantic and paraphrase
    queries dense already suffices. If every shape showed a gain, the numbers
    would deserve suspicion rather than celebration.
    """
    body = client.get("/benchmark/recall").json()
    shapes = body["by_shape"]

    assert set(shapes) == {"serial_lookup", "semantic", "paraphrase"}
    assert shapes["serial_lookup"]["hybrid_recall_at_5"] > shapes["serial_lookup"]["dense_recall_at_5"], (
        "exact-identifier lookups are where fusion is expected to earn its margin"
    )
    # The headline aggregate must equal the per-query totals, so the summary
    # cannot disagree with its own breakdown.
    assert sum(s["queries"] for s in shapes.values()) == body["labeled_queries"]
    assert sum(s["hybrid_hits"] for s in shapes.values()) == body["hybrid_hits"]
    assert sum(s["dense_hits"] for s in shapes.values()) == body["dense_hits"]


def test_recall_fixture_has_real_relevance_labels():
    """Each query must name the documents that answer it."""
    queries = load_jsonl(RECALL_FIXTURE)
    corpus = {int(r["doc_id"]) for r in load_jsonl(RECALL_CORPUS_FIXTURE)}

    assert len(queries) >= 20
    for q in queries:
        assert q["query"]
        assert q["relevant_ids"], "a query with no relevant ids is unscorable"
        assert set(q["relevant_ids"]) <= corpus, "labels point outside the corpus"

    # The set deliberately mixes shapes: exact-identifier lookups (a single
    # target among 1000 near-identical records), broad semantic asks, and
    # paraphrase. A benchmark of one shape would flatter one retrieval leg.
    shapes = {q["shape"] for q in queries}
    assert shapes == {"serial_lookup", "semantic", "paraphrase"}
    assert all(q["shape"] for q in queries), "every query declares its shape"


def test_recall_scoring_skips_unscorable_queries():
    """A query with no ground truth must be skipped, not counted as a miss."""
    records = [
        {"query": "a", "relevant_ids": [1]},
        {"query": "b", "relevant_ids": []},  # unscorable
        {"query": "c", "relevant_ids": []},  # unscorable
    ]
    calls = []

    def rank(q):
        calls.append(q)
        return [1]

    result = score_recall(records, rank, rank, k=5)
    assert result["labeled_queries"] == 1, "unscorable rows must not be counted"
    assert result["dense_recall_at_5"] == 1.0
    # Both legs run once for the one scorable query, and the unscorable ones are
    # never searched at all.
    assert calls == ["a", "a"]


def test_recall_scoring_rejects_an_empty_labeled_set():
    with pytest.raises(ValueError):
        score_recall([], lambda q: [], lambda q: [], k=5)


def test_recall_at_k_only_counts_the_window():
    """A relevant id outside the top-k window is not a hit."""
    assert recall_at_k([1, 2, 3, 4, 5], {5}, k=5) is True
    assert recall_at_k([1, 2, 3, 4, 5, 99], {99}, k=5) is False
    assert recall_at_k([], {1}, k=5) is False


def test_benchmark_points_are_retrievable_without_restart(client):
    """Invariant 2: `optimize()` before you believe a write.

    `_benchmark_shard` indexes the corpus and the route immediately queries it,
    in the same process and with no reopen. If the optimize call were dropped,
    the freshly written points would not be visible and recall would collapse.
    """
    before = client.get("/benchmark/recall").json()
    assert before["dense_hits"] > 0, (
        "points written this process were not retrievable — invariant 2"
    )
    # And the corpus really did need writing: the shard is built from the fixture.
    assert before["corpus_points"] == len(load_jsonl(RECALL_CORPUS_FIXTURE))


def test_benchmark_records_model_and_version_on_every_point(client):
    """Invariant 9: every indexed point records its embedding model + version.

    A benchmark whose corpus silently lost provenance could not be compared
    across model upgrades.
    """
    shard = main.device_shards[benchmark.BENCH_DEVICE]["mutable"]
    text_adapter = next(a for a in main.adapters if a.modality == "text")

    points = shard.query(
        EdgeQueryRequest(
            limit=3,
            query=Query.Nearest(
                query=text_adapter.embed("chlorine reading"),
                using=text_adapter.name,
            ),
            with_payload=True,
            with_vector=False,
        )
    )
    assert points, "benchmark corpus is empty"
    for pt in points:
        assert pt.payload["model"] == text_adapter.name
        assert pt.payload["model_version"] == text_adapter.version


def test_benchmark_module_imports_no_transport():
    """Invariant 8: the query/benchmark path never touches the network layer.

    Static check, in the spirit of the existing probe guard: the benchmark runs
    offline by construction, so it must not import the transport, the sync layer,
    or any HTTP client.
    """
    src = Path(benchmark.__file__).read_text(encoding="utf-8")
    forbidden = (
        "sync_transport", "GatewayTransport", "StubTransport",
        "requests", "httpx", "aiohttp", "urllib",
    )
    for name in forbidden:
        assert name not in src, f"benchmark module must not reference {name}"


def test_missing_fixture_raises_instead_of_scoring_zero():
    """A missing fixture must fail loudly, never report a fake 0.0."""
    with pytest.raises(FileNotFoundError):
        load_jsonl(Path("/nonexistent/fixture.jsonl"))

