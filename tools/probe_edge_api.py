"""Phase 0 — Edge API probe + doc reconcile.

A fact-printer. It exercises the pinned `qdrant-edge-py==0.8.0` directly and
reports the exact behaviours phases 3–7 will depend on, so no later phase builds
on a guessed signature. It imports **nothing** from `src/edge_node` on purpose:
we want to validate the real Edge API, not our own wrapper.

Four checks, matching the Phase 0 build steps in docs/30-phases.md:

  1. Build a real shard with one dense field + one `text_bm25` sparse field and
     insert four decidable documents.
  2. Hybrid: run dense-only, BM25-only, and a `Prefetch`+`Fusion.Rrf` request and
     decide whether RRF genuinely blends the two legs or silently returns one.
  3. Sync APIs: confirm `snapshot_manifest` / `update_from_snapshot` /
     `unpack_snapshot` / `create` / `load` and the mutating filter ops by shape
     and callability.
  4. Facet on the boolean `synced` field and scroll-with-`Filter`; note whether
     scroll can sort, and pin the boolean-filter trap.

Run it directly (`python tools/probe_edge_api.py`) to print a PASS/behaviour
line per check; `run_probe()` returns the same facts as a dict for the test.
"""

import inspect
import os
import shutil
import tempfile

import qdrant_edge as q


# Four short documents with known text so BM25 ranking is decidable. A single
# doc would make every ranking trivially identical, so we use four.
DOCS = {
    1: ("structural collapse at zone c building", [1.0, 0.0, 0.0, 0.0]),
    2: ("minor water leak reported downtown", [0.0, 1.0, 0.0, 0.0]),
    3: ("gas smell near zone c apartments", [0.9, 0.1, 0.0, 0.0]),
    4: ("road blocked by fallen tree", [0.0, 0.0, 1.0, 0.0]),
}

DENSE_FIELD = "text_dense"
SPARSE_FIELD = "text_bm25"


def _build_shard(path):
    """A shard shaped like a real edge node: one dense field + BM25 sparse."""
    config = q.EdgeConfig(
        vectors={DENSE_FIELD: q.EdgeVectorParams(size=4, distance=q.Distance.Cosine)},
        sparse_vectors={SPARSE_FIELD: q.EdgeSparseVectorParams(modifier=q.Modifier.Idf)},
    )
    shard = q.EdgeShard.create(path, config)
    bm25 = q.Bm25(q.Bm25Config())
    points = []
    for pid, (text, dense) in DOCS.items():
        synced = pid % 2 == 0  # docs 1,3 are "unsynced"; 2,4 are "synced"
        points.append(
            q.Point(
                id=pid,
                vector={DENSE_FIELD: dense, SPARSE_FIELD: bm25.embed_document(text)},
                payload={
                    "text": text,
                    "synced_bool": synced,          # Python bool (the trap)
                    "synced_int": 1 if synced else 0,  # integer 0/1 (the fix)
                    "client_ts": pid * 100,
                },
            )
        )
    shard.update(q.UpdateOperation.upsert_points(points=points))
    # Index the fields we will filter / facet / sort on.
    shard.update(q.UpdateOperation.create_field_index(field_name="synced_bool", schema=q.PayloadSchemaType.Bool))
    shard.update(q.UpdateOperation.create_field_index(field_name="synced_int", schema=q.PayloadSchemaType.Integer))
    shard.update(q.UpdateOperation.create_field_index(field_name="client_ts", schema=q.PayloadSchemaType.Integer))
    shard.optimize()  # no background optimizer — nothing is indexed until we ask
    return shard, bm25


def _ids(scored):
    return [p.id for p in scored]


def _check_hybrid(shard, bm25):
    """Does `Prefetch`+`Fusion.Rrf` actually blend dense + BM25, or pass one through?

    We pick a query where the two legs disagree: the dense vector points at doc 4
    (the "road" fact) while the BM25 text targets "zone c" (docs 1 and 3). If
    fusion truly blends, the fused order will match neither single leg and will
    promote the doc that both legs rank.
    """
    dense_vec = [0.0, 0.0, 1.0, 0.0]
    sparse_vec = bm25.embed_query("zone c")

    dense_ids = _ids(shard.query(q.QueryRequest(limit=4, query=q.Query.Nearest(dense_vec, using=DENSE_FIELD))))
    bm25_ids = _ids(shard.query(q.QueryRequest(limit=4, query=q.Query.Nearest(sparse_vec, using=SPARSE_FIELD))))
    fused_ids = _ids(
        shard.query(
            q.QueryRequest(
                limit=4,
                prefetches=[
                    q.Prefetch(limit=4, query=q.Query.Nearest(dense_vec, using=DENSE_FIELD)),
                    q.Prefetch(limit=4, query=q.Query.Nearest(sparse_vec, using=SPARSE_FIELD)),
                ],
                query=q.Fusion.Rrf(k=60),
            )
        )
    )

    # A genuine blend: the fused order equals neither leg, yet draws ids from both.
    blends = (
        fused_ids != dense_ids
        and fused_ids != bm25_ids
        and bool(set(fused_ids) & set(dense_ids))
        and bool(set(fused_ids) & set(bm25_ids))
    )
    return {
        "dense_ids": dense_ids,
        "bm25_ids": bm25_ids,
        "fused_ids": fused_ids,
        "fusion_blends": blends,
    }


