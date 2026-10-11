#!/usr/bin/env python3
"""Verify a hash-sealed evidence packet (D10 / T12).

Usage:
    python tools/verify_packet.py path/to/packet.json
    curl -s $GATEWAY/evidence/zone_c_gas | python tools/verify_packet.py -

What it checks (each independently):
  1. SEAL   — recompute the canonical-JSON SHA-256 over the packet's core and
              compare it to the stored `hash`. Any flipped byte, dropped event,
              or nudged parameter changes the hash, so tampering is detected.
  2. FOLD   — re-run the PURE `fold_consensus` on the packet's own events with
              the packet's fold parameters and confirm the verdict (status +
              value, and the full fold) matches byte-for-byte. The packet cannot
              carry a verdict its events do not actually produce.
  3. MODULE — compare the packet's `fold_module_sha256` to the local
              `consensus_fold.py`. A mismatch means a DIFFERENT fold produced the
              packet; the re-run above still uses the local, auditable module.
  4. SIGN   — if an admin secret is available (``--secret`` or
              ``GATEWAY_ADMIN_TOKEN``) and the packet carries a `signature`,
              verify the HMAC. A bare hash detects accidental change only; the
              signature is what detects forgery.

Exit code 0 iff every applicable check passes. The packet proves the verdict is
a faithful function of the recorded events — NOT that those events are true.
"""

import argparse
import hmac
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "cloud-gateway"))

import evidence  # noqa: E402
from consensus_fold import derive_device_trust, fold_consensus  # noqa: E402

_LOCAL_FOLD_MODULE = os.path.join(_HERE, "..", "cloud-gateway", "consensus_fold.py")


def _load(path: str) -> dict:
    text = sys.stdin.read() if path == "-" else open(path, "r", encoding="utf-8").read()
    return json.loads(text)


def verify(packet: dict, secret: str | None = None) -> list[tuple[str, bool, str]]:
    """Return a list of (check, passed, detail)."""
    results: list[tuple[str, bool, str]] = []

    # 1. SEAL
    recomputed = evidence.recompute_hash(packet)
    seal_ok = hmac.compare_digest(recomputed, str(packet.get("hash", "")))
    results.append(("SEAL", seal_ok, f"stored={packet.get('hash')} recomputed={recomputed}"))

    # 2. FOLD — reconstruct the events (re-attach the single corroboration_key the
    # projection drops) and re-run the pure fold with the packet's parameters.
    key = packet.get("corroboration_key")
    fp = packet.get("fold_params", {})
    events = [dict(e, corroboration_key=key) for e in packet.get("events", [])]
    trust = derive_device_trust(
        events,
        threshold=fp.get("threshold", 0.66),
        decay=fp.get("decay", 0.0),
        alpha=fp.get("trust_alpha", 0.0),
        beta=fp.get("trust_beta", 0.0),
        trust_floor=fp.get("trust_floor", 0.0),
        trust_ceiling=fp.get("trust_ceiling", 1.0),
    )
    refold = fold_consensus(
        events,
        threshold=fp.get("threshold", 0.66),
        decay=fp.get("decay", 0.0),
        device_trust=trust,
        cross_modal_weight=fp.get("cross_modal_weight", 0.3),
    )
    fold_ok = evidence.canonical_json(refold) == evidence.canonical_json(packet.get("fold", {}))
    stored_fold = packet.get("fold", {})
    results.append((
        "FOLD",
        fold_ok,
        f"recomputed status={refold.get('status')} value={refold.get('value')!r}; "
        f"stored status={stored_fold.get('status')} value={stored_fold.get('value')!r}",
    ))

    # 2b. TRUST — the re-derived per-device trust must match what the packet stored.
    trust_ok = evidence.canonical_json(trust) == evidence.canonical_json(packet.get("device_trust", {}))
    results.append(("TRUST", trust_ok, "re-derived trust matches" if trust_ok else "trust MISMATCH"))

    # 3. MODULE
    try:
        local_sha = evidence.module_source_sha256(_LOCAL_FOLD_MODULE)
        module_ok = local_sha == packet.get("fold_module_sha256")
        results.append((
            "MODULE", module_ok,
            f"local={local_sha} packet={packet.get('fold_module_sha256')}",
        ))
    except OSError as e:
        results.append(("MODULE", False, f"could not read local fold module: {e}"))

    # 4. SIGN (only when both a secret and a signature are present)
    sig = packet.get("signature")
    if secret and sig:
        expected = evidence.sign(str(packet.get("hash", "")), secret)
        sign_ok = hmac.compare_digest(expected, str(sig))
        results.append(("SIGN", sign_ok, "signature verified" if sign_ok else "signature MISMATCH"))
    elif sig and not secret:
        results.append(("SIGN", True, "signature present but no secret supplied — not checked"))

    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a hash-sealed evidence packet.")
    ap.add_argument("packet", help="path to the packet JSON, or - for stdin")
    ap.add_argument("--secret", default=os.environ.get("GATEWAY_ADMIN_TOKEN"),
                    help="admin secret for signature verification (or GATEWAY_ADMIN_TOKEN)")
    args = ap.parse_args()

    packet = _load(args.packet)
    results = verify(packet, secret=args.secret)

    all_ok = True
    for check, passed, detail in results:
        # A SIGN result that is informational (present, not checked) must not fail
        # the run; it is reported as PASS with its note above.
        all_ok = all_ok and passed
        print(f"[{'PASS' if passed else 'FAIL'}] {check}: {detail}")
    print("\nNote: this proves the verdict faithfully follows the recorded events, "
          "not that the events are true.")
    print("RESULT:", "VERIFIED" if all_ok else "TAMPERED/INVALID")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
