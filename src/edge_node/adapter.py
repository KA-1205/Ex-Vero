"""Model Adapter Registry — the config-driven model table (backend.md §3).

This module is the ONLY place that names concrete embedding model classes.
Everything else in the kernel talks to the structural `Embedder` / `Generator`
Protocols, so swapping a model is a one-line YAML edit — no kernel code change.

Payload contract (so wrong-modality inputs fail loudly instead of silently
producing garbage vectors):
  - text   modality: payload is a `str`.
  - vision modality: payload is raw image `bytes` or a `PIL.Image.Image`.
A payload of the wrong type for an adapter's modality raises.
"""
from __future__ import annotations

import hashlib
import io
import math
from typing import List, Literal, Protocol, runtime_checkable

Modality = Literal["text", "vision"]


# ---------------------------------------------------------------------------
# Structural interfaces (backend.md §3.1). Concrete adapters need not subclass
# these — they conform by shape, so a team model drops in unmodified.
# ---------------------------------------------------------------------------
@runtime_checkable
class Embedder(Protocol):
    name: str        # named vector field in the shard, e.g. "text_dense"
    dim: int         # output dimensionality — derived from the model, never hardcoded
    modality: Modality
    version: str     # pretrained checkpoint id; stamped on every point

    def embed_batch(self, payloads: list) -> List[List[float]]:  # unit-norm vectors
        ...


@runtime_checkable
class Generator(Protocol):
    name: str

    def generate(self, question: str, context: list) -> str:  # answer only from context
        ...


# Backward-compatible alias: earlier phases refer to `Adapter`.
Adapter = Embedder


# ---------------------------------------------------------------------------
# Normalization guard — the single choke point that rejects the vectors cosine
# distance can't handle (zero, NaN, Inf). Every adapter routes through this.
# ---------------------------------------------------------------------------
def as_unit_vector(vec: List[float]) -> List[float]:
    """Return `vec` scaled to unit L2 norm.

    Raises ValueError on a NaN/Inf component or a zero vector — cosine
    similarity on any of those returns arbitrary rows, which is a silent
    correctness bug, so we fail loudly instead.
    """
    if any(not math.isfinite(x) for x in vec):
        raise ValueError("vector contains NaN or Inf")
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        raise ValueError("cannot normalize a zero vector")
    return [x / norm for x in vec]


def _reject_wrong_modality(payload, modality: Modality) -> None:
    """Raise if `payload` is not the type this modality embeds."""
    if modality == "text":
        if not isinstance(payload, str):
            raise TypeError(f"text embedder expects str, got {type(payload).__name__}")
    elif modality == "vision":
        # Accept raw bytes or a PIL image; a str is a text payload (wrong here).
        is_bytes = isinstance(payload, (bytes, bytearray))
        is_pil = payload.__class__.__module__.startswith("PIL.")
        if not (is_bytes or is_pil):
            raise TypeError(f"vision embedder expects image bytes/PIL, got {type(payload).__name__}")
    else:
        raise ValueError(f"unknown modality: {modality}")


# ---------------------------------------------------------------------------
# FakeEmbedder — deterministic vectors hashed from the payload. No downloads,
# so the whole system is buildable and testable offline. Distinct inputs
# diverge because the full payload feeds the hash.
# ---------------------------------------------------------------------------
class FakeEmbedder:
    def __init__(self, name: str, dim: int, modality: Modality, version: str = "fake-v1"):
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.name = name
        self.dim = dim
        self.modality: Modality = modality
        self.version = version

    def _payload_bytes(self, payload) -> bytes:
        if isinstance(payload, str):
            return payload.encode("utf-8")
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        # PIL image (vision): hash its raw pixels.
        if payload.__class__.__module__.startswith("PIL."):
            return payload.tobytes()
        raise TypeError(f"unhashable payload type {type(payload).__name__}")

    def _embed_one(self, payload) -> List[float]:
        _reject_wrong_modality(payload, self.modality)
        raw = self._payload_bytes(payload)
        # Stretch a SHA-256 stream to `dim` floats in [-0.5, 0.5). Deterministic
        # per input; different inputs almost surely diverge.
        out: List[float] = []
        counter = 0
        while len(out) < self.dim:
            block = hashlib.sha256(raw + counter.to_bytes(4, "big")).digest()
            for b in block:
                out.append((b / 255.0) - 0.5)
                if len(out) == self.dim:
                    break
            counter += 1
        return as_unit_vector(out)

    def embed_batch(self, payloads: list) -> List[List[float]]:
        return [self._embed_one(p) for p in payloads]

    # Single-payload convenience used by the capture/query runtime path.
    def embed(self, payload) -> List[float]:
        return self._embed_one(payload)