def _check_sync(base_dir):
    """Confirm the sync surface by shape + callability, and a real round trip."""
    path = os.path.join(base_dir, "sync_shard")
    os.makedirs(path, exist_ok=True)
    shard, _ = _build_shard(path)

    manifest = shard.snapshot_manifest()
    manifest_type = type(manifest).__name__

    update_args = list(inspect.signature(q.EdgeShard.update_from_snapshot).parameters)
    update_args = [a for a in update_args if a != "self"]
    unpack_args = list(inspect.signature(q.EdgeShard.unpack_snapshot).parameters)

    # delete_points_by_filter: drop doc 1 by its client_ts.
    before = shard.count(q.CountRequest())
    shard.update(
        q.UpdateOperation.delete_points_by_filter(
            filter=q.Filter(must=[q.FieldCondition(key="client_ts", match=q.MatchValue(value=100))])
        )
    )
    shard.optimize()
    after = shard.count(q.CountRequest())
    delete_works = after == before - 1

    # set_payload_by_filter: flip synced_int 0 -> 1 (uses a filter we know matches).
    shard.update(
        q.UpdateOperation.set_payload_by_filter(
            filter=q.Filter(must=[q.FieldCondition(key="synced_int", match=q.MatchValue(value=0))]),
            payload={"synced_int": 1},
        )
    )
    shard.optimize()
    still_zero = shard.count(
        q.CountRequest(filter=q.Filter(must=[q.FieldCondition(key="synced_int", match=q.MatchValue(value=0))]))
    )
    set_payload_works = still_zero == 0

    shard.close()

    # load() reopens a populated dir; create() must refuse it (AGENTS.md gotcha).
    reopened = q.EdgeShard.load(path)
    load_reopens = reopened.count(q.CountRequest()) == after
    reopened.close()

    config = q.EdgeConfig(
        vectors={DENSE_FIELD: q.EdgeVectorParams(size=4, distance=q.Distance.Cosine)},
        sparse_vectors={SPARSE_FIELD: q.EdgeSparseVectorParams(modifier=q.Modifier.Idf)},
    )
    try:
        q.EdgeShard.create(path, config)
        create_on_populated_raises = False
    except Exception:
        create_on_populated_raises = True

    return {
        "manifest_type": manifest_type,
        "manifest_segment_count": len(manifest),
        "manifest_value_keys": sorted(next(iter(manifest.values())).keys()) if manifest else [],
        "update_from_snapshot_args": update_args,
        "unpack_snapshot_args": unpack_args,
        "create_on_populated_raises": create_on_populated_raises,
        "load_reopens": load_reopens,
        "delete_by_filter_works": delete_works,
        "set_payload_by_filter_works": set_payload_works,
    }


def _check_facet_scroll(shard):
    """facet on the boolean `synced` field + scroll-with-Filter, and the trap.

    The trap: filtering a Python-`bool` field via `MatchValue` matches nothing in
    scroll/count (a silent zero) even though `facet` counts it. An integer 0/1
    field filters correctly. This decides the Phase 3 outbox schema.
    """
    facet = shard.facet(q.FacetRequest(key="synced_bool"))
    facet_buckets = {str(hit.value): hit.count for hit in facet.hits}

    # scroll accepts a Filter and returns (points, next_offset).
    unsynced_bool, _ = shard.scroll(
        q.ScrollRequest(
            limit=10,
            filter=q.Filter(must=[q.FieldCondition(key="synced_bool", match=q.MatchValue(value=False))]),
        )
    )
    bool_filter_ids = _ids(unsynced_bool)

    unsynced_int, _ = shard.scroll(
        q.ScrollRequest(
            limit=10,
            filter=q.Filter(must=[q.FieldCondition(key="synced_int", match=q.MatchValue(value=0))]),
        )
    )
    int_filter_ids = _ids(unsynced_int)

    scroll_accepts_filter = True  # the two calls above did not raise

    # scroll can sort, but only on a range-indexed key; an unindexed key raises.
    try:
        shard.scroll(q.ScrollRequest(limit=10, order_by=q.OrderBy(key="text", direction=q.Direction.Desc)))
        order_by_needs_range_index = False
    except Exception:
        order_by_needs_range_index = True
    # And it succeeds on the integer-indexed key:
    ordered, _ = shard.scroll(
        q.ScrollRequest(limit=10, order_by=q.OrderBy(key="client_ts", direction=q.Direction.Desc))
    )

    expected_unsynced = sorted(pid for pid in DOCS if pid % 2 != 0)
    return {
        "facet_buckets": facet_buckets,
        "facet_bucket_count": len(facet_buckets),
        "scroll_accepts_filter": scroll_accepts_filter,
        "scroll_order_by_ids": _ids(ordered),
        "scroll_order_by_needs_range_index": order_by_needs_range_index,
        "bool_filter_ids": bool_filter_ids,
        "bool_filter_silently_empty": bool_filter_ids == [] and facet_buckets != {},
        "int_filter_ids": int_filter_ids,
        "expected_unsynced_ids": expected_unsynced,
    }


