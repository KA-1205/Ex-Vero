"""Hash-sealed evidence packet (D10 / T12) — pure, dependency-free.

An evidence packet is a self-contained, reproducible record of how one
`corroboration_key` folded to its verdict: the event log that fed the fold, the
fold parameters, the derived trust, the fold output, and a SHA-256 of the exact
`consensus_fold.py` that produced it. A canonical-JSON hash seals the packet so
any later change — one flipped byte, a dropped event, a nudged parameter — is
detectable, and a verifier can re-run the pure fold and confirm the verdict.

Honesty bounds (stated to the UI too):
  * a bare hash detects **accidental** change only; it does not prove the packet
    was not forged by whoever produced it — use the HMAC `signature` for that;
  * the packet proves the verdict is a faithful function of the recorded events,
    NOT that those events are true. It never claims to prove ground truth.

The module is pure (no network, no clock reads inside the hash) so the SAME
inputs yield **byte-identical** canonical bytes across runs and Python versions,
which the verifier relies on. Volatile wall-clock fields are kept out of the
hashed core on purpose.

This module is imported by both the gateway (`app.py` `GET /evidence/{key}`) and
the standalone verifier (`tools/verify_packet.py`), so both seal and check the
packet with the exact same code.
"""

import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional

# The packet layout version — bump only on a breaking change to the hashed core.
SCHEMA_VERSION = 1

# Floats are rounded to this many decimal places before hashing, so summation
# jitter or a platform's float repr can never change the canonical bytes.
CANON_FLOAT_DP = 6

# Per-event fields carried into the (hashed) packet. Volatile wall-clock fields
# (`received_at_ns`, `client_timestamp_ns`) are deliberately excluded so the hash
# is reproducible; the `value` is whatever the hub stored (already redacted on
# the hub, so no raw PII enters the packet).
EVENT_FIELDS = (
    "device_id",
    "event_type",
    "value",
    "value_cluster",
    "seq",
    "client_sequence",
    "device_trust_at_report",
)


# The keys that make up the hashed core of a packet, in no particular order
# (canonical_json sorts them). Anything NOT in this set — `hash`, `signature`,
# a UI `disclaimer` — rides outside the seal and never affects the hash.
CORE_KEYS = (
    "schema_version",
    "corroboration_key",
    "fold",
    "fold_params",
    "device_trust",
    "events",
    "fold_module_sha256",
)


def _round_floats(obj: Any) -> Any:
    """Recursively round every float to CANON_FLOAT_DP, so the canonical bytes
    do not depend on float repr jitter across platforms/versions."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return round(obj, CANON_FLOAT_DP)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, `,`/`:` separators, UTF-8, floats
    rounded to CANON_FLOAT_DP. Deterministic across runs and Python versions."""
    rounded = _round_floats(obj)
    return json.dumps(
        rounded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def module_source_sha256(path: str) -> str:
    """SHA-256 of the fold module's source bytes, so the packet records exactly
    which `consensus_fold.py` produced the verdict."""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _packet_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Project the raw log onto the stable, non-volatile fields, ordered by hub
    seq so the list itself is deterministic."""
    projected = [
        {k: e.get(k) for k in EVENT_FIELDS}
        for e in events
    ]
    return sorted(projected, key=lambda e: (e.get("seq") or 0, e.get("device_id") or ""))


def build_packet(
    corroboration_key: Optional[str],
    events: List[Dict[str, Any]],
    folded: Dict[str, Any],
    fold_params: Dict[str, Any],
    device_trust: Dict[str, float],
    fold_module_sha256: str,
) -> Dict[str, Any]:
    """Assemble the hashed core of an evidence packet and seal it with `hash`.

    `folded` must be the DETERMINISTIC fold (no injected `now`/half-life), so the
    verifier can reproduce it from the events alone. The `hash` seals every field
    below; a `signature` (if any) is added by the caller OUTSIDE this core.
    """
    core = {
        "schema_version": SCHEMA_VERSION,
        "corroboration_key": corroboration_key,
        "fold": folded,
        "fold_params": fold_params,
        "device_trust": device_trust,
        "events": _packet_events(events),
        "fold_module_sha256": fold_module_sha256,
    }
    core["hash"] = hashlib.sha256(canonical_json(core)).hexdigest()
    return core


def recompute_hash(packet: Dict[str, Any]) -> str:
    """Recompute the seal over ONLY the known core keys, so UI/transport extras
    (`signature`, `disclaimer`, …) can ride alongside without affecting it."""
    core = {k: packet[k] for k in CORE_KEYS if k in packet}
    return hashlib.sha256(canonical_json(core)).hexdigest()


def sign(packet_hash: str, secret: str) -> str:
    """HMAC-SHA256 over the packet hash with an admin secret — this is what makes
    the packet unforgeable, as opposed to the bare hash (accidental change only)."""
    return hmac.new(secret.encode("utf-8"), packet_hash.encode("utf-8"), hashlib.sha256).hexdigest()
