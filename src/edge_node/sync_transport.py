"""Sync transport — the edge node's ONLY egress to the Cloud Gateway.

The query and answer paths must never import this module (AGENTS.md §5,
invariant 8); only `push`/`pull` do. The runtime path wires `GatewayTransport`
(real HTTP to a real gateway backed by a real Qdrant Server). Tests inject an
in-process stub instead — no network, but the same shape.

Envelope pushed per point: `{id, vector, sparse, payload, client_sequence}`.
The gateway upserts it (idempotent by deterministic id) and appends an OBSERVED
event to the `fact_events` collection; the delta handshake returns the highest
`client_sequence` the hub already holds for a device so we push deltas only,
never a full snapshot.
"""

import os
import tempfile
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

import httpx


@runtime_checkable
class SyncTransport(Protocol):
    def get_delta(self, device_id: str) -> int:
        """Highest client_sequence the hub holds for this device (-1 if none)."""
        ...

    def push(self, device_id: str, envelopes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Ingest a delta batch. Returns {"acked_ids": [...], "count": n}.
        Raises on any transport/HTTP failure so the caller leaves points pending.
        """
        ...

    def pull(self, device_id: str) -> List[Dict[str, Any]]:
        """Return the envelopes the hub holds for this device."""
        ...

    def pull_snapshot(
        self, device_id: str, manifest: Dict[str, Any]
    ) -> Optional[str]:
        """Fetch a partial snapshot of the fleet's facts as a filesystem path."""
        ...

    def retract(
        self, device_id: str, point_id: int, corroboration_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Notify the hub of a retracted point."""
        ...

    def get_cloud_state(self) -> Dict[str, Any]:
        """Fetch merged fleet state from the hub."""
        ...

    def inject_conflict(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Inject a reproducible conflict into the gateway's event history."""
        ...


class GatewayTransport:
    """Real HTTP client to the Cloud Gateway (`cloud-gateway/`)."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_delta(self, device_id: str) -> int:
        r = httpx.get(f"{self.base_url}/delta/{device_id}", timeout=self.timeout)
        r.raise_for_status()
        return int(r.json()["max_client_sequence"])

    def push(self, device_id: str, envelopes: List[Dict[str, Any]]) -> Dict[str, Any]:
        r = httpx.post(
            f"{self.base_url}/ingest",
            json={"device_id": device_id, "envelopes": envelopes},
            timeout=self.timeout,
        )
        r.raise_for_status()  # a 5xx / connection error propagates -> caller keeps pending
        return r.json()

    def retract(
        self, device_id: str, point_id: int, corroboration_key: Optional[str] = None
    ) -> Dict[str, Any]:
        r = httpx.post(
            f"{self.base_url}/retract",
            json={
                "device_id": device_id,
                "point_id": point_id,
                "corroboration_key": corroboration_key,
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_cloud_state(self) -> Dict[str, Any]:
        r = httpx.get(f"{self.base_url}/cloud/state", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def inject_conflict(self, request: Dict[str, Any]) -> Dict[str, Any]:
        r = httpx.post(f"{self.base_url}/demo/inject-conflict", json=request, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def pull(self, device_id: str) -> List[Dict[str, Any]]:
        r = httpx.get(f"{self.base_url}/facts/{device_id}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()["envelopes"]

    def pull_snapshot(
        self, device_id: str, manifest: Dict[str, Any]
    ) -> Optional[str]:
        """POST the device's manifest to the hub and stream the returned Edge
        snapshot tar to a temp file, whose path we hand back for
        ``update_from_snapshot``. A 204 means the hub has nothing new -> None.
        """
        r = httpx.post(
            f"{self.base_url}/snapshot/{device_id}",
            json={"manifest": manifest},
            timeout=self.timeout,
        )
        if r.status_code == 204:
            return None
        r.raise_for_status()
        fd, path = tempfile.mkstemp(prefix="edge_snapshot_", suffix=".tar")
        with os.fdopen(fd, "wb") as fh:
            fh.write(r.content)
        return path