# ---------------------------------------------------------------------------
# Real dense text adapter — fastembed (e.g. BAAI/bge-small-en-v1.5).
# ---------------------------------------------------------------------------
class TextDenseAdapter:
    modality: Modality = "text"

    def __init__(self, name: str, model: str, version: str):
        from fastembed import TextEmbedding

        self.name = name
        self.version = version
        self._model = TextEmbedding(model_name=model)
        # Derive the dimension from the model — never hardcode it.
        self.dim = len(list(self._model.embed(["dim probe"]))[0])

    def embed_batch(self, payloads: list) -> List[List[float]]:
        for p in payloads:
            _reject_wrong_modality(p, "text")
        vecs = list(self._model.embed(list(payloads)))
        return [as_unit_vector([float(x) for x in v]) for v in vecs]

    def embed(self, payload) -> List[float]:
        return self.embed_batch([payload])[0]


# ---------------------------------------------------------------------------
# CLIP cross-modal adapters — one model family, two named vectors, one shared
# 512-dim space, so a text query retrieves a photo (backend.md §3.3).
# ---------------------------------------------------------------------------
class ClipTextAdapter:
    modality: Modality = "text"

    def __init__(self, name: str, model: str, version: str):
        from fastembed import TextEmbedding

        self.name = name
        self.version = version
        # The config carries the family name; fastembed splits it into legs.
        self._model = TextEmbedding(model_name=_clip_leg(model, "text"))
        self.dim = len(list(self._model.embed(["dim probe"]))[0])

    def embed_batch(self, payloads: list) -> List[List[float]]:
        for p in payloads:
            _reject_wrong_modality(p, "text")
        vecs = list(self._model.embed(list(payloads)))
        return [as_unit_vector([float(x) for x in v]) for v in vecs]

    def embed(self, payload) -> List[float]:
        return self.embed_batch([payload])[0]


class ClipVisionAdapter:
    modality: Modality = "vision"

    def __init__(self, name: str, model: str, version: str):
        from fastembed import ImageEmbedding

        self.name = name
        self.version = version
        self._model = ImageEmbedding(model_name=_clip_leg(model, "vision"))
        self.dim = len(list(self._model.embed([self._probe_image()]))[0])

    @staticmethod
    def _probe_image():
        from PIL import Image

        return Image.new("RGB", (8, 8), (0, 0, 0))

    def _to_image(self, payload):
        if payload.__class__.__module__.startswith("PIL."):
            return payload
        from PIL import Image

        return Image.open(io.BytesIO(bytes(payload))).convert("RGB")

    def embed_batch(self, payloads: list) -> List[List[float]]:
        images = []
        for p in payloads:
            _reject_wrong_modality(p, "vision")
            images.append(self._to_image(p))
        vecs = list(self._model.embed(images))
        return [as_unit_vector([float(x) for x in v]) for v in vecs]

    def embed(self, payload) -> List[float]:
        return self.embed_batch([payload])[0]


def _clip_leg(model: str, leg: Literal["text", "vision"]) -> str:
    """Map a CLIP family name to its fastembed per-modality checkpoint."""
    base = model.rstrip("/")
    if base.endswith("-text") or base.endswith("-vision"):
        # Already a leg name — trust it.
        return base
    return f"{base}-{leg}"


