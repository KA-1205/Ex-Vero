"""Phase 7 — Answer layer (on-device RAG) acceptance tests.

Acceptance criterion (docs/30-phases.md, Phase 7):
  With a stub/extractive generator (no network in the test), the same question
  returns a GROUNDED answer citing REAL retrieved ids and a correct
  `answer_path`; a live Ollama test is marked integration and skipped if the
  daemon is absent.

Build steps proved here (docs/30-phases.md Phase 7 + backend.md §8):
  1. Retrieve = the hybrid query over both shards (the real `/query` path).
  2. Augment = a prompt built from the top-k retrieved facts as the ONLY allowed
     context; CONFIRMED facts preferred, DISPUTED flagged.
  3. Generate = a `Generator`-conforming backend; `ExtractiveGenerator` is the
     always-available fallback, `OllamaGenerator` the offline model.
  4. Response reports `answer`, `answer_path`, `model`, `latency_ms`, `sources`.

Invariant touched (AGENTS.md §5 invariant 8, backend.md §2.5):
  The answer path never imports the transport or the network simulator, and it
  answers under `offline` without being gated by the wire. Proven by a clean
  subprocess import-graph check plus a functional answer under `offline`.

Guardrails proven: the generator only ever sees the retrieved context (no free
generation), and the generator is config-selected.
"""

import os
import subprocess
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import edge_node.main as main  # noqa: E402
import edge_node.network as network  # noqa: E402
from edge_node.registry import load_adapters, build_generators  # noqa: E402
from edge_node.answer import answer_question  # noqa: E402
from edge_node.adapter import ExtractiveGenerator  # noqa: E402
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

from sync_helpers import StubTransport  # noqa: E402


# --------------------------------------------------------------------------- #
# Test doubles — Generators that need no network. They conform to the Generator
# protocol (a `name` + `generate(question, context)`) and carry an `answer_path`.
# --------------------------------------------------------------------------- #
class RecordingGenerator:
    """Captures exactly the context it was handed, so a test can prove the
    generator only ever sees the retrieved facts (grounding guard)."""

    name = "recording"
    answer_path = "offline"

    def __init__(self):
        self.last_question = None
        self.last_context = None

    def generate(self, question, context):
        self.last_question = question
        self.last_context = list(context)
        return "STUB ANSWER: " + " || ".join(context)


class FailingGenerator:
    """Imitates an unreachable model daemon: raises, so the orchestrator must
    fall back to the next generator in the chain."""

    name = "unreachable-model"
    answer_path = "online"

    def generate(self, question, context):
        raise RuntimeError("model daemon unreachable")


class _Hit:
    """Duck-typed retrieval hit (id/score/payload) for the augment unit tests."""

    def __init__(self, id, score, payload):
        self.id = id
        self.score = score
        self.payload = payload


def _init(generators):
    """Fresh app state (no lifespan) with an explicit generator chain."""
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
    # Query/answer never touch the transport, but capture bookkeeping expects one.
    main.sync_transport = StubTransport()
    main.generators = generators
    network.reset()
    clear_feed()
    from edge_node.conflicts import clear_conflicts

    clear_conflicts()


def _teardown():
    """Undo the module-global generator chain so it can't leak into other test
    files (test_answer runs first alphabetically)."""
    main.generators = None
    network.reset()


