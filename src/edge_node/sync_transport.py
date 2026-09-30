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

from typing import Any, Dict, List, Protocol, runtime_checkable

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

    def pull(self, device_id: str) -> List[Dict[str, Any]]:
        r = httpx.get(f"{self.base_url}/facts/{device_id}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()["envelopes"]
