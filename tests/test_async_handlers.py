"""Phase 2 / A6a — handlers must not block the event loop.

The sync drain (`push`/`pull`) and the Ollama answer step do real blocking work:
the degraded link sleeps, the token bucket waits, the gateway HTTP round-trips.
If those run on the event loop, a single slow push freezes every concurrent
request — including local search, which AGENTS.md requires to stay responsive
regardless of connectivity.

This test drives the real ASGI app with an async client and runs a slow degraded
push concurrently with a query. On the blocking (pre-fix) handlers the query is
stalled behind the push's ~1.5 s sleep; once the drain is offloaded with
`asyncio.to_thread`, the query returns promptly while the push is still in flight.
"""

import asyncio
import os
import tempfile
import time

import httpx
import yaml

import edge_node.main as main
import edge_node.network as network
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

from sync_helpers import StubTransport


def _init(inner_transport):
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
    main.sync_transport = network.NetworkTransport(inner_transport)
    network.reset()
    clear_feed()
    from edge_node.conflicts import clear_conflicts

    clear_conflicts()


def test_search_latency_stays_low_while_a_degraded_push_runs():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init(StubTransport())
            device_id = "dev_async"

            async def scenario():
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as ac:
                    # Capture a fact and warm the embedder with a first query so the
                    # measured query below reflects search, not model load.
                    await ac.post(
                        f"/devices/{device_id}/capture",
                        json={
                            "device_id": device_id,
                            "corroboration_key": "collapse",
                            "value": "structural collapse on 5th street",
                        },
                    )
                    warm = await ac.post(
                        f"/devices/{device_id}/query",
                        json={"device_id": device_id, "text": "structural collapse"},
                    )
                    assert warm.status_code == 200

                    # Arm a slow degraded link: ~1.5 s of real latency per request,
                    # no byte cap, no random failures.
                    await ac.post("/network/mode", json={"mode": "degraded"})
                    network.configure(
                        latency_min_ms=1500,
                        latency_max_ms=1500,
                        failure_rate=0.0,
                        byte_rate_bytes_per_sec=0,
                    )

                    # Kick off the slow push and, concurrently, time a query. If the
                    # drain runs on the event loop, the push's ~1.5 s wire sleep blocks
                    # the loop and the query cannot be served until the push finishes.
                    t0 = time.perf_counter()
                    push_task = asyncio.create_task(ac.post(f"/devices/{device_id}/push"))
                    await asyncio.sleep(0)  # let the push task start before we query

                    q = await ac.post(
                        f"/devices/{device_id}/query",
                        json={"device_id": device_id, "text": "structural collapse"},
                    )
                    q_elapsed = time.perf_counter() - t0

                    push_resp = await push_task
                    push_elapsed = time.perf_counter() - t0
                    return q_elapsed, push_elapsed, q, push_resp

            q_elapsed, push_elapsed, q, push_resp = asyncio.run(scenario())

            assert q.status_code == 200
            # The push really is slow (it slept ~1.5 s on the degraded wire)...
            assert push_resp.json()["pushed_count"] == 1
            assert push_elapsed >= 1.5, (
                f"the degraded push did not actually wait on the wire "
                f"(push_elapsed={push_elapsed:.3f}s)"
            )
            # ...but the concurrent query was NOT stuck behind it. If the drain ran
            # on the event loop the query would inherit the full ~1.5 s wire sleep.
            assert q_elapsed < 0.6, (
                f"query stalled behind the degraded push (elapsed={q_elapsed:.3f}s) "
                "— the sync drain is blocking the event loop"
            )
        finally:
            os.chdir(old)