def _capture(client, device_id, key, value, zone=None):
    body = {"device_id": device_id, "corroboration_key": key, "value": value}
    if zone is not None:
        body["zone"] = zone
    r = client.post(f"/devices/{device_id}/capture", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# 1. Isolation (invariant 8): the answer module imports neither the network
#    simulator nor the sync transport. Checked in a clean subprocess so nothing
#    else in this session's sys.modules can mask a real import.
# --------------------------------------------------------------------------- #
def test_answer_module_does_not_import_network_or_transport():
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    probe = (
        "import sys; import edge_node.answer;"
        "assert 'edge_node.network' not in sys.modules, 'answer imports the network simulator';"
        "assert 'edge_node.sync_transport' not in sys.modules, 'answer imports the sync transport';"
        "print('ISOLATED')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env
    )
    assert out.returncode == 0, f"isolation broken:\n{out.stdout}\n{out.stderr}"
    assert "ISOLATED" in out.stdout


# --------------------------------------------------------------------------- #
# 2. THE acceptance test: a grounded answer citing real retrieved ids, correct
#    answer_path, produced offline with an extractive generator (no network).
# --------------------------------------------------------------------------- #
def test_query_answer_is_grounded_cites_real_ids_offline():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init([ExtractiveGenerator()])
            client = TestClient(main.app)
            device_id = "dev_rag"

            # Multiple distinct facts (not a single-doc setup): retrieval must
            # actually rank among them.
            ids = {}
            ids["gas"] = _capture(
                client, device_id, "zone_c.hazard",
                "Gas leak reported at the Zone C east stairwell", zone="C",
            )["id"]
            ids["collapse"] = _capture(
                client, device_id, "zone_d.hazard",
                "Structural collapse blocking the Zone D exit ramp", zone="D",
            )["id"]
            ids["water"] = _capture(
                client, device_id, "zone_a.status",
                "Water levels rising near the Zone A footbridge", zone="A",
            )["id"]
            captured_ids = set(ids.values())

            # Go fully offline with a large injected wire latency: if the answer
            # path were gated by the sim it would inherit that latency.
            client.post("/network/mode", json={"mode": "degraded"})
            network.configure(latency_min_ms=800, latency_max_ms=800, failure_rate=0.0)
            client.post("/network/mode", json={"mode": "offline"})

            resp = client.post(
                f"/devices/{device_id}/query",
                json={"text": "which zone has a gas hazard?", "answer": True},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()

            # Path + model are reported and correct for the extractive fallback.
            assert data["answer_path"] == "extractive"
            assert data["model"] == "extractive"

            # Sources cite REAL retrieved point ids, never fabricated ones.
            assert data["sources"], "an answer must cite its sources"
            for s in data["sources"]:
                assert s["id"] in captured_ids, f"cited id {s['id']} was never captured"
            # The gas fact is the most relevant and must be among the sources.
            assert ids["gas"] in {s["id"] for s in data["sources"]}

            # Grounding: the generated answer is built from the retrieved context —
            # the top source's value appears verbatim in the answer.
            top_value = data["sources"][0]["value"]
            assert top_value and top_value in data["answer"]

            # Answered offline, and the 800 ms wire did NOT bleed into the path.
            assert data["latency_ms"] < 400, "answer latency inflated by the network sim"
        finally:
            _teardown()
            os.chdir(old)


# --------------------------------------------------------------------------- #
# 3. Grounding contract: the generator is only ever handed the retrieved facts
#    (the "top-k retrieved facts as the ONLY allowed context" rule). Driven by
#    the REAL hybrid retrieval through the endpoint.
# --------------------------------------------------------------------------- #
def test_generator_receives_only_retrieved_context():
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            recorder = RecordingGenerator()
            _init([recorder, ExtractiveGenerator()])
            client = TestClient(main.app)
            device_id = "dev_ctx"

            _capture(client, device_id, "k1", "Gas leak at the Zone C east stairwell", zone="C")
            _capture(client, device_id, "k2", "Fire spreading on the Zone B rooftop", zone="B")

            resp = client.post(
                f"/devices/{device_id}/query",
                json={"text": "gas hazard zone", "answer": True},
            ).json()

            # The recording generator served the answer (chain head).
            assert resp["answer_path"] == "offline"
            assert resp["model"] == "recording"

            # Every string the generator saw is the `value` of a retrieved fact —
            # no outside context was injected — and it matches the top-k retrieval
            # order exactly.
            retrieved_values = [r["payload"]["value"] for r in resp["results"]]
            assert recorder.last_context, "generator got no context"
            assert set(recorder.last_context).issubset(set(retrieved_values))
            k = len(recorder.last_context)
            assert recorder.last_context == retrieved_values[:k]
        finally:
            _teardown()
            os.chdir(old)


# --------------------------------------------------------------------------- #
# 4. Augment: prefer CONFIRMED, flag DISPUTED (backend.md §8). Unit-level over
#    answer_question with hits carrying a consensus_state.
# --------------------------------------------------------------------------- #
def test_augment_prefers_confirmed_and_flags_disputed():
    recorder = RecordingGenerator()
    # Retrieval order (by score) is unknown → disputed → confirmed. The augment
    # must PROMOTE the CONFIRMED fact to the front and FLAG the disputed one.
    hits = [
        _Hit(1, 0.9, {"value": "unverified rumor about zone e", "consensus_state": None}),
        _Hit(2, 0.8, {"value": "zone e is on fire", "consensus_state": "DISPUTED"}),
        _Hit(3, 0.7, {"value": "zone c gas leak confirmed by three crews",
                      "consensus_state": "CONFIRMED"}),
    ]

    result = answer_question("what is happening?", hits, [recorder], top_k=5)

    # CONFIRMED promoted ahead of the higher-scored uncorroborated hits.
    assert recorder.last_context[0] == "zone c gas leak confirmed by three crews"
    # DISPUTED is still surfaced but explicitly flagged, never silently dropped.
    assert any(line.startswith("[DISPUTED]") and "zone e is on fire" in line
               for line in recorder.last_context)
    assert "zone e is on fire" in {  # flag is presentation-only; source value is clean
        s["value"] for s in result.sources
    }
    disputed_source = next(s for s in result.sources if s["id"] == 2)
    assert disputed_source["consensus_state"] == "DISPUTED"
    confirmed_source = next(s for s in result.sources if s["id"] == 3)
    assert confirmed_source["consensus_state"] == "CONFIRMED"


# --------------------------------------------------------------------------- #
# 5. answer_path reports which generator actually served, and the chain falls
#    back past an unreachable model to the extractive generator.
# --------------------------------------------------------------------------- #
def test_answer_path_reports_serving_generator_and_falls_back():
    hits = [_Hit(10, 0.5, {"value": "flooding on 4th street", "consensus_state": None})]

    # Primary is reachable → it serves and its path is reported.
    ok = RecordingGenerator()
    served = answer_question("q", hits, [ok, ExtractiveGenerator()], top_k=5)
    assert served.answer_path == "offline"
    assert served.model == "recording"

    # Primary raises (unreachable) → the chain falls back to extractive, and the
    # answer is still grounded in the retrieved context.
    fell_back = answer_question("q", hits, [FailingGenerator(), ExtractiveGenerator()], top_k=5)
    assert fell_back.answer_path == "extractive"
    assert fell_back.model == "extractive"
    assert "flooding on 4th street" in fell_back.answer


# --------------------------------------------------------------------------- #
# 6. The generator chain is config-selected (build_generators reads YAML) and
#    always terminates in the always-available extractive fallback.
# --------------------------------------------------------------------------- #
def test_generator_chain_is_config_selected():
    cfg = {
        "models": {
            "generator": {
                "prefer": "offline",
                "offline": {
                    "provider": "ollama",
                    "model": "qwen2.5:1.5b",
                    "endpoint": "http://localhost:11434",
                },
            }
        }
    }
    chain = build_generators(cfg)
    assert [g.name for g in chain] == ["qwen2.5:1.5b", "extractive"]
    assert chain[0].answer_path == "offline"
    assert chain[-1].answer_path == "extractive"

    # No generator block at all → still a usable chain (extractive only).
    assert [g.name for g in build_generators({})] == ["extractive"]


# --------------------------------------------------------------------------- #
# 7. Live Ollama — integration only; skipped unless the daemon is present AND
#    the model is actually serving (a warm-up generate). This keeps the default
#    suite deterministic: a missing/cold model skips rather than flakes.
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_answer_with_live_ollama():
    import httpx

    from edge_node.adapter import OllamaGenerator

    endpoint = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

    # Daemon present?
    try:
        tags = httpx.get(f"{endpoint}/api/tags", timeout=2.0)
        tags.raise_for_status()
        available = {m.get("name") for m in tags.json().get("models", [])}
    except Exception:
        pytest.skip(f"Ollama not reachable at {endpoint}")
    if model not in available:
        pytest.skip(f"Ollama model {model!r} not pulled ({sorted(available)})")

    # Model actually serving right now? A cold/loaded box may need a moment; if
    # this warm-up can't complete we skip rather than fail the suite.
    gen = OllamaGenerator(model=model, endpoint=endpoint, timeout=120.0)
    try:
        warm = gen.generate("ping", ["the sky is blue"])
        assert isinstance(warm, str)
    except Exception as e:
        pytest.skip(f"Ollama present but not serving {model!r} right now: {e}")

    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.getcwd()
        os.chdir(tmpdir)
        try:
            _init([gen, ExtractiveGenerator()])
            client = TestClient(main.app)
            device_id = "dev_ollama"
            cap_id = _capture(client, device_id, "zone_c.hazard",
                              "Gas leak reported at the Zone C east stairwell", zone="C")["id"]
            resp = client.post(
                f"/devices/{device_id}/query",
                json={"text": "where is the gas hazard?", "answer": True},
            ).json()
            # The offline model served it (not the extractive fallback), grounded
            # in the retrieved fact.
            assert resp["answer_path"] == "offline"
            assert resp["model"] == model
            assert resp["answer"].strip()
            assert cap_id in {s["id"] for s in resp["sources"]}
        finally:
            _teardown()
            os.chdir(old)
