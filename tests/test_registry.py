"""Phase 2 acceptance tests — Model Adapter Registry.

Proves the registry resolves config-declared models into adapter objects that
all satisfy the same structural `Embedder` contract, that a model is swapped by
editing YAML (no code change), that `dim` is read from the adapter, and that
the conformance suite in backend.md §3.3 holds for the fake, the real dense
text, and the CLIP cross-modal adapters.

Tests use `FakeEmbedder` by default (no downloads). Real-model tests are guarded
and skip when the checkpoint can't be materialised (offline CI).
"""
import io
import math
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from edge_node.adapter import (
    Embedder,
    Generator,
    FakeEmbedder,
    TextDenseAdapter,
    ClipTextAdapter,
    ClipVisionAdapter,
    as_unit_vector,
)
from edge_node import registry


# ---------------------------------------------------------------------------
# The conformance suite (backend.md §3.3) — one function, run over every adapter.
# ---------------------------------------------------------------------------
def assert_conforms(embedder, ok_payloads, wrong_modality_payload):
    """Assert an embedder satisfies every clause of the §3.3 contract.

    `ok_payloads` are >=3 distinct, valid payloads for this embedder's
    modality; `wrong_modality_payload` is a payload of the *other* modality.
    """
    # It must structurally be an Embedder (runtime-checkable Protocol).
    assert isinstance(embedder, Embedder), "adapter does not satisfy the Embedder protocol"

    # (9) non-empty version — a checkpoint id is stamped on every point.
    assert isinstance(embedder.version, str) and embedder.version, "version must be a non-empty string"

    vecs = embedder.embed_batch(ok_payloads)

    # (1) output length == input length, order preserved.
    assert len(vecs) == len(ok_payloads), "output length must equal input length"
    # Order preservation: embedding each payload alone matches its batch slot.
    for i, p in enumerate(ok_payloads):
        solo = embedder.embed_batch([p])[0]
        assert solo == pytest.approx(vecs[i], abs=1e-6), "batch order not preserved"

    for v in vecs:
        # (2) len(vec) == dim.
        assert len(v) == embedder.dim, f"vector length {len(v)} != dim {embedder.dim}"
        # (6) no NaN / Inf.
        assert all(math.isfinite(x) for x in v), "vector contains NaN/Inf"
        # (3) unit norm.
        norm = math.sqrt(sum(x * x for x in v))
        assert norm == pytest.approx(1.0, abs=1e-5), f"vector not unit-norm (||v||={norm})"

    # (4) determinism — same input, same vector.
    again = embedder.embed_batch(ok_payloads)
    for a, b in zip(vecs, again):
        assert a == pytest.approx(b, abs=1e-6), "embedder is not deterministic"

    # (5) distinct inputs -> distinct vectors.
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            assert vecs[i] != pytest.approx(vecs[j], abs=1e-9), "distinct inputs collapsed to same vector"

    # (7) zero vector raises (guard lives in as_unit_vector).
    with pytest.raises(ValueError):
        as_unit_vector([0.0] * embedder.dim)

    # (8) wrong-modality payload raises.
    with pytest.raises((TypeError, ValueError)):
        embedder.embed_batch([wrong_modality_payload])


# ---------------------------------------------------------------------------
# as_unit_vector guards (NaN/Inf/zero) — the normalization is the single guard.
# ---------------------------------------------------------------------------
def test_as_unit_vector_normalizes():
    v = as_unit_vector([3.0, 4.0])
    assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0, abs=1e-9)
    assert v == pytest.approx([0.6, 0.8], abs=1e-9)


def test_as_unit_vector_rejects_zero():
    with pytest.raises(ValueError):
        as_unit_vector([0.0, 0.0, 0.0])


def test_as_unit_vector_rejects_nan_and_inf():
    with pytest.raises(ValueError):
        as_unit_vector([float("nan"), 1.0])
    with pytest.raises(ValueError):
        as_unit_vector([float("inf"), 1.0])


# ---------------------------------------------------------------------------
# FakeEmbedder — always runs, no downloads. Covers text and vision modalities.
# ---------------------------------------------------------------------------
def test_fake_embedder_text_conforms():
    emb = FakeEmbedder(name="text_dense", dim=64, modality="text", version="fake-text-v1")
    assert emb.dim == 64
    ok = ["structural collapse on main street", "flooding near the river", "power line down"]
    wrong = b"\x89PNG not a string"  # bytes -> vision payload, wrong for a text embedder
    assert_conforms(emb, ok, wrong)


def test_fake_embedder_vision_conforms():
    emb = FakeEmbedder(name="image", dim=64, modality="vision", version="fake-vision-v1")
    ok = [b"image-bytes-A", b"image-bytes-B", b"image-bytes-C"]
    wrong = "a plain text string"  # str -> text payload, wrong for a vision embedder
    assert_conforms(emb, ok, wrong)


