"""Extracted API or orchestration implementation.

The factory receives the existing runtime module: state and test overrides are
shared, not copied into a second shard/adapter/transport registry.
"""

from types import ModuleType
from fastapi import APIRouter


def register(runtime: ModuleType) -> APIRouter:
    router = APIRouter()

    @router.get("/benchmark/resolver-vs-lww")
    async def benchmark_resolver_vs_lww():
        """Phase 9 benchmark: trust-weighted resolver vs a last-write-wins baseline.

        Scores the REAL fold (`fold_consensus`, the exact module the Cloud Gateway
        runs) against a REAL LWW baseline — newest event by hub `seq` wins — over the
        labeled dispute scenarios in `tests/fixtures/conflict_facts.jsonl`.

        The fixture's scenarios are shaped like LWW's actual failure mode: several
        truthful devices report first, then a single low-trust device reports a wrong
        value LAST. LWW takes the late report; the fold backs the corroborated truth.
        Every number is recomputed on each call — nothing here is a constant, and
        nothing is tuned to hit a target. If the fold ever lost, this would say so.
        """
        facts = runtime.load_jsonl(runtime.CONFLICT_FIXTURE)
        threshold = float(runtime.CONSENSUS_THRESHOLD)
        result = runtime.score_resolver_vs_lww(facts, threshold)
        result["threshold"] = threshold
        result["fixture"] = "tests/fixtures/conflict_facts.jsonl"
        return result

    runtime.benchmark_resolver_vs_lww = benchmark_resolver_vs_lww

    def _benchmark_shard(corpus):
        """Build (once per process) the Qdrant Edge shard holding the labeled corpus.

        This is a real shard with real dense + BM25 vectors, so the recall benchmark
        exercises the same retrieval code the device uses — not a stand-in. It lives
        under its own `__benchmark__` device id so it never mixes with, or evicts
        from, a real device's memory.

        The corpus is seeded exactly once per process, then left alone. An earlier
        version re-seeded whenever the point count looked short, which meant the
        benchmark silently repaired any change made to the index and could only ever
        report the score of a pristine corpus — a benchmark that cannot observe its
        own index is not measuring it. After seeding, the score reflects the shard
        as it actually stands.
        """
        shards = runtime.get_or_create_shards(runtime.BENCH_DEVICE)
        mutable = shards["mutable"]
        if runtime.BENCH_DEVICE in runtime._bench_seeded:
            return mutable
        text_adapter = next(
            (a for a in runtime.adapters if a.modality == "text"), runtime.adapters[0]
        )
        points = []
        for record in corpus:
            text = record["text"]
            points.append(
                runtime.Point(
                    id=int(record["doc_id"]),
                    vector={
                        text_adapter.name: text_adapter.embed(text),
                        "text_bm25": runtime.bm25.embed_document(text),
                    },
                    payload={
                        "value": text,
                        "benchmark_doc_id": int(record["doc_id"]),
                        "model": text_adapter.name,
                        "model_version": text_adapter.version,
                        "corroboration_key": "benchmark.corpus",
                        "modality": "text",
                        "_sync_meta": {
                            "synced": 1,
                            "syncable": 0,
                            "sync_priority": "HELD",
                            "client_sequence": 0,
                        },
                    },
                )
            )
        mutable.update(runtime.UpdateOperation.upsert_points(points=points))
        mutable.optimize()
        runtime._bench_seeded.add(runtime.BENCH_DEVICE)
        return mutable

    runtime._benchmark_shard = _benchmark_shard

    @router.get("/benchmark/recall")
    async def benchmark_recall():
        """Phase 9 benchmark: fused hybrid recall@5 vs dense-only recall@5.

        Both legs are measured over one labeled query set against one real Qdrant
        Edge shard holding the labeled corpus, so the comparison is apples-to-apples
        (backend.md §9). A query counts as recalled when a known-relevant point id
        comes back in the top 5.

        The corpus is indexed into a dedicated benchmark shard rather than read from a
        device's live captures, because captured facts carry no relevance labels —
        scoring against them would measure nothing. Everything is recomputed on each
        call; no number here is a constant.
        """
        corpus = runtime.load_jsonl(runtime.RECALL_CORPUS_FIXTURE)
        queries = runtime.load_jsonl(runtime.RECALL_FIXTURE)
        shard = runtime._benchmark_shard(corpus)
        text_adapter = next(
            (a for a in runtime.adapters if a.modality == "text"), runtime.adapters[0]
        )
        imm = runtime.device_shards[runtime.BENCH_DEVICE]["immutable"]

        def dense_rank(query_text: str) -> runtime.List[int]:
            vec = text_adapter.embed(query_text)
            req = runtime.EdgeQueryRequest(
                limit=runtime.RECALL_K,
                query=runtime.Query.Nearest(query=vec, using=text_adapter.name),
                with_payload=False,
                with_vector=False,
            )
            return [r.id for r in shard.query(req)]

        def hybrid_rank(query_text: str) -> runtime.List[int]:
            vec = text_adapter.embed(query_text)
            return [
                h.id
                for h in runtime.hybrid_query(
                    shard,
                    imm,
                    dense_vector=vec,
                    sparse_vector=runtime.bm25.embed_query(query_text),
                    dense_name=text_adapter.name,
                    limit=runtime.RECALL_K,
                    prefetch_limit=runtime.RECALL_PREFETCH_K,
                )
            ]

        result = runtime.score_recall(
            queries, dense_rank, hybrid_rank, k=runtime.RECALL_K
        )
        result["corpus_points"] = len(corpus)
        result["fixture"] = "tests/fixtures/recall_queries.jsonl"
        result["model"] = text_adapter.name
        result["model_version"] = text_adapter.version
        return result

    runtime.benchmark_recall = benchmark_recall

    return router
