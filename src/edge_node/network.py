"""Network layer — a transport interceptor that emulates intermittent links.

It wraps **only** the sync client used by `push`/`pull` (the egress to the Cloud
Gateway). The query and answer paths never import this module (AGENTS.md §5
invariant 8, backend.md §2.5): connectivity affects sync alone, never local
search. A static import-graph test enforces that isolation.

Three global modes (backend.md §2.2). Every effect is **real**, never a label:

  * ``offline``  — the connection is refused immediately (`NetworkError`); the
                   sync worker never reaches the hub, so the outbox accumulates
                   while the device stays fully queryable.
  * ``degraded`` — a real ``time.sleep`` latency (default 400-1200 ms), a
                   token-bucket byte cap over the ACTUAL serialized payload bytes
                   (default ~64 kbps), and a real failure rate (default ~8%) that
                   genuinely raises so the caller leaves the point pending.
  * ``full``     — no injection; the wrapped transport behaves normally.

Per push we log bytes / duration / attempted-accepted-failed / priority mix /
mode, so the sync dashboard reports measured numbers rather than asserted ones.
"""

import json
import random
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

# --- modes -----------------------------------------------------------------
OFFLINE = "offline"
DEGRADED = "degraded"
FULL = "full"
_MODES = (OFFLINE, DEGRADED, FULL)


class NetworkError(Exception):
    """A real wire failure: connection refused (offline) or a dropped request
    (degraded). It propagates through the transport so `push` leaves the point
    pending — never silent data loss."""


@dataclass
class NetworkConfig:
    # Injected round-trip latency window, in milliseconds.
    latency_min_ms: float = 400.0
    latency_max_ms: float = 1200.0
    # Byte-rate cap over real payload bytes. ~64 kbps ≈ 8000 bytes/sec.
    # 0 disables the cap (used in tests where only latency/failure is measured).
    byte_rate_bytes_per_sec: float = 8000.0
    # Fraction of requests that genuinely fail on a degraded link.
    failure_rate: float = 0.08
    # RNG seed; None = system entropy. Set in tests to make failures deterministic.
    seed: Optional[int] = None


