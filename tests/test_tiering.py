"""D8 (T9) — tiered storage: thumbnail byte-cap demotion + orphan scan.

Two tiers: a `full` image point keeps its thumbnail; over the byte cap the oldest
SYNCED, unprotected image points are demoted to `embedding_only` — the thumbnail
file is deleted and `thumbnail_url` cleared, but the vector stays so the point is
still searchable. Pending (unsynced) and CONFIRMED/DISPUTED/URGENT points are
never demoted. A startup scan removes thumbnail files no point references.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qdrant_edge import (  # noqa: E402
    Bm25, Distance, EdgeConfig, EdgeShard, EdgeVectorParams, EdgeSparseVectorParams,
    Modifier, Point, Query, QueryRequest, UpdateOperation,
)
from edge_node.outbox import ensure_indexes  # noqa: E402
from edge_node.eviction import (  # noqa: E402
    TIER_EMBEDDING_ONLY, TIER_FULL, TIER_KEY,
    demote_images_over_thumbnail_cap, remove_orphan_thumbnails,
)


def _make_shard(path):
    os.makedirs(path, exist_ok=True)
    cfg = EdgeConfig(
        vectors={"text": EdgeVectorParams(size=3, distance=Distance.Cosine)},
        sparse_vectors={"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
        max_search_threads=2, search_pool_core=0,
    )
    sh = EdgeShard.create(path, cfg)
    ensure_indexes(sh)
    return sh


def _img_point(pid, seq, thumb_dir, *, synced=1, consensus=None, urgent=False, nbytes=100):
    """An image point with a real thumbnail file of `nbytes` bytes on disk."""
    name = f"thumb_{pid}.jpg"
    with open(os.path.join(thumb_dir, name), "wb") as fh:
        fh.write(b"x" * nbytes)
    meta = {"synced": synced, "syncable": 1, "client_sequence": seq,
            "sync_priority": "URGENT" if urgent else "ROUTINE"}
    payload = {"modality": "vision", "thumbnail_url": f"/thumbnails/{name}",
               "value": f"[photo {pid}]", "_sync_meta": meta, TIER_KEY: TIER_FULL}
    if consensus:
        payload["consensus_state"] = consensus
    return Point(id=pid, vector={"text": [0.1 * pid, 0.2, 0.3]}, payload=payload)


def _bytes_of(thumb_dir):
    def f(payload):
        url = payload.get("thumbnail_url")
        if not url:
            return 0
        try:
            return os.path.getsize(os.path.join(thumb_dir, os.path.basename(url)))
        except OSError:
            return 0
    return f


def _rm(thumb_dir):
    def f(payload):
        url = payload.get("thumbnail_url")
        if url:
            try:
                os.remove(os.path.join(thumb_dir, os.path.basename(url)))
            except OSError:
                pass
    return f


def test_demotes_oldest_synced_images_and_keeps_them_searchable():
    with tempfile.TemporaryDirectory() as tmp:
        thumb_dir = os.path.join(tmp, "thumbs"); os.makedirs(thumb_dir)
        sh = _make_shard(os.path.join(tmp, "shard"))
        sh.update(UpdateOperation.upsert_points(points=[
            _img_point(1, 1, thumb_dir),
            _img_point(2, 2, thumb_dir),
            _img_point(3, 3, thumb_dir),
            _img_point(4, 4, thumb_dir),
        ]))
        sh.optimize()

        # 400 bytes of thumbnails, cap 250 → demote oldest until <= 250: ids 1,2.
        demoted = demote_images_over_thumbnail_cap(
            sh, 250, thumbnail_bytes_of=_bytes_of(thumb_dir), on_demote=_rm(thumb_dir),
        )
        assert demoted == [1, 2]
        # Files for 1,2 removed; 3,4 kept.
        assert not os.path.exists(os.path.join(thumb_dir, "thumb_1.jpg"))
        assert not os.path.exists(os.path.join(thumb_dir, "thumb_2.jpg"))
        assert os.path.exists(os.path.join(thumb_dir, "thumb_3.jpg"))

        recs = {r.id: r.payload for r in sh.retrieve([1, 2, 3], with_payload=True, with_vector=False)}
        assert recs[1][TIER_KEY] == TIER_EMBEDDING_ONLY
        assert recs[1].get("thumbnail_url") in (None, "")  # cleared
        assert recs[3][TIER_KEY] == TIER_FULL
        # The demoted point still has its vector → still searchable.
        res = sh.query(QueryRequest(limit=5, query=Query.Nearest(query=[0.1, 0.2, 0.3], using="text"), with_payload=False))
        assert 1 in [r.id for r in res]
        sh.close()


def test_unsynced_points_are_never_demoted():
    with tempfile.TemporaryDirectory() as tmp:
        thumb_dir = os.path.join(tmp, "thumbs"); os.makedirs(thumb_dir)
        sh = _make_shard(os.path.join(tmp, "shard"))
        # Oldest is unsynced (pending) — must be protected from demotion.
        sh.update(UpdateOperation.upsert_points(points=[
            _img_point(1, 1, thumb_dir, synced=0),
            _img_point(2, 2, thumb_dir, synced=1),
            _img_point(3, 3, thumb_dir, synced=1),
        ]))
        sh.optimize()
        demoted = demote_images_over_thumbnail_cap(
            sh, 150, thumbnail_bytes_of=_bytes_of(thumb_dir), on_demote=_rm(thumb_dir),
        )
        # Only synced points are eligible; the pending id 1 is never demoted.
        assert 1 not in demoted
        assert demoted == [2]
        assert os.path.exists(os.path.join(thumb_dir, "thumb_1.jpg"))
        sh.close()


def test_confirmed_disputed_urgent_are_protected():
    with tempfile.TemporaryDirectory() as tmp:
        thumb_dir = os.path.join(tmp, "thumbs"); os.makedirs(thumb_dir)
        sh = _make_shard(os.path.join(tmp, "shard"))
        sh.update(UpdateOperation.upsert_points(points=[
            _img_point(1, 1, thumb_dir, consensus="CONFIRMED"),
            _img_point(2, 2, thumb_dir, urgent=True),
            _img_point(3, 3, thumb_dir),
            _img_point(4, 4, thumb_dir),
        ]))
        sh.optimize()
        # Cap 150 needs ~250 bytes demoted, but 1 (CONFIRMED) and 2 (URGENT) are
        # protected, so only 3 and 4 can go.
        demoted = demote_images_over_thumbnail_cap(
            sh, 150, thumbnail_bytes_of=_bytes_of(thumb_dir), on_demote=_rm(thumb_dir),
        )
        assert set(demoted) == {3, 4}
        assert os.path.exists(os.path.join(thumb_dir, "thumb_1.jpg"))
        assert os.path.exists(os.path.join(thumb_dir, "thumb_2.jpg"))
        sh.close()


def test_disabled_cap_is_a_noop():
    with tempfile.TemporaryDirectory() as tmp:
        thumb_dir = os.path.join(tmp, "thumbs"); os.makedirs(thumb_dir)
        sh = _make_shard(os.path.join(tmp, "shard"))
        sh.update(UpdateOperation.upsert_points(points=[_img_point(1, 1, thumb_dir)]))
        sh.optimize()
        assert demote_images_over_thumbnail_cap(
            sh, 0, thumbnail_bytes_of=_bytes_of(thumb_dir), on_demote=_rm(thumb_dir)
        ) == []
        sh.close()


def test_orphan_thumbnail_scan_removes_unreferenced_files():
    with tempfile.TemporaryDirectory() as tmp:
        thumb_dir = os.path.join(tmp, "thumbs"); os.makedirs(thumb_dir)
        sh = _make_shard(os.path.join(tmp, "shard"))
        sh.update(UpdateOperation.upsert_points(points=[_img_point(1, 1, thumb_dir)]))
        sh.optimize()
        # An orphan file no point references.
        with open(os.path.join(thumb_dir, "thumb_999.jpg"), "wb") as fh:
            fh.write(b"orphan")
        removed = remove_orphan_thumbnails(sh, thumb_dir)
        assert "thumb_999.jpg" in removed
        assert os.path.exists(os.path.join(thumb_dir, "thumb_1.jpg"))  # referenced, kept
        assert not os.path.exists(os.path.join(thumb_dir, "thumb_999.jpg"))
        sh.close()
