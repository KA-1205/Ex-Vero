"""Embedding-model stamp & guard (D2 / T5, same work as Brief G2).

A shard, a pushed envelope batch and a pulled snapshot are only meaningful if
the model that produced their vectors is the *same* model that reads them.
Dimension alone does not prove that: two different checkpoints (or two sets of
weights) can share a dimension and still live in incompatible vector spaces, so
a cosine search across them returns silent garbage.

This module derives a **space id** per vector slot from what the model actually
does — its identity, its real dimension, and a probe vector it produces — never
from a human-typed version string. It is a *pure* helper: it takes an embedder
(or an already-computed stamp) and returns data; it never touches disk unless
you call the explicit `read_stamp` / `write_stamp` helpers, and it imports only
the standard library so the edge, the gateway and the tests can all use it.

Stamp shape (one per slot):

    {
      "name": str,        # the named vector slot, e.g. "text_dense"
      "modality": str,    # "text" | "vision"
      "dim": int,         # the model's real output dimension
      "space_id": str,    # sha256 over (name|modality|dim|rounded probe)
      "probe": [float],   # the unit probe vector, rounded (compared by cosine)
    }

Two stamps are *compatible* when name, modality and dim match AND their probe
vectors are within `min_cosine` of each other. Equality is never used — floats
re-serialised across machines/Python versions can differ in the last bit, so a
high-cosine check is the honest comparison (Improvement Prompt T5).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any, Dict, List, Optional

# The fixed strings/images the probe embeds. A single, stable probe per modality
# so the same model always yields the same probe vector.
PROBE_TEXT = "ex-vero model stamp probe v1"

# Cosine at/above which two probe vectors are treated as the same space. A
# proposed starting value (Improvement Prompt T5: "0.999 ... tune it"); callers
# may override from config/env. It lives here as the one named default rather
# than as a bare literal scattered through the guard logic.
DEFAULT_STAMP_COSINE = 0.999

# How many decimals the probe is rounded to before hashing/serialising, so the
# space_id is stable across runs and Python versions (mirrors the fold's _round).
PROBE_ROUND_DP = 6

STAMP_FILENAME = "space_stamp.json"


class StampMismatch(Exception):
    """Raised when a stored/pulled stamp is incompatible with the live model."""


def _probe_payload(modality: str):
    """The fixed payload a given modality embeds for its probe."""
    if modality == "text":
        return PROBE_TEXT
    if modality == "vision":
        # A fixed 8x8 black image — deterministic, no file needed.
        from PIL import Image

        return Image.new("RGB", (8, 8), (0, 0, 0))
    raise ValueError(f"unknown modality for probe: {modality}")


def _round_vec(vec: List[float]) -> List[float]:
    return [round(float(x), PROBE_ROUND_DP) for x in vec]


def _space_id(name: str, modality: str, dim: int, probe: List[float]) -> str:
    """A stable label over the model's identity + real behaviour. Cheap to
    compare for a fast reject; the probe cosine is the authoritative check."""
    canonical = json.dumps(
        {"name": name, "modality": modality, "dim": dim, "probe": probe},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def compute_stamp(embedder: Any) -> Dict[str, Any]:
    """Build the stamp for one loaded embedder by actually embedding its probe.

    `embedder` conforms to the adapter `Embedder` protocol (has `name`,
    `modality`, `dim`, and `embed`/`embed_batch`). The probe is embedded through
    the same code path real captures use, so the stamp reflects the real space.
    """
    modality = embedder.modality
    payload = _probe_payload(modality)
    if hasattr(embedder, "embed"):
        probe = embedder.embed(payload)
    else:
        probe = embedder.embed_batch([payload])[0]
    probe = _round_vec(list(probe))
    dim = int(getattr(embedder, "dim", len(probe)))
    name = embedder.name
    return {
        "name": name,
        "modality": modality,
        "dim": dim,
        "space_id": _space_id(name, modality, dim, probe),
        "probe": probe,
    }


def fleet_stamp(adapters: List[Any]) -> Dict[str, Dict[str, Any]]:
    """slot name -> stamp for every loaded adapter (the device's whole space)."""
    return {a.name: compute_stamp(a) for a in adapters}


def cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return -1.0
    return dot / (na * nb)


def stamps_compatible(
    a: Dict[str, Any], b: Dict[str, Any], min_cosine: float = DEFAULT_STAMP_COSINE
) -> bool:
    """True when two slot stamps describe the same vector space.

    name/modality/dim must match exactly; the probe vectors must agree by cosine
    (never equality). The fast `space_id` match short-circuits the common case.
    """
    if a.get("name") != b.get("name"):
        return False
    if a.get("modality") != b.get("modality"):
        return False
    if a.get("dim") != b.get("dim"):
        return False
    if a.get("space_id") == b.get("space_id"):
        return True
    pa, pb = a.get("probe"), b.get("probe")
    if not pa or not pb:
        # No probe to compare and space_ids already differed → incompatible.
        return False
    return cosine(pa, pb) >= min_cosine


def diff_fleet(
    local: Dict[str, Dict[str, Any]],
    other: Dict[str, Dict[str, Any]],
    min_cosine: float = DEFAULT_STAMP_COSINE,
) -> List[str]:
    """Return a list of human-readable incompatibilities between two fleet
    stamps, comparing only the slots present in BOTH. An empty list means every
    shared slot is compatible. Slots unique to one side are not an error (a
    low-memory profile may simply not load vision)."""
    problems: List[str] = []
    for slot, other_stamp in other.items():
        if slot not in local:
            continue
        if not stamps_compatible(local[slot], other_stamp, min_cosine):
            problems.append(
                f"slot '{slot}': local space_id={local[slot].get('space_id')} "
                f"dim={local[slot].get('dim')} vs incoming "
                f"space_id={other_stamp.get('space_id')} dim={other_stamp.get('dim')}"
            )
    return problems


def _stamp_path(shard_dir: str) -> str:
    return os.path.join(shard_dir, STAMP_FILENAME)


def read_stamp(shard_dir: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """Read a shard dir's fleet stamp, or None if it was never stamped."""
    path = _stamp_path(shard_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_stamp(shard_dir: str, stamp: Dict[str, Dict[str, Any]]) -> None:
    """Stamp a shard dir with the fleet stamp (canonical, sorted) that produced
    its vectors."""
    os.makedirs(shard_dir, exist_ok=True)
    with open(_stamp_path(shard_dir), "w", encoding="utf-8") as fh:
        json.dump(stamp, fh, sort_keys=True, separators=(",", ":"))
