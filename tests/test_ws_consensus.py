"""Phase 2 / A6c — the consensus WebSocket is a real set, and broadcasts land.

`_ws_consensus_events` was initialised to a dict literal (`{}`) while the connect
handler calls `.add(websocket)`. `dict` has no `.add`, so every attempt to open
`/consensus/events` raised `AttributeError` the moment a client connected — the
Conflict Theater live feed could never receive a single event.

This test opens the socket (which must not raise) and proves a consensus event
broadcast by the app actually arrives over the wire.
"""

import os
import tempfile

import yaml
from fastapi.testclient import TestClient

import edge_node.main as main
from edge_node.registry import load_adapters
from edge_node.decision_engine import DecisionEngine, clear_feed
from qdrant_edge import (
    EdgeConfig,
    EdgeVectorParams,
    Distance,
    EdgeSparseVectorParams,
    Modifier,
    Bm25,
)


def _init():
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
    # No transport: the demo inject path forwards to the hub only when wired, and
    # this test only cares about the local broadcast fan-out.
    main.sync_transport = None
    clear_feed()


def test_consensus_events_socket_connects_and_receives_a_broadcast():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init()
            # The underlying registry must be a set so `.add` works on connect.
            assert isinstance(main._ws_consensus_events, set)

            client = TestClient(main.app)
            with client.websocket_connect("/consensus/events") as ws:
                # Connecting registered this socket (pre-fix this raised before we
                # got here because `.add` was called on a dict).
                assert len(main._ws_consensus_events) == 1

                # Trigger a real consensus broadcast through the app.
                resp = client.post(
                    "/demo/inject-conflict",
                    json={
                        "corroboration_key": "sector-4-gas",
                        "assignments": {"dev-a": "safe", "dev-b": "not safe"},
                    },
                )
                assert resp.status_code == 200

                event = ws.receive_json()
                assert event["corroboration_key"] == "sector-4-gas"
                assert event["state"] == "DISPUTED"
        finally:
            os.chdir(old)