class TokenBucket:
    """A real token-bucket rate limiter over bytes.

    ``consume(n)`` blocks (a genuine sleep) until ``n`` byte-tokens have
    accrued, so pushing a large payload on a capped link actually takes the
    time the cap implies. Thread-safe for the single sync worker.
    """

    def __init__(self, bytes_per_sec: float, capacity: Optional[float] = None):
        self.rate = float(bytes_per_sec)
        self.capacity = float(capacity) if capacity is not None else float(bytes_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def consume(self, nbytes: float) -> None:
        if self.rate <= 0:
            return  # cap disabled
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= nbytes:
                    self._tokens -= nbytes
                    return
                # How long until enough tokens have accrued.
                deficit = nbytes - self._tokens
                wait = deficit / self.rate
            time.sleep(wait)


# --- global mutable state (one link per process) ---------------------------
_mode: str = FULL
_config = NetworkConfig()
_rng = random.Random()
_bucket: Optional[TokenBucket] = None
_push_log: List[Dict[str, Any]] = []
_LOG_CAP = 200


def _rebuild_bucket() -> None:
    global _bucket
    _bucket = (
        TokenBucket(_config.byte_rate_bytes_per_sec)
        if _config.byte_rate_bytes_per_sec > 0
        else None
    )


def reset() -> None:
    """Return to FULL with default config and an empty log (used by tests)."""
    global _mode, _config, _rng, _push_log
    _mode = FULL
    _config = NetworkConfig()
    _rng = random.Random()
    _push_log = []
    _rebuild_bucket()


def set_mode(mode: str) -> None:
    global _mode
    if mode not in _MODES:
        raise ValueError(f"unknown network mode {mode!r}; expected one of {_MODES}")
    _mode = mode


def get_mode() -> str:
    return _mode


def configure(**kwargs: Any) -> None:
    """Override link parameters (from YAML at startup, or from a test).

    Recognises the fields of ``NetworkConfig``. Re-seeds the RNG and rebuilds
    the token bucket so the change takes effect immediately.
    """
    global _config, _rng
    fields = _config.__dict__.copy()
    for k, v in kwargs.items():
        if k not in fields:
            raise ValueError(f"unknown network config key {k!r}")
        fields[k] = v
    _config = NetworkConfig(**fields)
    _rng = random.Random(_config.seed)
    _rebuild_bucket()


def config_dict() -> Dict[str, Any]:
    return asdict(_config)


def get_push_log() -> List[Dict[str, Any]]:
    return list(_push_log)


def clear_push_log() -> None:
    _push_log.clear()


def _record_push(entry: Dict[str, Any]) -> None:
    _push_log.append(entry)
    if len(_push_log) > _LOG_CAP:
        del _push_log[0 : len(_push_log) - _LOG_CAP]


def _apply(nbytes: int) -> None:
    """Apply the current link's real effects to a single request of ``nbytes``.

    Raises ``NetworkError`` when the link refuses (offline) or drops the request
    (degraded failure). Returns after any real latency + byte-cap wait.
    """
    if _mode == OFFLINE:
        # Refuse immediately, before any latency — the wire is simply down.
        raise NetworkError("connection refused (offline)")
    if _mode == DEGRADED:
        lo, hi = _config.latency_min_ms, _config.latency_max_ms
        latency_ms = _rng.uniform(lo, hi) if hi > lo else lo
        if latency_ms > 0:
            time.sleep(latency_ms / 1000.0)
        if _bucket is not None and nbytes > 0:
            _bucket.consume(nbytes)
        if _rng.random() < _config.failure_rate:
            raise NetworkError("request dropped (degraded link)")
    # FULL: no injection.


def _byte_len(obj: Any) -> int:
    try:
        return len(json.dumps(obj, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(obj).encode("utf-8"))


def _priority_mix(envelopes: List[Dict[str, Any]]) -> Dict[str, int]:
    mix: Dict[str, int] = {}
    for env in envelopes:
        prio = (env.get("payload") or {}).get("_sync_meta", {}).get("sync_priority", "ROUTINE")
        mix[prio] = mix.get(prio, 0) + 1
    return mix


class NetworkTransport:
    """Decorates any ``SyncTransport`` with the current link's real effects.

    Only the sync methods (`get_delta`/`push`/`pull`/`pull_snapshot`) pass
    through the wire; the query path holds no reference to this object, so search
    is never gated by the link.
    """

    def __init__(self, inner: Any):
        self.inner = inner

    def get_delta(self, device_id: str) -> int:
        _apply(_byte_len(device_id))
        return self.inner.get_delta(device_id)

    def push(self, device_id: str, envelopes: List[Dict[str, Any]]) -> Dict[str, Any]:
        nbytes = _byte_len({"device_id": device_id, "envelopes": envelopes})
        attempted = len(envelopes)
        t0 = time.perf_counter()
        entry = {
            "mode": _mode,
            "bytes": nbytes,
            "attempted": attempted,
            "accepted": 0,
            "failed": attempted,
            "priority_mix": _priority_mix(envelopes),
        }
        try:
            _apply(nbytes)
            ack = self.inner.push(device_id, envelopes)
            entry["accepted"] = ack.get("count", len(ack.get("acked_ids", [])))
            entry["failed"] = attempted - entry["accepted"]
            return ack
        finally:
            entry["duration_ms"] = (time.perf_counter() - t0) * 1000.0
            _record_push(entry)

    def pull(self, device_id: str) -> List[Dict[str, Any]]:
        _apply(_byte_len(device_id))
        return self.inner.pull(device_id)

    def pull_snapshot(self, device_id: str, manifest: Dict[str, Any]) -> Optional[str]:
        _apply(_byte_len(manifest))
        return self.inner.pull_snapshot(device_id, manifest)


# Build the default bucket at import.
_rebuild_bucket()