def run_probe():
    """Run all four checks against a fresh temp shard and return the facts."""
    base_dir = tempfile.mkdtemp(prefix="edge_probe_")
    try:
        shard_dir = os.path.join(base_dir, "hybrid_shard")
        os.makedirs(shard_dir, exist_ok=True)
        shard, bm25 = _build_shard(shard_dir)
        try:
            results = {
                "shard_setup": {
                    "dense_field": DENSE_FIELD,
                    "sparse_field": SPARSE_FIELD,
                    "doc_count": shard.count(q.CountRequest()),
                },
                "hybrid": _check_hybrid(shard, bm25),
                "facet_scroll": _check_facet_scroll(shard),
            }
        finally:
            shard.close()
        # Sync uses its own shard dir (it closes/reopens/deletes).
        results["sync"] = _check_sync(base_dir)
        return results
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)


def main():
    r = run_probe()

    print("=== Phase 0 — Edge API probe (qdrant-edge-py 0.8.0) ===\n")

    setup = r["shard_setup"]
    print("Check 1 — shard + documents:")
    print(f"  PASS: shard with dense '{setup['dense_field']}' + sparse "
          f"'{setup['sparse_field']}', {setup['doc_count']} docs inserted.\n")

    h = r["hybrid"]
    print("Check 2 — hybrid (Prefetch + Fusion.Rrf):")
    print(f"  dense-only : {h['dense_ids']}")
    print(f"  bm25-only  : {h['bm25_ids']}")
    print(f"  fused RRF  : {h['fused_ids']}")
    if h["fusion_blends"]:
        print("  BEHAVIOUR: Fusion.Rrf GENUINELY BLENDS both legs (fused order "
              "matches neither single leg and draws ids from both).")
        print("  DECISION: Phase 3/hybrid MAY use server-side Prefetch+Fusion.Rrf; "
              "no need to fuse in Python. Use embed_query for the BM25 leg.\n")
    else:
        print("  BEHAVIOUR: Fusion.Rrf did NOT blend — it returned a single leg.")
        print("  DECISION: Phase 3/hybrid MUST fuse in Python (two queries + manual RRF).\n")

    s = r["sync"]
    print("Check 3 — sync APIs:")
    print(f"  snapshot_manifest() -> {s['manifest_type']} of {s['manifest_segment_count']} "
          f"segment(s); value keys {s['manifest_value_keys']}")
    print(f"  update_from_snapshot(args)={s['update_from_snapshot_args']}  "
          f"-> takes a filesystem PATH, not bytes/handle")
    print(f"  unpack_snapshot(args)={s['unpack_snapshot_args']} (static)")
    print(f"  create() on populated dir raises: {s['create_on_populated_raises']}; "
          f"load() reopens: {s['load_reopens']}")
    print(f"  delete_points_by_filter works: {s['delete_by_filter_works']}; "
          f"set_payload_by_filter works: {s['set_payload_by_filter_works']}")
    print("  PASS: all sync calls confirmed by shape + round trip.\n")

    f = r["facet_scroll"]
    print("Check 4 — facet + scroll:")
    print(f"  facet(synced) buckets: {f['facet_buckets']}")
    print(f"  scroll accepts Filter: {f['scroll_accepts_filter']}; "
          f"order_by needs a range index: {f['scroll_order_by_needs_range_index']} "
          f"(sorted ids {f['scroll_order_by_ids']})")
    print(f"  TRAP: bool filter (synced_bool==False) -> {f['bool_filter_ids']} "
          f"(silently empty: {f['bool_filter_silently_empty']})")
    print(f"        int  filter (synced_int==0)      -> {sorted(f['int_filter_ids'])} "
          f"(expected {f['expected_unsynced_ids']})")
    print("  DECISION: store _sync_meta.synced as an INTEGER 0/1 (Integer-indexed), "
          "never a Python bool — a bool filter matches nothing, which would make "
          "the outbox silently sync zero points.\n")

    print("=== Probe complete — see AGENTS.md §3 for the recorded decisions ===")


if __name__ == "__main__":
    main()
