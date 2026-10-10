"""Acceptance tests for Phase 0: Cloud Gateway and Cross-Device Consensus.

Criterion:
  3 simulated devices push conflicting values for the same corroboration_key;
  /cloud/state returns one reconciled fact with a confidence score, not three raw rows.
"""

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

# Ensure cloud-gateway is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
# ...and src, so the edge Edge engine + shared snapshot builder are importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app as gateway_app


@pytest.fixture
def gateway_client():
    os.environ["QDRANT_URL"] = ":memory:"
    with TestClient(gateway_app.app) as client:
        yield client


def test_cross_device_consensus_reconciles_conflicts(gateway_client):
    """Phase 0 acceptance test: 3 devices report on the same corroboration_key.

    dev-01 -> "structural collapse near exit B"
    dev-02 -> "structural collapse near exit B"
    dev-03 -> "all clear near exit B" (contradictory)

    /cloud/state must return 1 consolidated fact with CONFIRMED state and
    confidence reflecting supermajority, not 3 raw rows.
    """
    key = "zone_b.structural_status"

    # Device 1 reports collapse
    p1 = {
        "id": 101,
        "vector": [0.1] * 384,
        "payload": {
            "value": "structural collapse near exit B",
            "corroboration_key": key,
            "reporter_device_id": "dev-01",
            "client_timestamp_ns": 1000,
        },
        "client_sequence": 1,
    }
    r1 = gateway_client.post("/ingest", json={"device_id": "dev-01", "envelopes": [p1]})
    assert r1.status_code == 200

    # Device 2 corroborates collapse
    p2 = {
        "id": 102,
        "vector": [0.1] * 384,
        "payload": {
            "value": "structural collapse near exit B",
            "corroboration_key": key,
            "reporter_device_id": "dev-02",
            "client_timestamp_ns": 2000,
        },
        "client_sequence": 1,
    }
    r2 = gateway_client.post("/ingest", json={"device_id": "dev-02", "envelopes": [p2]})
    assert r2.status_code == 200

    # Device 3 reports conflicting "all clear"
    p3 = {
        "id": 103,
        "vector": [0.1] * 384,
        "payload": {
            "value": "all clear near exit B",
            "corroboration_key": key,
            "reporter_device_id": "dev-03",
            "client_timestamp_ns": 3000,
        },
        "client_sequence": 1,
    }
    r3 = gateway_client.post("/ingest", json={"device_id": "dev-03", "envelopes": [p3]})
    assert r3.status_code == 200

    # Query /cloud/state
    resp = gateway_client.get("/cloud/state")
    assert resp.status_code == 200
    data = resp.json()

    assert "facts" in data
    assert "device_trust" in data

    # Must contain exactly 1 reconciled fact for this corroboration key, NOT 3 raw rows
    facts_for_key = [f for f in data["facts"] if f["corroboration_key"] == key]
    assert len(facts_for_key) == 1, f"Expected 1 reconciled fact, got {len(facts_for_key)}"

    fact = facts_for_key[0]
    assert fact["value"] == "structural collapse near exit B"
    assert fact["state"] in ("CONFIRMED", "DISPUTED")
    assert fact["confidence"] > 0.5
    assert set(fact["corroborating_devices"]) == {"dev-01", "dev-02"}

    # Device trust: dev-01 and dev-02 agreed with the winner, dev-03 disagreed
    trust = data["device_trust"]
    assert "dev-01" in trust
    assert "dev-02" in trust
    assert "dev-03" in trust
    assert trust["dev-01"] > trust["dev-03"]
    assert trust["dev-02"] > trust["dev-03"]


def test_conflict_injection_and_retraction(gateway_client):
    """Tests scripted conflict injection and retractions via gateway endpoints."""
    key = "zone_d.gas_leak"
    inj_resp = gateway_client.post(
        "/demo/inject-conflict",
        json={
            "corroboration_key": key,
            "assignments": {
                "dev-01": "methane 120ppm",
                "dev-02": "methane 120ppm",
                "dev-03": "methane 120ppm",
                "dev-04": "normal 5ppm",
            },
        },
    )
    assert inj_resp.status_code == 200
    assert inj_resp.json()["injected"] is True

    state_resp = gateway_client.get("/cloud/state")
    assert state_resp.status_code == 200
    facts = state_resp.json()["facts"]
    d_facts = [f for f in facts if f["corroboration_key"] == key]
    assert len(d_facts) == 1
    assert d_facts[0]["value"] == "methane 120ppm"
    assert d_facts[0]["state"] == "CONFIRMED"
    assert d_facts[0]["confidence"] >= 0.66