# Backward-compatible name for the earlier "Step 1" text adapter.
TextAdapter = TextDenseAdapter


# ---------------------------------------------------------------------------
# Generators (Phase 7) — the RAG backends behind the `Generator` Protocol. All
# three answer ONLY from the retrieved context they are handed; none reaches for
# outside knowledge. Each carries an `answer_path` label the Answer layer reports
# so the UI can show which path served a given answer.
#
# The registry (`registry.build_generators`) is the only place that names these
# classes; the kernel talks to them through the Protocol, so swapping the model
# is a one-line YAML edit. `httpx` is imported lazily inside the network-backed
# generators (mirroring how `fastembed` is imported lazily above) — this module
# must stay importable on a tiny device with no HTTP stack loaded, and the
# answer path must not pull in the sync transport / network simulator.
# ---------------------------------------------------------------------------
def build_grounded_prompt(question: str, context: List[str]) -> str:
    """Assemble the strict-RAG prompt: the model may use ONLY these facts.

    Kept here (not in the orchestrator) so every network-backed generator frames
    the grounding instruction identically. The Answer layer has already selected
    and ordered the context lines (CONFIRMED first, DISPUTED flagged).
    """
    if context:
        facts = "\n".join(f"- {line}" for line in context)
    else:
        facts = "(no relevant local records)"
    return (
        "You are a disaster-response field assistant. Answer the question using "
        "ONLY the facts listed in the context below. Do not use any outside "
        "knowledge. If the context does not contain the answer, say you do not "
        "have that information. Treat any fact marked [DISPUTED] as unconfirmed.\n\n"
        f"Context:\n{facts}\n\n"
        f"Question: {question}\n"
        "Answer:"
    )


class ExtractiveGenerator:
    """The always-available fallback: no model, no network — it stitches the top
    retrieved snippets into a short grounded summary. This is what keeps a tiny
    device (or one with no reachable model) able to answer at all, and it can
    never fabricate because its output is assembled straight from the context.
    """

    name = "extractive"
    answer_path = "extractive"

    def __init__(self, max_snippets: int = 3):
        self.max_snippets = max_snippets

    def generate(self, question: str, context: List[str]) -> str:
        if not context:
            return "No local records match that question."
        snippets = context[: self.max_snippets]
        return "Based on local records: " + " | ".join(snippets)


class OllamaGenerator:
    """On-device generation via a local Ollama daemon (default `qwen2.5:1.5b`).

    This is the `offline` path: the model runs on the device, so it needs no
    connectivity. `generate` raises on any transport/HTTP error so the Answer
    layer falls back to the next generator in the chain (ultimately extractive)
    rather than returning nothing.
    """

    answer_path = "offline"

    def __init__(
        self,
        model: str = "qwen2.5:1.5b",
        endpoint: str = "http://localhost:11434",
        version: str = None,
        timeout: float = 60.0,
    ):
        self.model = model
        self.name = model
        self.version = version or model
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def generate(self, question: str, context: List[str]) -> str:
        import httpx

        prompt = build_grounded_prompt(question, context)
        r = httpx.post(
            f"{self.endpoint}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["response"].strip()


class CloudGenerator:
    """Online generation via an OpenAI-compatible chat endpoint.

    This is the `online` path. Same grounding contract as the offline model;
    `generate` raises on failure so the chain can fall back.
    """

    answer_path = "online"

    def __init__(
        self,
        model: str,
        endpoint: str,
        api_key: str = "",
        version: str = None,
        timeout: float = 60.0,
    ):
        self.model = model
        self.name = model
        self.version = version or model
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def generate(self, question: str, context: List[str]) -> str:
        import httpx

        prompt = build_grounded_prompt(question, context)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        r = httpx.post(
            f"{self.endpoint}/v1/chat/completions",
            json={"model": self.model, "messages": [{"role": "user", "content": prompt}]},
            headers=headers,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
