"""Test doubles for the sync transport.

`StubTransport` is an in-process stand-in for the Cloud Gateway used by the
Phase 3 unit tests. It is a **test double, kept out of `src/`** — the runtime
path only ever wires `GatewayTransport` (real HTTP). The stub lets us exercise
the mark-after-ack invariant deterministically:

  - it records every ingested envelope in an in-memory "server collection", so a
    test can assert the fact really landed on the hub;
  - it keeps a per-device max `client_sequence` so the delta handshake is real;
  - `fail_next` / `fail` toggles make it raise like a dead link, so a test can
    force a failure mid-push and prove nothing was marked synced.
"""

from typing import Dict, List, Any, Optional

from edge_node.snapshot import build_snapshot_tar


class TransportFailure(Exception):
    """Raised by the stub to imitate a refused/failed push."""


class StubTransport:
    def __init__(self):
        # device_id -> {point_id: envelope}   (the "server collection")
        self.store: Dict[str, Dict[int, Dict[str, Any]]] = {}
        # device_id -> highest client_sequence the hub holds
        self.max_seq: Dict[str, int] = {}
        # append-only event mirror the gateway would keep in fact_events
        self.events: List[Dict[str, Any]] = []
        # failure controls
        self.fail = False        # fail every push while True
        self.fail_next = False   # fail exactly one push, then clear

    # --- SyncTransport protocol ------------------------------------------
    def get_delta(self, device_id: str) -> int:
        return self.max_seq.get(device_id, -1)

    def push(self, device_id: str, envelopes: List[Dict[str, Any]]) -> Dict[str, Any]:
        if self.fail or self.fail_next:
            self.fail_next = False
            # Mimic a transport-layer error: nothing is persisted, caller must
            # NOT mark anything synced.
            raise TransportFailure("stub transport forced failure")

        dev = self.store.setdefault(device_id, {})
        acked = []
        for env in envelopes:
            # Idempotent upsert keyed by deterministic point id.
            dev[env["id"]] = env
            self.events.append({
                "device_id": device_id,
                "point_id": env["id"],
                "event_type": "OBSERVED",
                "client_sequence": env["client_sequence"],
            })
            self.max_seq[device_id] = max(self.max_seq.get(device_id, -1), env["client_sequence"])
            acked.append(env["id"])
        return {"acked_ids": acked, "count": len(acked)}

    def pull(self, device_id: str) -> List[Dict[str, Any]]:
        return list(self.store.get(device_id, {}).values())

    def pull_snapshot(self, device_id: str, manifest: Dict[str, Any]) -> Optional[str]:
        """Pack the WHOLE fleet's facts into a real Edge snapshot tar.

        The hub aggregates across devices, so a device pulls facts other devices
        reported — this is what proves cross-device learning. The `manifest`
        argument mirrors the real partial-snapshot handshake; the stub ships the
        full fact set (the restore is idempotent by point id) rather than
        diffing segments. It builds a genuine Edge shard via `build_snapshot_tar`
        and returns the path, so the test drives the real `update_from_snapshot`
        restore path with no network.
        """
        all_envelopes: List[Dict[str, Any]] = []
        for dev in self.store.values():
            all_envelopes.extend(dev.values())
        if not all_envelopes:
            return None
        # Dense vector name = the edge adapter name stamped on every point;
        # dim = the length of the stored dense vector.
        sample = all_envelopes[0]
        dense_name = (sample.get("payload") or {}).get("model", "text_dense")
        dim = len(sample["vector"])
        return build_snapshot_tar(all_envelopes, dense_name, dim)

    # --- test conveniences ------------------------------------------------
    def server_has(self, device_id: str, point_id: int) -> bool:
        return point_id in self.store.get(device_id, {})

    def server_count(self, device_id: str) -> int:
        return len(self.store.get(device_id, {}))