def test_snapshot_roundtrips_mixed_text_and_image_points(gateway_client):
    """A1 (P0-1): a text fact and an image fact pushed through the REAL `/ingest`
    survive a `/snapshot` round-trip into a genuine Edge shard, and dense text,
    BM25, and image search each find their point.

    This fails on the pre-fix gateway: `/ingest` slotted the dense vector into a
    `text`/`image` slot but `/facts`+`_all_fact_envelopes` read it back from a
    hardcoded `dense` slot (so the vector came back `None`), and the snapshot
    builder did `len(sample["vector"])` on that `None` (TypeError) and packed a
    single-slot shard the device's two-slot immutable shard rejects.
    """
    from qdrant_edge import (
        Bm25,
        Distance,
        EdgeConfig,
        EdgeShard,
        EdgeSparseVectorParams,
        EdgeVectorParams,
        Modifier,
        Prefetch,
        Query,
        QueryRequest,
    )

    device = "dev-mix"
    bm25 = Bm25()
    text_value = "structural collapse near exit B blocking the stairwell"
    text_sparse = bm25.embed_document(text_value)
    text_id, image_id = 7001, 7002
    text_vec = [0.5] * 384
    image_vec = [0.3] * 512

    text_env = {
        "id": text_id,
        "vector": text_vec,
        "sparse": {"indices": list(text_sparse.indices), "values": list(text_sparse.values)},
        "payload": {
            "value": text_value,
            "model": "text",
            "modality": "text",
            "corroboration_key": "zone_b.structural_status",
            "reporter_device_id": device,
            "client_timestamp_ns": 1000,
        },
        "client_sequence": 1,
    }
    image_env = {
        "id": image_id,
        "vector": image_vec,
        "sparse": None,
        "payload": {
            "value": "[photo] smoke in corridor",
            "model": "image",
            "modality": "vision",
            "corroboration_key": "zone_b.visual",
            "reporter_device_id": device,
            "client_timestamp_ns": 2000,
        },
        "client_sequence": 2,
    }

    r = gateway_client.post(
        "/ingest", json={"device_id": device, "envelopes": [text_env, image_env]}
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["acked_ids"]) == {text_id, image_id}

    # /facts must return every named vector per point (no None dense vector).
    facts_resp = gateway_client.get(f"/facts/{device}")
    assert facts_resp.status_code == 200
    returned = {e["id"]: e for e in facts_resp.json()["envelopes"]}
    assert set(returned) == {text_id, image_id}
    assert returned[text_id]["vectors"].get("text") is not None
    assert returned[image_id]["vectors"].get("image") is not None
    assert returned[text_id]["sparse"] is not None

    # /snapshot must materialize a real Edge tar (no TypeError on a None vector).
    snap = gateway_client.post(f"/snapshot/{device}", json={"manifest": {}})
    assert snap.status_code == 200, snap.text
    assert snap.content, "snapshot body was empty"

    with tempfile.TemporaryDirectory() as tmp:
        tar_path = os.path.join(tmp, "snapshot.tar")
        with open(tar_path, "wb") as fh:
            fh.write(snap.content)

        # The device's immutable shard carries BOTH named dense slots + BM25.
        shard_dir = os.path.join(tmp, "edge_shard")
        os.makedirs(shard_dir, exist_ok=True)
        config = EdgeConfig(
            vectors={
                "text": EdgeVectorParams(size=384, distance=Distance.Cosine),
                "image": EdgeVectorParams(size=512, distance=Distance.Cosine),
            },
            sparse_vectors={"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
        )
        shard = EdgeShard.create(shard_dir, config)
        try:
            shard.update_from_snapshot(tar_path)
            shard.optimize()

            def _ids(using, query):
                req = QueryRequest(
                    limit=10,
                    query=Query.Nearest(query=query, using=using),
                    with_payload=False,
                    with_vector=False,
                )
                return [p.id for p in shard.query(req)]

            # Dense text search finds the text point.
            assert text_id in _ids("text", text_vec)
            # Image search finds the image point.
            assert image_id in _ids("image", image_vec)
            # BM25 search finds the text point from the restored sparse leg.
            bm25_ids = _ids("text_bm25", bm25.embed_query("collapse stairwell"))
            assert text_id in bm25_ids
        finally:
            shard.close()


def _events_for_point(point_id):
    """Scroll the gateway's fact_events for one point's events."""
    flt = gateway_app.models.Filter(
        must=[gateway_app.models.FieldCondition(
            key="point_id", match=gateway_app.models.MatchValue(value=point_id))]
    )
    pts, _ = gateway_app.client.scroll(
        gateway_app.EVENTS_COLLECTION, scroll_filter=flt, limit=100,
        with_payload=True, with_vectors=False,
    )
    return pts


def test_ingest_is_insert_if_absent_a_retry_keeps_the_event_seq(gateway_client):
    """A3 (P0-3): re-pushing the same envelope (a crash-retry) must NOT change the
    event's hub `seq`. Pre-fix, /ingest called _next_seq() unconditionally and
    overwrote the event with a higher seq; a reordered delta could then flip a
    verdict. Insert-if-absent keeps the original seq.
    """
    device = "dev-retry"
    pid = 90210
    env = {
        "id": pid,
        "vector": [0.2] * 384,
        "sparse": None,
        "payload": {
            "value": "gas leak in zone k",
            "model": "text",
            "modality": "text",
            "corroboration_key": "zone_k.gas",
            "reporter_device_id": device,
            "client_timestamp_ns": 1000,
        },
        "client_sequence": 1,
    }

    r1 = gateway_client.post("/ingest", json={"device_id": device, "envelopes": [env]})
    assert r1.status_code == 200, r1.text
    evs1 = _events_for_point(pid)
    assert len(evs1) == 1
    seq1 = evs1[0].payload["seq"]

    # Retry the identical push.
    r2 = gateway_client.post("/ingest", json={"device_id": device, "envelopes": [env]})
    assert r2.status_code == 200, r2.text
    evs2 = _events_for_point(pid)
    assert len(evs2) == 1, "retry duplicated the event"
    assert evs2[0].payload["seq"] == seq1, "retry changed the event's hub seq"


def _ingest_n(gateway_client, device, n, key="zone_n.count"):
    for i in range(1, n + 1):
        env = {
            "id": 500000 + i,
            "vector": [0.01 * i] * 384,
            "sparse": None,
            "payload": {
                "value": f"observation {i}",
                "model": "text",
                "modality": "text",
                "corroboration_key": key,
                "reporter_device_id": device,
                "client_timestamp_ns": i,
            },
            "client_sequence": i,
        }
        r = gateway_client.post("/ingest", json={"device_id": device, "envelopes": [env]})
        assert r.status_code == 200, r.text


def test_delta_returns_max_without_scanning_all_events(gateway_client):
    """A4 (P0-4): /delta returns the device's max client_sequence via a single
    order_by(DESC) limit-1 query — it must not scroll the whole event log.

    We spy on client.scroll during the /delta call and assert it never pulls back
    more than one event row, even though the device has many.
    """
    device = "dev-delta"
    n = 6
    _ingest_n(gateway_client, device, n)

    real_scroll = gateway_app.client.scroll
    max_rows = {"n": 0}

    def spy_scroll(collection_name, *a, **kw):
        res = real_scroll(collection_name, *a, **kw)
        if collection_name == gateway_app.EVENTS_COLLECTION:
            pts = res[0] if isinstance(res, tuple) else res
            max_rows["n"] = max(max_rows["n"], len(pts))
        return res

    gateway_app.client.scroll = spy_scroll
    try:
        resp = gateway_client.get(f"/delta/{device}")
    finally:
        gateway_app.client.scroll = real_scroll

    assert resp.status_code == 200
    assert resp.json()["max_client_sequence"] == n
    assert max_rows["n"] <= 1, f"/delta scanned {max_rows['n']} event rows (expected <= 1)"


def test_restart_resumes_seq_from_meta_point_without_reissuing(gateway_client):
    """A4 (P0-4): the sequencer resumes from the meta point on restart (one point
    read, no log scan) and never re-issues an old seq."""
    device = "dev-restart"
    _ingest_n(gateway_client, device, 3, key="zone_r.count")
    high_before = gateway_app._seq
    assert high_before >= 3

    # Simulate a restart: drop the in-memory counter and re-init from storage.
    gateway_app._seq = 0
    gateway_app._init_sequencer()
    assert gateway_app._seq == high_before, "restart did not resume the high-water seq"

    # A fresh ingest must get a seq strictly greater than any already issued.
    env = {
        "id": 777001,
        "vector": [0.3] * 384,
        "sparse": None,
        "payload": {
            "value": "post restart",
            "model": "text",
            "modality": "text",
            "corroboration_key": "zone_r.count",
            "reporter_device_id": device,
            "client_timestamp_ns": 99,
        },
        "client_sequence": 99,
    }
    assert gateway_client.post("/ingest", json={"device_id": device, "envelopes": [env]}).status_code == 200
    new_seq = _events_for_point(777001)[0].payload["seq"]
    assert new_seq > high_before, "restart re-issued an old seq"



