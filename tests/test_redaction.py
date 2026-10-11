"""D5 (T7) — PII redaction before push.

Three layers:
  * the pure `redaction.redact` (right-to-left replacement, accurate spans);
  * the edge push path, which rebuilds the outbound envelope from redacted text
    (value + sparse recomputed + dense re-embedded) so NO PII reaches the hub,
    while the raw copy stays in the local shard;
  * the hub `GET /fact/{point_id}`, which returns the (redacted) copy the hub
    holds.
"""

import json
import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))

from edge_node.redaction import redact, REDACTION_PLACEHOLDER  # noqa: E402

SSN = r"\b\d{3}-\d{2}-\d{4}\b"
EMAIL = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"


# --- pure redaction -----------------------------------------------------------

def test_redact_removes_every_pattern_and_reports_spans():
    text = "call 555-12-3456 or email jane.doe@example.com now"
    out, spans = redact(text, [SSN, EMAIL])
    assert "555-12-3456" not in out
    assert "jane.doe@example.com" not in out
    assert out.count(REDACTION_PLACEHOLDER) == 2
    # Spans are in original coordinates and carry the removed originals (local).
    originals = {s["original"] for s in spans}
    assert originals == {"555-12-3456", "jane.doe@example.com"}


def test_redact_right_to_left_keeps_offsets_valid():
    # Two matches of DIFFERENT placeholder-vs-match length; a left-to-right pass
    # would corrupt the second span. Right-to-left must place both correctly.
    text = "a@b.com and 111-22-3333 end"
    out, spans = redact(text, [SSN, EMAIL])
    assert out == f"{REDACTION_PLACEHOLDER} and {REDACTION_PLACEHOLDER} end"
    assert [s["start"] for s in spans] == [0, 12]


def test_redact_no_patterns_or_no_match_is_identity():
    assert redact("nothing here", [SSN, EMAIL]) == ("nothing here", [])
    assert redact("abc", []) == ("abc", [])


# --- edge push path -----------------------------------------------------------

from edge_node.registry import load_adapters  # noqa: E402
from qdrant_edge import (  # noqa: E402
    Bm25, Distance, EdgeConfig, EdgeSparseVectorParams, EdgeVectorParams, Modifier,
)
from fastapi.testclient import TestClient  # noqa: E402
from edge_node.decision_engine import DecisionEngine, clear_feed  # noqa: E402
import edge_node.main as main  # noqa: E402
from sync_helpers import StubTransport  # noqa: E402


def _init(stub):
    config_path = str(main.DEFAULT_CONFIG_PATH)
    main.adapters = load_adapters(config_path)
    vectors = {a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine) for a in main.adapters}
    main.edge_config = EdgeConfig(
        vectors=vectors,
        sparse_vectors={"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
        max_search_threads=2, search_pool_core=0,
    )
    main.device_shards = {}
    main.event_logs = {}
    with open(config_path) as f:
        full_config = yaml.safe_load(f)
    policy_config = full_config.get("policy", {})
    main.decision_engine = DecisionEngine(policy_config)
    main.TRUST_DECAY = float(policy_config.get("trust_decay", 0.0))
    main.CONFLICT_THRESHOLD = float(full_config.get("conflict", {}).get("similarity_threshold", 0.5))
    main.bm25 = Bm25()
    main.sync_transport = stub
    clear_feed()
    from edge_node.conflicts import clear_conflicts
    clear_conflicts()


def test_push_strips_pii_from_every_envelope_field_and_keeps_raw_local():
    """D5 done-when: a captured string matching each pattern appears in NO
    envelope field; sparse indices == redacted text's; dense == embedding of the
    redacted text; the raw copy stays on the device."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = StubTransport()
            _init(stub)
            client = TestClient(main.app)
            dev = "dev_pii"

            ssn, email = "123-45-6789", "victim@example.com"
            raw = f"gas leak reported; contact {email} ssn {ssn} urgent"
            cap = client.post(
                f"/devices/{dev}/capture",
                json={"device_id": dev, "corroboration_key": "pii1", "value": raw},
            )
            assert cap.status_code == 200, cap.text
            assert cap.json()["verdict"] == "REDACT_AND_QUEUE"
            pid = int(cap.json()["id"])

            pushed = client.post(f"/devices/{dev}/push").json()
            assert pushed["pushed_count"] == 1, pushed

            env = stub.store[dev][pid]
            blob = json.dumps(env, default=str)
            # No PII anywhere in the envelope.
            assert ssn not in blob
            assert email not in blob
            assert REDACTION_PLACEHOLDER in env["payload"]["value"]
            assert env["payload"].get("redacted") == 1

            redacted_value = env["payload"]["value"]
            # Sparse recomputed from the redacted text.
            expected_sparse = main.bm25.embed_document(redacted_value)
            assert env["sparse"]["indices"] == list(expected_sparse.indices)
            # Dense re-embedded from the redacted text.
            expected_dense = main.adapters[0].embed(redacted_value)
            assert env["vector"] == pytest.approx(list(expected_dense), abs=1e-6)

            # The RAW copy stays on the device (local shard), with a redaction record.
            shard = main.device_shards[dev]["mutable"]
            rec = shard.retrieve([pid], with_payload=True, with_vector=False)[0]
            assert ssn in rec.payload["value"]
            assert email in rec.payload["value"]
            assert rec.payload.get("_redaction", {}).get("spans")
        finally:
            os.chdir(old)


# --- hub /fact ----------------------------------------------------------------

def test_hub_fact_endpoint_returns_the_redacted_copy():
    """D5: GET /fact/{id} returns the (redacted) fact the hub holds; 404 for an
    unknown id."""
    os.environ["QDRANT_URL"] = ":memory:"
    import app as gateway_app
    with TestClient(gateway_app.app) as gw:
        env = {
            "id": 950001,
            "vector": [0.2] * 384,
            "sparse": None,
            "payload": {
                "value": "gas leak reported; contact [REDACTED] ssn [REDACTED]",
                "redacted": 1,
                "model": "text",
                "modality": "text",
                "corroboration_key": "pii_hub",
                "reporter_device_id": "dev_pii",
                "client_timestamp_ns": 1,
            },
            "client_sequence": 1,
        }
        assert gw.post("/ingest", json={"device_id": "dev_pii", "envelopes": [env]}).status_code == 200
        r = gw.get("/fact/950001")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["redacted"] == 1
        assert "[REDACTED]" in body["value"]
        assert gw.get("/fact/404404").status_code == 404
