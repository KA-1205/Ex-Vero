"""Registry — resolve the config `models:` table into adapter objects.

This is the single place that maps a provider string in YAML to a concrete
adapter class. Nothing else in the kernel names a model class, so swapping a
model is a one-line config edit.
"""
from typing import List

import yaml

from .adapter import (
    Embedder,
    FakeEmbedder,
    TextDenseAdapter,
    ClipTextAdapter,
    ClipVisionAdapter,
)

_VALID_MODALITIES = {"text", "vision"}


def _resolve_embedder(spec: dict) -> Embedder:
    """Turn one embedder spec into an adapter object."""
    name = spec.get("name")
    modality = spec.get("modality")
    provider = spec.get("provider")
    model = spec.get("model")
    version = spec.get("version") or model or name

    if modality not in _VALID_MODALITIES:
        raise ValueError(f"unknown modality '{modality}' for embedder '{name}'")

    if provider == "fake":
        # `dim` is legitimately config-driven here: a fake has no model to
        # derive it from. Real adapters below derive dim from the checkpoint.
        dim = spec.get("dim")
        if dim is None:
            raise ValueError(f"fake embedder '{name}' needs a 'dim'")
        return FakeEmbedder(name=name, dim=int(dim), modality=modality, version=version)

    if provider == "fastembed":
        if model is None:
            raise ValueError(f"fastembed embedder '{name}' needs a 'model'")
        # CLIP family is cross-modal: pick the leg by modality.
        if "clip" in model.lower():
            if modality == "text":
                return ClipTextAdapter(name=name, model=model, version=version)
            return ClipVisionAdapter(name=name, model=model, version=version)
        if modality != "text":
            raise ValueError(f"provider fastembed with modality '{modality}' needs a CLIP model")
        return TextDenseAdapter(name=name, model=model, version=version)

    raise ValueError(f"unknown provider '{provider}' for embedder '{name}'")


def from_config(config: dict) -> List[Embedder]:
    """Resolve the `models.embedders` list into adapter objects, in order."""
    models = config.get("models", {})
    specs = models.get("embedders", [])
    return [_resolve_embedder(s) for s in specs]


def _load_legacy(config: dict) -> List[Embedder]:
    """Resolve the pre-Phase-2 `adapters:` schema (kept for older tests)."""
    adapters: List[Embedder] = []
    for spec in config.get("adapters", []):
        modality = spec.get("modality")
        if modality != "text":
            raise ValueError(f"Unsupported modality: {modality}")
        adapters.append(
            TextDenseAdapter(
                name=spec.get("name"),
                model=spec.get("model"),
                version=spec.get("model"),
            )
        )
    return adapters


def load_adapters(config_path: str) -> List[Embedder]:
    """Read a YAML config and resolve its embedders.

    Accepts both the Phase 2 `models:` schema and the legacy `adapters:`
    schema so existing call sites keep working during the migration.
    """
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    if "models" in config:
        return from_config(config)
    return _load_legacy(config)
