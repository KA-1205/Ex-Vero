"""D9 (T8) — geo-aware recall.

Spike confirmed: `qdrant-edge-py==0.8.0` supports a geo payload field + a
`GeoRadius` filter in `shard.query` (see the task report). Team decision: a
capture's location comes from an explicit lat/lon (source (c)) or the vertical's
zone -> coords map (source (b)); the feature is per-vertical.

These tests drive the real edge app: an optional `near(lat, lon, radius_m)` on
`/query` restricts hits to within the radius (haversine distances annotated),
while leaving corroboration keys and the consensus fold untouched.
"""

import os
import sys
import tempfile

import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
from edge_node.registry import load_adapters  # noqa: E402
from qdrant_edge import (  # noqa: E402
    Bm25, Distance, EdgeConfig, EdgeSparseVectorParams, EdgeVectorParams, Modifier,
)
from edge_node.decision_engine import DecisionEngine, clear_feed  # noqa: E402
from sync_helpers import StubTransport  # noqa: E402


def _init():
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
    main.sync_transport = StubTransport()
    main.GEO_ZONE_COORDS = {}
    clear_feed()
    from edge_node.conflicts import clear_conflicts
    clear_conflicts()


def _capture(client, dev, key, value, **extra):
    body = {"device_id": dev, "corroboration_key": key, "value": value}
    body.update(extra)
    r = client.post(f"/devices/{dev}/capture", json=body)
    assert r.status_code == 200, r.text
    return int(r.json()["id"])


def test_query_near_returns_only_in_radius_points():
    """D9 done-when: a near(lat,lon,radius) query returns only in-radius points;
    corroboration keys are unchanged (consensus untouched)."""
    with tempfile.TemporaryDirectory() as tmp:
        old = os.getcwd(); os.chdir(tmp)
        try:
            _init()
            client = TestClient(main.app)
            dev = "dev_geo"
            # Two near (40.0,-74.0), one ~140km away.
            a = _capture(client, dev, "siteA", "gas leak reported at the north yard", lat=40.0, lon=-74.0)
            b = _capture(client, dev, "siteB", "flood rising near the north yard", lat=40.001, lon=-74.0)
            c = _capture(client, dev, "siteC", "gas leak reported far to the west", lat=41.0, lon=-75.0)

            # Without geo: all three are retrievable.
            allr = client.post(f"/devices/{dev}/query",
                               json={"device_id": dev, "text": "incident", "answer": False, "limit": 10}).json()
            all_ids = {int(x["id"]) for x in allr["results"]}
            assert {a, b, c} <= all_ids

            # With near(40,-74, 5km): only the two nearby points come back.
            near = client.post(f"/devices/{dev}/query", json={
                "device_id": dev, "text": "incident", "answer": False, "limit": 10,
                "lat": 40.0, "lon": -74.0, "radius_m": 5000,
            }).json()
            near_ids = {int(x["id"]) for x in near["results"]}
            assert a in near_ids and b in near_ids
            assert c not in near_ids
            # Haversine distance annotated; corroboration keys unchanged.
            by_id = {int(x["id"]): x for x in near["results"]}
            assert by_id[a]["payload"]["distance_m"] == 0.0
            assert by_id[a]["payload"]["corroboration_key"] == "siteA"
            assert 0 < by_id[b]["payload"]["distance_m"] < 5000
        finally:
            os.chdir(old)


def test_zone_coords_map_supplies_location_when_no_latlon():
    """D9: with no explicit lat/lon, a capture's zone resolves to coords via the
    vertical's zone_coords map (source (b))."""
    with tempfile.TemporaryDirectory() as tmp:
        old = os.getcwd(); os.chdir(tmp)
        try:
            _init()
            main.GEO_ZONE_COORDS = {"north_yard": {"lat": 40.0, "lon": -74.0}}
            client = TestClient(main.app)
            dev = "dev_geo_zone"
            pid = _capture(client, dev, "zonesiteA", "gas leak in the north yard",
                           zone="north_yard")
            shard = main.device_shards[dev]["mutable"]
            loc = shard.retrieve([pid], with_payload=True, with_vector=False)[0].payload.get("location")
            assert loc == {"lat": 40.0, "lon": -74.0}

            near = client.post(f"/devices/{dev}/query", json={
                "device_id": dev, "text": "incident", "answer": False, "limit": 10,
                "lat": 40.0, "lon": -74.0, "radius_m": 1000,
            }).json()
            assert pid in {int(x["id"]) for x in near["results"]}
        finally:
            os.chdir(old)
