"""D2 (T5) — embedding-model stamp & guard (same work as Brief G2).

Two embedders of EQUAL dimension but DIFFERENT weights live in incompatible
vector spaces; a dimension check cannot tell them apart, but a cosine search
across them returns silent garbage. These tests prove the three enforcement
points the task requires with exactly that adversarial pair:

  * startup refuses to load a shard stamped by a different model;
  * the hub returns 409 when an ingest's model stamp conflicts with the one it
    registered;
  * a pull refuses to restore a snapshot whose stamp is incompatible.

The pair is `FakeEmbedder('text', 384, 'text')` vs the same name/dim with a
different `weights_id`, so dim is identical and only the weights differ.
"""

import json
import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from edge_node import model_stamp
from edge_node.adapter import FakeEmbedder
from edge_node.snapshot import build_snapshot_tar, read_snapshot_stamp

SLOT = "text"
DIM = 384


def _adapter(weights_id=""):
    return FakeEmbedder(SLOT, DIM, "text", weights_id=weights_id)


# --- the stamp core -------------------------------------------------------
def test_equal_dim_different_weights_are_incompatible():
    a = model_stamp.compute_stamp(_adapter(""))
    b = model_stamp.compute_stamp(_adapter("other-weights"))
    assert a["dim"] == b["dim"] == DIM
    assert a["space_id"] != b["space_id"]
    assert not model_stamp.stamps_compatible(a, b)
    # Same model is compatible with itself.
    assert model_stamp.stamps_compatible(a, model_stamp.compute_stamp(_adapter("")))


def test_space_id_is_stable_across_runs():
    assert (
        model_stamp.compute_stamp(_adapter(""))["space_id"]
        == model_stamp.compute_stamp(_adapter(""))["space_id"]
    )


# --- (a) startup refuses --------------------------------------------------
def test_startup_refuses_a_shard_stamped_by_a_different_model():
    import edge_node.main as main

    saved_adapters = main.adapters
    saved_cache = main._fleet_stamp_cache
    try:
        shard_dir = tempfile.mkdtemp(prefix="stamp_shard_")
        # Shard was created under model A.
        model_stamp.write_stamp(shard_dir, model_stamp.fleet_stamp([_adapter("")]))

        # Now the node comes up with model B (equal dim, different weights).
        main.adapters = [_adapter("different-weights")]
        main._fleet_stamp_cache = None

        with pytest.raises(RuntimeError) as exc:
            main._verify_shard_stamp(shard_dir)
        assert "stamp mismatch" in str(exc.value).lower()

        # --rebuild / EDGE_REBUILD re-stamps instead of refusing.
        os.environ["EDGE_REBUILD"] = "1"
        try:
            main._verify_shard_stamp(shard_dir)  # must not raise
        finally:
            os.environ.pop("EDGE_REBUILD", None)
        restamped = model_stamp.read_stamp(shard_dir)
        assert restamped[SLOT]["space_id"] == main._current_fleet_stamp()[SLOT]["space_id"]
    finally:
        main.adapters = saved_adapters
        main._fleet_stamp_cache = saved_cache


def test_startup_accepts_a_matching_stamp_and_stamps_new_shards():
    import edge_node.main as main

    saved_adapters = main.adapters
    saved_cache = main._fleet_stamp_cache
    try:
        main.adapters = [_adapter("")]
        main._fleet_stamp_cache = None
        # A brand-new (unstamped) shard dir is stamped, not refused.
        shard_dir = tempfile.mkdtemp(prefix="stamp_new_")
        main._verify_shard_stamp(shard_dir)
        assert model_stamp.read_stamp(shard_dir) is not None
        # Loading it again under the same model is accepted.
        main._verify_shard_stamp(shard_dir)
    finally:
        main.adapters = saved_adapters
        main._fleet_stamp_cache = saved_cache


# --- (b) ingest returns 409 ----------------------------------------------
def _envelope(pid, value):
    return {
        "id": pid,
        "vector": [0.1] * DIM,
        "sparse": None,
        "payload": {
            "value": value,
            "model": SLOT,
            "modality": "text",
            "corroboration_key": "zone_b.status",
            "reporter_device_id": "dev-01",
            "client_timestamp_ns": 1000,
        },
        "client_sequence": 1,
    }


@pytest.fixture
def client():
    os.environ["QDRANT_URL"] = ":memory:"
    import app as gateway_app

    with TestClient(gateway_app.app) as c:
        yield c


def test_ingest_registers_then_409s_on_an_incompatible_model(client):
    stamp_a = model_stamp.fleet_stamp([_adapter("")])
    stamp_b = model_stamp.fleet_stamp([_adapter("different-weights")])

    # First ingest registers the fleet's model space.
    r1 = client.post(
        "/ingest",
        json={"device_id": "dev-01", "envelopes": [_envelope(101, "all clear")], "space_stamps": stamp_a},
    )
    assert r1.status_code == 200

    # A second device on a different model (equal dim, different weights) → 409.
    r2 = client.post(
        "/ingest",
        json={"device_id": "dev-02", "envelopes": [_envelope(102, "collapse")], "space_stamps": stamp_b},
    )
    assert r2.status_code == 409
    assert "stamp mismatch" in r2.json()["detail"].lower()

    # The same model re-ingesting is still fine.
    r3 = client.post(
        "/ingest",
        json={"device_id": "dev-03", "envelopes": [_envelope(103, "all clear")], "space_stamps": stamp_a},
    )
    assert r3.status_code == 200


def test_ingest_without_stamps_still_works(client):
    # Backward compatibility: a pre-D2 client that sends no stamp is accepted.
    r = client.post(
        "/ingest",
        json={"device_id": "dev-01", "envelopes": [_envelope(201, "all clear")]},
    )
    assert r.status_code == 200


# --- (c) pull refuses -----------------------------------------------------
def test_snapshot_carries_a_stamp_and_a_mismatch_is_detected():
    stamp_a = model_stamp.fleet_stamp([_adapter("")])
    stamp_b = model_stamp.fleet_stamp([_adapter("different-weights")])

    work = tempfile.mkdtemp(prefix="stamp_snap_")
    env = {"id": 5, "vector": [0.1] * 4, "payload": {"value": "hi"}}
    tar = build_snapshot_tar([env], dense_name="text", dim=4, work_dir=work,
                             extra_vectors={}, space_stamps=stamp_a)
    assert tar is not None

    carried = read_snapshot_stamp(tar)
    assert carried is not None
    assert carried[SLOT]["space_id"] == stamp_a[SLOT]["space_id"]

    # A device on model B would refuse this snapshot (what _do_pull checks).
    assert model_stamp.diff_fleet(stamp_b, carried, min_cosine=model_stamp.DEFAULT_STAMP_COSINE)
    # A device on model A accepts it.
    assert not model_stamp.diff_fleet(stamp_a, carried, min_cosine=model_stamp.DEFAULT_STAMP_COSINE)


def test_snapshot_without_a_stamp_reads_as_none():
    work = tempfile.mkdtemp(prefix="stamp_snap_none_")
    env = {"id": 6, "vector": [0.1] * 4, "payload": {"value": "hi"}}
    tar = build_snapshot_tar([env], dense_name="text", dim=4, work_dir=work, extra_vectors={})
    assert read_snapshot_stamp(tar) is None
