"""Phase 2 / B7 — edge conflict detection over both shards + hub consensus_state.

Two halves of P1-7:
  * the edge detects a contradicting fact another device reported (found in the
    immutable shard after a pull) and persists the conflict flag on the point so
    it survives a restart;
  * the hub writes `consensus_state` + `confidence` into the facts payloads, so a
    device answering offline flags [DISPUTED] facts (answer.py already consumes it).
"""

import os
import sys
import tempfile

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
from edge_node.registry import load_adapters  # noqa: E402
from edge_node.decision_engine import DecisionEngine, clear_feed  # noqa: E402
from edge_node.answer import select_context  # noqa: E402
from qdrant_edge import (  # noqa: E402
    EdgeConfig, EdgeVectorParams, Distance, EdgeSparseVectorParams, Modifier,
    Bm25, Point, UpdateOperation,
)

from sync_helpers import StubTransport  # noqa: E402

import app as gateway_app  # noqa: E402


# ---------------------------------------------------------------- edge half ---

def _init_edge():
    config_path = str(main.DEFAULT_CONFIG_PATH)
    main.adapters = load_adapters(config_path)
    vectors = {a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine) for a in main.adapters}
    sparse_vectors = {"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)}
    main.edge_config = EdgeConfig(
        vectors=vectors, sparse_vectors=sparse_vectors,
        max_search_threads=2, search_pool_core=0,
    )
    main.device_shards = {}
    main.event_logs = {}
    with open(config_path) as f:
        full_config = yaml.safe_load(f)
    main.decision_engine = DecisionEngine(full_config.get("policy", {}))
    main.TRUST_DECAY = 0.0
    main.CONFLICT_THRESHOLD = 0.75  # dense cosine (F3): paraphrases ~0.9, different hazards ~0.6
    main.bm25 = Bm25()
    main.sync_transport = StubTransport()
    clear_feed()
    from edge_node.conflicts import clear_conflicts
    clear_conflicts()


def test_contradicting_fleet_fact_is_flagged_and_persists(tmp_path):
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        _init_edge()
        device_id = "dev_b"
        shards = main.get_or_create_shards(device_id)
        text_adapter = next(a for a in main.adapters if a.modality == "text")

        # Simulate a fact PULLED from the fleet: device A said the area is safe.
        pulled_value = "the loading dock area is safe"
        pulled_vec = text_adapter.embed(pulled_value)
        pulled_sparse = main.bm25.embed_document(pulled_value)
        shards["immutable"].update(UpdateOperation.upsert_points(points=[Point(
            id=424242,
            vector={text_adapter.name: pulled_vec, "text_bm25": pulled_sparse},
            payload={"value": pulled_value, "corroboration_key": "dock.status",
                     "zone": "dock", "reporter_device_id": "dev_a"},
        )]))
        shards["immutable"].optimize()

        client = TestClient(main.app)
        # This device captures the opposite claim in the same zone.
        cap = client.post(
            f"/devices/{device_id}/capture",
            json={"device_id": device_id, "corroboration_key": "dock.status",
                  "value": "the loading dock area is not safe", "zone": "dock"},
        )
        assert cap.status_code == 200
        pid = int(cap.json()["id"])
        assert cap.json()["conflicts"], "the contradicting fleet fact was not flagged"

        # The conflict flag is persisted on the point payload (survives restart).
        rec = shards["mutable"].retrieve([pid], with_payload=True, with_vector=False)
        assert rec and rec[0].payload.get("has_conflict") == 1
    finally:
        os.chdir(old)


def test_f3_conflict_by_dense_cosine_flags_paraphrase_not_unrelated(tmp_path):
    """F3 (P1-7 residual): candidate search is dense COSINE, thresholded on a real
    cosine. A paraphrase of the same hazard (cosine ~0.9) is flagged; a different
    hazard in the same zone (cosine ~0.6, below the 0.75 threshold) is not."""
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        _init_edge()
        device_id = "dev_cos"
        client = TestClient(main.app)

        # Seed report under key A.
        r1 = client.post(f"/devices/{device_id}/capture", json={
            "device_id": device_id, "corroboration_key": "reportA", "zone": "C",
            "value": "strong gas leak reported in the east stairwell"})
        assert r1.status_code == 200
        assert r1.json()["conflicts"] == []

        # Same zone, DIFFERENT key, UNRELATED hazard: dense cosine ~0.63 < 0.75,
        # so despite the different key it must NOT be flagged.
        r2 = client.post(f"/devices/{device_id}/capture", json={
            "device_id": device_id, "corroboration_key": "reportB", "zone": "C",
            "value": "water flooding reported in the west basement garage"})
        assert r2.status_code == 200
        assert r2.json()["conflicts"] == [], "dissimilar hazard flagged by cosine threshold"

        # Same zone, DIFFERENT key, a PARAPHRASE of reportA: dense cosine ~0.9 >= 0.75
        # -> flagged as a possible same-event conflict the exact-key scheme missed.
        r3 = client.post(f"/devices/{device_id}/capture", json={
            "device_id": device_id, "corroboration_key": "reportD", "zone": "C",
            "value": "gas smell and possible leak near the east stairs"})
        assert r3.status_code == 200
        conflicts = r3.json()["conflicts"]
        assert any(c["existing_key"] == "reportA" for c in conflicts), \
            "paraphrase of reportA not flagged by cosine"
    finally:
        os.chdir(old)


# ----------------------------------------------------------------- hub half ---

@pytest.fixture
def gateway_client():
    os.environ["QDRANT_URL"] = ":memory:"
    with TestClient(gateway_app.app) as client:
        yield client


def _ingest(client, device, pid, key, value, vector):
    env = {
        "id": pid, "vector": vector, "sparse": None,
        "payload": {"value": value, "model": "text", "modality": "text",
                    "corroboration_key": key, "reporter_device_id": device,
                    "client_timestamp_ns": pid},
        "client_sequence": 1,
    }
    assert client.post("/ingest", json={"device_id": device, "envelopes": [env]}).status_code == 200


def test_hub_writes_consensus_state_into_facts(gateway_client):
    key = "zone_d.status"
    base = [0.9] + [0.01] * 383
    other = [0.1, 0.9] + [0.0] * 382
    _ingest(gateway_client, "dev-01", 701, key, "the area is safe", base)
    _ingest(gateway_client, "dev-02", 702, key, "the area is not safe", other)

    # The facts now carry the fold's verdict so answers can flag it offline.
    flt = gateway_app.models.Filter(
        must=[gateway_app.models.FieldCondition(
            key="corroboration_key", match=gateway_app.models.MatchValue(value=key))]
    )
    pts, _ = gateway_app.client.scroll(
        gateway_app.FACTS_COLLECTION, scroll_filter=flt, limit=10,
        with_payload=True, with_vectors=False,
    )
    assert pts, "no facts stored"
    for p in pts:
        assert p.payload.get("consensus_state") == "DISPUTED", p.payload
        assert "confidence" in p.payload

    # And answer.py flags such a fact as [DISPUTED] in the prompt context.
    class _Hit:
        def __init__(self, payload):
            self.id = 1
            self.score = 0.9
            self.payload = payload
    ctx = select_context([_Hit({"value": "the area is safe", "consensus_state": "DISPUTED"})], top_k=1)
    assert ctx[0]["context_line"].startswith("[DISPUTED] ")
