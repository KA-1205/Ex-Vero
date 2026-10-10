"""Phase 2 / A6d — ack handshake by acked-id set, defaulting to nothing acked.

The push drain used to (a) default the ack to "everything we sent" when the
transport returned no `acked_ids`, and (b) pre-filter the outbox by the hub's
max `client_sequence`. Together those fail open: a hub that advances its
high-water sequence while NOT storing a particular lower-sequence point would
cause that point to be skipped on every subsequent push — stranded forever,
silent data loss (breaks invariant 1).

The fix marks synced strictly by the set of acknowledged ids and never skips a
pending point by a sequence watermark. These tests pin both halves.
"""

import os
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
from edge_node.registry import load_adapters  # noqa: E402
from edge_node.outbox import get_outbox  # noqa: E402
from qdrant_edge import (  # noqa: E402
    EdgeConfig,
    EdgeVectorParams,
    Distance,
    EdgeSparseVectorParams,
    Modifier,
    Bm25,
)
from fastapi.testclient import TestClient  # noqa: E402
from edge_node.decision_engine import DecisionEngine, clear_feed  # noqa: E402

from sync_helpers import StubTransport, TransportFailure  # noqa: E402


def _init(stub):
    config_path = str(main.DEFAULT_CONFIG_PATH)
    main.adapters = load_adapters(config_path)
    vectors = {a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine) for a in main.adapters}
    sparse_vectors = {"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)}
    main.edge_config = EdgeConfig(
        vectors=vectors,
        sparse_vectors=sparse_vectors,
        max_search_threads=2,
        search_pool_core=0,
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


class PartialAckStub(StubTransport):
    """A hub that advances its high-water client_sequence for every event it sees
    but does not store (nor ack) the facts whose id is in ``drop_ids``."""

    def __init__(self, drop_ids=()):
        super().__init__()
        self.drop_ids = set(drop_ids)

    def push(self, device_id, envelopes):
        if self.fail or self.fail_next:
            self.fail_next = False
            raise TransportFailure("stub transport forced failure")
        dev = self.store.setdefault(device_id, {})
        acked = []
        for env in envelopes:
            # The watermark advances even for a dropped (unstored) point — this is
            # exactly the condition that stranded lower-sequence points.
            self.max_seq[device_id] = max(
                self.max_seq.get(device_id, -1), env["client_sequence"]
            )
            if env["id"] in self.drop_ids:
                continue  # not stored, not acked
            dev[env["id"]] = env
            self.events.append({
                "device_id": device_id,
                "point_id": env["id"],
                "event_type": "OBSERVED",
                "client_sequence": env["client_sequence"],
            })
            acked.append(env["id"])
        return {"acked_ids": acked, "count": len(acked)}


def _outbox_ids(device_id):
    shard = main.device_shards[device_id]["mutable"]
    return [pid for pid, _ in get_outbox(shard, limit=100)]


def _capture(client, device_id, key, value):
    r = client.post(
        f"/devices/{device_id}/capture",
        json={"device_id": device_id, "corroboration_key": key, "value": value},
    )
    assert r.status_code == 200
    return int(r.json()["id"])


def test_partial_ack_does_not_strand_a_lower_sequence_point():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = PartialAckStub()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_partial"

            # Three URGENT facts -> client_sequence 1, 2, 3.
            p1 = _capture(client, device_id, "k1", "structural collapse at site one")
            p2 = _capture(client, device_id, "k2", "gas leak at site two, evacuate")
            p3 = _capture(client, device_id, "k3", "fire spreading at site three")

            # The hub drops the MIDDLE (lower-sequence) point but still advances its
            # watermark to 3 from the others.
            stub.drop_ids = {p2}
            resp = client.post(f"/devices/{device_id}/push").json()
            assert resp["pushed_count"] == 2  # p1, p3 acked; p2 dropped

            # p1/p3 landed and left the outbox; p2 did NOT land and is still pending.
            assert stub.server_has(device_id, p1)
            assert stub.server_has(device_id, p3)
            assert not stub.server_has(device_id, p2)
            assert _outbox_ids(device_id) == [p2]
            # The watermark really did advance past p2's sequence.
            assert stub.max_seq[device_id] == 3

            # Retry with the hub healthy: p2 must NOT be skipped by the watermark.
            stub.drop_ids = set()
            resp2 = client.post(f"/devices/{device_id}/push").json()
            assert resp2["pushed_count"] == 1
            assert stub.server_has(device_id, p2), (
                "the lower-sequence point was stranded by the max-sequence filter"
            )
            assert _outbox_ids(device_id) == []
        finally:
            os.chdir(old)


class NoAckIdsStub(StubTransport):
    """A hub that stores facts but returns a bare count with no `acked_ids`."""

    def push(self, device_id, envelopes):
        resp = super().push(device_id, envelopes)
        return {"count": resp["count"]}  # drop acked_ids


def test_missing_acked_ids_marks_nothing_synced():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            stub = NoAckIdsStub()
            _init(stub)
            client = TestClient(main.app)
            device_id = "dev_noack"

            pid = _capture(client, device_id, "k", "structural collapse reported")
            client.post(f"/devices/{device_id}/push")

            # Fail closed: with no explicit acked_ids we mark NOTHING synced, so the
            # point stays pending and will be retried — never falsely marked done.
            assert _outbox_ids(device_id) == [pid]
        finally:
            os.chdir(old)
