"""Phase 2 / B5 — semantic value clustering + contradiction guard (P1-5).

Paraphrases of the same claim fold together, but a different number or a flipped
negation is a genuinely different claim and must stay in its own cluster — the
embedding alone can't tell them apart, so a hard contradiction guard runs first.
The same guard protects the edge's novelty/REJECT path.
"""

import os
import sys
import tempfile

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from edge_node.value_cluster import contradicts, signature  # noqa: E402
from consensus_fold import OBSERVED, fold_consensus  # noqa: E402

import app as gateway_app  # noqa: E402

THRESHOLD = 0.66


# --- 1. The pure contradiction guard ---------------------------------------

def test_contradiction_guard_separates_numbers_and_negation():
    # Differing numbers contradict.
    assert contradicts("pressure 5 bar", "pressure 50 bar")
    # Flipped negation contradicts.
    assert contradicts("the stairwell is safe", "the stairwell is not safe")
    # Paraphrases with the same numbers and polarity do NOT contradict.
    assert not contradicts("gas leak in zone c", "there is a gas leak at zone c")
    assert not contradicts("5 bar at valve", "valve reads 5 bar")


# --- 2. The fold groups by value_cluster -----------------------------------

def _ev(seq, device, value, cluster):
    return {
        "corroboration_key": "zone_c.gas",
        "device_id": device,
        "value": value,
        "value_cluster": cluster,
        "event_type": OBSERVED,
        "seq": seq,
        "client_timestamp_ns": seq,
        "device_trust_at_report": 0.7,
    }


def test_three_paraphrases_in_one_cluster_confirm():
    events = [
        _ev(1, "dev_a", "gas leak in zone c", "CL1"),
        _ev(2, "dev_b", "there is a gas leak at zone c", "CL1"),
        _ev(3, "dev_c", "zone c has a gas leak", "CL1"),
    ]
    out = fold_consensus(events, threshold=THRESHOLD)
    assert out["status"] == "CONFIRMED"
    # One cluster → one surfaced value (a representative), three backers.
    assert len(out["values"]) == 1
    assert out["values"][0]["devices"] == ["dev_a", "dev_b", "dev_c"]


def test_different_clusters_stay_separate():
    events = [
        _ev(1, "dev_a", "pressure 5 bar", "CL_5"),
        _ev(2, "dev_b", "pressure 50 bar", "CL_50"),
    ]
    out = fold_consensus(events, threshold=THRESHOLD)
    surfaced = {v["value"] for v in out["values"]}
    assert surfaced == {"pressure 5 bar", "pressure 50 bar"}
    assert out["status"] == "DISPUTED"


# --- 3. The edge novelty/REJECT guard --------------------------------------

def test_near_duplicate_with_different_number_is_not_rejected():
    from edge_node.decision_engine import DecisionEngine
    from edge_node.registry import load_adapters
    from qdrant_edge import (
        Bm25, Distance, EdgeConfig, EdgeShard, EdgeSparseVectorParams,
        EdgeVectorParams, Modifier, Point, UpdateOperation,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        # Load the edge policy + a real text adapter.
        import edge_node.main as main
        cp = str(main.DEFAULT_CONFIG_PATH)
        adapters = load_adapters(cp)
        adapter = next(a for a in adapters if a.modality == "text")
        policy = yaml.safe_load(open(cp)).get("policy", {})
        engine = DecisionEngine(policy)

        shard_dir = os.path.join(tmpdir, "shard")
        os.makedirs(shard_dir, exist_ok=True)
        cfg = EdgeConfig(
            vectors={adapter.name: EdgeVectorParams(size=adapter.dim, distance=Distance.Cosine)},
            sparse_vectors={"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
        )
        shard = EdgeShard.create(shard_dir, cfg)
        try:
            vec = adapter.embed("manifold pressure reading is 5 bar")
            shard.update(UpdateOperation.upsert_points(points=[
                Point(id=1, vector={adapter.name: vec}, payload={"value": "manifold pressure reading is 5 bar"})
            ]))
            shard.optimize()

            # Same embedding (cosine ~1.0) but a contradicting NUMBER: must not be
            # dropped as a redundant duplicate.
            verdict, reason = engine.evaluate(
                {"value": "manifold pressure reading is 50 bar"}, vec, shard, adapter
            )
            assert verdict != "REJECT", (verdict, reason)

            # Control: an identical value at the same vector IS redundant.
            v2, r2 = engine.evaluate(
                {"value": "manifold pressure reading is 5 bar"}, vec, shard, adapter
            )
            assert v2 == "REJECT", (v2, r2)
        finally:
            shard.close()


# --- 4. The hub clusters paraphrases at ingest -----------------------------

@pytest.fixture
def gateway_client():
    os.environ["QDRANT_URL"] = ":memory:"
    with TestClient(gateway_app.app) as client:
        yield client


def _ingest(client, device, pid, key, value, vector):
    env = {
        "id": pid,
        "vector": vector,
        "sparse": None,
        "payload": {
            "value": value, "model": "text", "modality": "text",
            "corroboration_key": key, "reporter_device_id": device,
            "client_timestamp_ns": pid,
        },
        "client_sequence": 1,
    }
    assert client.post("/ingest", json={"device_id": device, "envelopes": [env]}).status_code == 200


def test_hub_clusters_paraphrases_and_separates_contradictions(gateway_client):
    key = "zone_c.gas"
    # Three devices, near-identical vectors (paraphrases), no numbers/negation.
    base = [0.9] + [0.01] * 383
    para = [0.89, 0.02] + [0.01] * 382
    para2 = [0.91] + [0.02] + [0.01] * 382
    _ingest(gateway_client, "dev-01", 1, key, "gas leak in zone c", base)
    _ingest(gateway_client, "dev-02", 2, key, "there is a gas leak at zone c", para)
    _ingest(gateway_client, "dev-03", 3, key, "zone c reports a gas leak", para2)

    out = gateway_client.get(f"/consensus/{key}").json()
    assert out["status"] == "CONFIRMED", out
    assert len(out["values"]) == 1  # all three paraphrases folded into one cluster

    # A fourth device reports a contradicting NUMBER with a near-identical vector:
    # the guard keeps it in a separate cluster despite the embedding similarity.
    _ingest(gateway_client, "dev-04", 4, "zone_c.pressure", "pressure 5 bar", base)
    _ingest(gateway_client, "dev-05", 5, "zone_c.pressure", "pressure 50 bar", para)
    pout = gateway_client.get("/consensus/zone_c.pressure").json()
    assert len(pout["values"]) == 2, pout
