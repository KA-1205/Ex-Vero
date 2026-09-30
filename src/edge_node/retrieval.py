"""Retrieval — the offline hybrid query path (dense + BM25 fused, both shards).

This module is deliberately narrow: it imports **only** `qdrant_edge`. It must
never import the sync transport or the network simulator (AGENTS.md §5
invariant 8, backend.md §0.4 / §2.5). Local semantic search is the device's
core promise and has to behave identically offline, so the code that performs it
cannot reach the wire. A static import-graph test enforces this.

Hybrid = dense + BM25 fused with server-side RRF per shard (verified on
`qdrant-edge-py==0.8.0`, AGENTS.md §3.1), then the two shards' result sets are
merged in Python and deduped by point id (backend.md §9).
"""

from typing import List

from qdrant_edge import (
    Query,
    QueryRequest as EdgeQueryRequest,
    Prefetch,
    Fusion,
)


class RetrievedPoint:
    """A single fused, deduped hit. Plain container so callers don't depend on
    the concrete `ScoredPoint` type."""

    __slots__ = ("id", "score", "payload")

    def __init__(self, id, score, payload):
        self.id = id
        self.score = score
        self.payload = payload


def hybrid_query(
    mutable_shard,
    immutable_shard,
    dense_vector: List[float],
    sparse_vector,
    dense_name: str,
    limit: int = 10,
    prefetch_limit: int = 25,
) -> List[RetrievedPoint]:
    """Run the fused dense+BM25 query over both shards and return deduped hits,
    highest score first.

    A point pushed up and later pulled back lives in both shards; we keep the
    higher-scoring copy so it never appears twice.
    """
    dense_prefetch = Prefetch(
        limit=prefetch_limit,
        query=Query.Nearest(query=dense_vector, using=dense_name),
    )
    sparse_prefetch = Prefetch(
        limit=prefetch_limit,
        query=Query.Nearest(query=sparse_vector, using="text_bm25"),
    )
    edge_request = EdgeQueryRequest(
        limit=limit,
        prefetches=[dense_prefetch, sparse_prefetch],
        query=Fusion.Rrf(k=60),
        with_payload=True,
        with_vector=False,
    )

    seen = {}
    for shard in (mutable_shard, immutable_shard):
        for res in shard.query(edge_request):
            pid = res.id
            if pid not in seen or res.score > seen[pid].score:
                seen[pid] = res

    ordered = sorted(seen.values(), key=lambda x: x.score, reverse=True)
    return [RetrievedPoint(r.id, r.score, r.payload) for r in ordered]