# ---------------------------------------------------------------------------
# Config swap: editing YAML alone changes the resolved adapter, no code change.
# ---------------------------------------------------------------------------
def _write_cfg(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


def test_config_swap_changes_resolved_adapter():
    cfg_a = _write_cfg(
        """
models:
  embedders:
    - name: text_dense
      modality: text
      provider: fake
      version: fake-a
      dim: 8
"""
    )
    cfg_b = _write_cfg(
        """
models:
  embedders:
    - name: text_dense
      modality: text
      provider: fake
      version: fake-b
      dim: 32
"""
    )
    try:
        a = registry.load_adapters(cfg_a)
        b = registry.load_adapters(cfg_b)
        assert a[0].dim == 8 and b[0].dim == 32, "config dim not honoured by resolver"
        assert a[0].version == "fake-a" and b[0].version == "fake-b"
        # Same code path, different resolved object driven purely by YAML.
        assert a[0].name == b[0].name == "text_dense"
    finally:
        os.unlink(cfg_a)
        os.unlink(cfg_b)


def test_provider_swap_changes_adapter_class():
    """Swapping provider fake->fastembed resolves a different concrete class."""
    cfg_fake = _write_cfg(
        """
models:
  embedders:
    - name: text_dense
      modality: text
      provider: fake
      version: fake-x
      dim: 384
"""
    )
    cfg_real = _write_cfg(
        """
models:
  embedders:
    - name: text_dense
      modality: text
      provider: fastembed
      model: BAAI/bge-small-en-v1.5
      version: bge-small-en-v1.5
"""
    )
    try:
        fake = registry.load_adapters(cfg_fake)[0]
        assert isinstance(fake, FakeEmbedder)
        try:
            real = registry.load_adapters(cfg_real)[0]
        except Exception as e:  # noqa: BLE001 - offline: checkpoint unavailable
            pytest.skip(f"real dense model unavailable offline: {e}")
        assert isinstance(real, TextDenseAdapter)
        # dim comes from the adapter (the real model), never the (absent) config value.
        assert real.dim == 384
    finally:
        os.unlink(cfg_fake)
        os.unlink(cfg_real)


def test_wrong_modality_rejected_by_registry_config():
    """An unknown modality in config is rejected, not silently ignored."""
    cfg = _write_cfg(
        """
models:
  embedders:
    - name: mystery
      modality: audio
      provider: fake
      version: v
      dim: 8
"""
    )
    try:
        with pytest.raises((ValueError, KeyError)):
            registry.load_adapters(cfg)
    finally:
        os.unlink(cfg)


# ---------------------------------------------------------------------------
# Generator Protocol exists (implementation is Phase 7 — only the contract here).
# ---------------------------------------------------------------------------
def test_generator_protocol_is_runtime_checkable():
    class _StubGen:
        name = "stub"

        def generate(self, question, context):
            return context[0] if context else ""

    assert isinstance(_StubGen(), Generator)

    class _NotAGen:
        name = "no"

    assert not isinstance(_NotAGen(), Generator)


# ---------------------------------------------------------------------------
# Guarded real-model tests — skip if the checkpoint can't be materialised.
# ---------------------------------------------------------------------------
def test_real_dense_text_adapter_conforms():
    try:
        emb = TextDenseAdapter(name="text_dense", model="BAAI/bge-small-en-v1.5", version="bge-small-en-v1.5")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"dense text model unavailable: {e}")
    assert emb.dim == 384  # read from the model, asserted against the known checkpoint size
    ok = ["a burning building", "a flooded road", "an injured survivor"]
    wrong = b"\x00\x01\x02 image bytes"
    assert_conforms(emb, ok, wrong)


def _tiny_png(color):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def test_clip_cross_modal_adapters_conform():
    """One CLIP family, two named vectors, one shared 512-dim space."""
    try:
        text = ClipTextAdapter(name="text", model="Qdrant/clip-ViT-B-32", version="clip-ViT-B-32")
        vision = ClipVisionAdapter(name="image", model="Qdrant/clip-ViT-B-32", version="clip-ViT-B-32")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"CLIP model unavailable: {e}")

    # Shared embedding space: both legs must be the same dimensionality.
    assert text.dim == vision.dim == 512

    assert_conforms(
        text,
        ["a photo of a cat", "a red fire truck", "a flooded street"],
        _tiny_png((255, 0, 0)),  # bytes -> vision payload, wrong for the text leg
    )
    assert_conforms(
        vision,
        [_tiny_png((255, 0, 0)), _tiny_png((0, 255, 0)), _tiny_png((0, 0, 255))],
        "a plain text string",  # str -> text payload, wrong for the vision leg
    )


# ---------------------------------------------------------------------------
# Invariant 9 (AGENTS.md §5): every captured point records its embedding model
# and the pretrained checkpoint version — the resolved adapter's version, not
# some library version — so an incompatible checkpoint change is detectable.
# ---------------------------------------------------------------------------
def test_captured_point_records_model_and_checkpoint_version():
    import yaml
    from fastapi.testclient import TestClient
    from qdrant_edge import (
        EdgeConfig,
        EdgeVectorParams,
        Distance,
        EdgeSparseVectorParams,
        Modifier,
        Bm25,
    )
    import edge_node.main as main
    from edge_node.decision_engine import DecisionEngine, get_feed, clear_feed

    with tempfile.TemporaryDirectory() as tmpdir:
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            config_path = str(main.DEFAULT_CONFIG_PATH)
            main.adapters = registry.load_adapters(config_path)
            adapter = main.adapters[0]
            vectors = {a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine) for a in main.adapters}
            main.edge_config = EdgeConfig(
                vectors=vectors,
                sparse_vectors={"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
                max_search_threads=2,
                search_pool_core=0,
            )
            main.device_shards = {}
            with open(config_path) as f:
                policy = (yaml.safe_load(f) or {}).get("policy", {})
            main.decision_engine = DecisionEngine(policy)
            main.bm25 = Bm25()
            clear_feed()

            client = TestClient(main.app)
            resp = client.post(
                "/devices/dev1/capture",
                json={"device_id": "dev1", "corroboration_key": "k", "value": "a bridge has collapsed"},
            )
            assert resp.status_code == 200

            payload = get_feed("dev1")[0]["payload"]
            # Model name recorded and matches the resolved adapter.
            assert payload["model"] == adapter.name
            # Checkpoint version recorded, non-empty, and is the adapter's
            # version (e.g. "bge-small-en-v1.5"), not a library version string.
            assert payload["model_version"] == adapter.version
            assert payload["model_version"] and not payload["model_version"][0].isdigit()
        finally:
            os.chdir(old_cwd)
