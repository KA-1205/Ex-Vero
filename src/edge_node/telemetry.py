"""Telemetry — real cgroup/process CPU, RAM, and query latency (p50/p95).

CPU and memory come from the cgroup this process actually belongs to, so a
container run with `--cpus=1 --memory=512m` reports that budget rather than the
host's capacity. Nothing here is a constant, and nothing is mocked: when a
limit cannot be read the answer is `None`, never a plausible-looking number.

Both cgroup layouts are supported. v2 is a single tree (`cpu.max`, `cpu.stat`,
`memory.max`); v1 splits them into per-controller directories (`cpuacct.usage`,
`cpu.cfs_quota_us`, `memory.limit_in_bytes`), and is still what older hosts and
some container runtimes mount.

CPU is a *rate*, not a total: two samples of `usage_usec` are differenced over
the wall-clock interval between them and expressed as a percentage of the
cgroup's own quota. That is why 100% means "this container's whole CPU budget"
and a 4-core budget can legitimately read 400%.
"""

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# Where the cgroup filesystem is mounted. Overridable so the same parsing code
# can be pointed at a specific subtree (and exercised by tests); the default is
# the real mount, and a container with its own cgroup namespace needs no override.
CGROUP_ROOT_ENV = "EDGE_CGROUP_ROOT"
DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"


def _cgroup_root() -> str:
    return os.environ.get(CGROUP_ROOT_ENV, DEFAULT_CGROUP_ROOT)


def _read_text(path: str) -> Optional[str]:
    """Read a cgroup file, or None if it is absent or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def _read_int(path: str) -> Optional[int]:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


@dataclass
class TelemetryState:
    """Mutable state for tracking query latencies and computing percentiles."""
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=1000))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_latency(self, latency_ms: float) -> None:
        with self._lock:
            self.latencies_ms.append(latency_ms)

    def percentiles(self) -> tuple[float, float]:
        """Return (p50, p95) in milliseconds. Returns (0, 0) if no data."""
        with self._lock:
            if not self.latencies_ms:
                return 0.0, 0.0
            sorted_latencies = sorted(self.latencies_ms)
            n = len(sorted_latencies)
            p50 = sorted_latencies[n // 2]
            p95_idx = min(int(n * 0.95), n - 1)
            p95 = sorted_latencies[p95_idx]
            return p50, p95


# Global telemetry state
_telemetry_state = TelemetryState()


def record_query_latency(latency_ms: float) -> None:
    """Record a query latency for percentile computation."""
    _telemetry_state.record_latency(latency_ms)


def get_query_percentiles() -> tuple[float, float]:
    """Get (p50, p95) query latencies in milliseconds."""
    return _telemetry_state.percentiles()


def read_cgroup_cpu_quota() -> Optional[float]:
    """CPU cores this cgroup is allowed, or None when unlimited.

    v2 states it as a quota/period pair in `cpu.max`; v1 as
    `cpu.cfs_quota_us` / `cpu.cfs_period_us`. A literal "max" or a negative
    quota means no limit, which is None rather than 0.0 — zero would read as
    "this container gets no CPU", the opposite of the truth.
    """
    root = _cgroup_root()

    v2 = _read_text(os.path.join(root, "cpu.max"))
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    return quota / period
            except ValueError:
                pass

    quota_v1 = _read_int(os.path.join(root, "cpu", "cpu.cfs_quota_us"))
    period_v1 = _read_int(os.path.join(root, "cpu", "cpu.cfs_period_us"))
    if quota_v1 and quota_v1 > 0 and period_v1 and period_v1 > 0:
        return quota_v1 / period_v1

    return None


def read_cgroup_cpu_usage_usec() -> Optional[int]:
    """Total CPU microseconds consumed by this cgroup, or None if unreadable."""
    root = _cgroup_root()

    v2 = _read_text(os.path.join(root, "cpu.stat"))
    if v2:
        for line in v2.splitlines():
            if line.startswith("usage_usec "):
                try:
                    return int(line.split()[1])
                except (ValueError, IndexError):
                    return None

    # v1 reports nanoseconds, so convert rather than compare across units.
    v1_ns = _read_int(os.path.join(root, "cpu", "cpuacct.usage"))
    if v1_ns is not None:
        return v1_ns // 1000

    return None


def cpu_pct_between(
    prev_usage_usec: float,
    prev_ts: float,
    usage_usec: float,
    now_ts: float,
    quota: Optional[float],
) -> float:
    """CPU busy percentage of the cgroup's budget between two samples.

    `quota` is the cores allowed, so 100% means the container's entire CPU
    budget is in use. When no quota is set the reading is relative to a single
    core, which keeps the number meaningful on an unconstrained host.
    """
    elapsed_s = now_ts - prev_ts
    if elapsed_s <= 0:
        return 0.0

    used_cores = (usage_usec - prev_usage_usec) / 1_000_000.0 / elapsed_s
    if used_cores < 0:
        # A counter reset (cgroup recreated) is not negative usage.
        return 0.0

    budget = quota if quota else 1.0
    return max(0.0, used_cores / budget * 100.0)


# Shortest interval that yields a trustworthy rate.
#
# cgroup CPU accounting is quantised to the scheduler tick, so a window much
# shorter than a tick reports the cost of a partial slice as if it covered the
# whole window. Measured on a saturated 1-CPU cgroup: a 20 ms window read a
# median of 108% and peaked at 134%, while 250 ms settled on 100%. Polling
# faster than this must therefore reuse the last real reading rather than
# invent a new one.
MIN_CPU_SAMPLE_INTERVAL_S = 0.25


class _CpuBaseline:
    """Last cgroup CPU sample, so each call can report a rate.

    Held in one place with a lock because the telemetry route can be hit
    concurrently; without it two callers could interleave and difference
    against each other's sample.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usage_usec: Optional[float] = None
        self.ts: Optional[float] = None
        self.pct: float = 0.0

    def set(self, usage_usec: float, ts: float) -> None:
        with self._lock:
            self.usage_usec = usage_usec
            self.ts = ts
            self.pct = 0.0

    def sample(
        self,
        usage_usec: float,
        now_ts: float,
        quota: Optional[float],
        min_interval_s: float,
    ) -> tuple:
        """Advance the rate, or hand back the last trustworthy one.

        Reading under the lock and writing under it again would leave a window
        in which two callers both difference against the same baseline; the
        second would then erase the first's reading, and the CPU spent between
        them would never be reported at all.

        Returns `(pct, is_fresh)`. When the caller polls faster than
        `min_interval_s` the stored sample is left alone and the previous
        percentage is returned unchanged — reporting a fresh number here is
        what produces impossible readings like 250% on a 1-CPU cgroup.
        """
        with self._lock:
            if self.usage_usec is None or self.ts is None:
                pct = 0.0
            else:
                elapsed = now_ts - self.ts
                if 0 < elapsed < min_interval_s:
                    return self.pct, False
                pct = cpu_pct_between(
                    self.usage_usec, self.ts, usage_usec, now_ts, quota
                )

            self.usage_usec = usage_usec
            self.ts = now_ts
            self.pct = pct
            return pct, True

    def clear(self) -> None:
        with self._lock:
            self.usage_usec = None
            self.ts = None
            self.pct = 0.0


_cpu_baseline = _CpuBaseline()


def reset_cpu_baseline() -> None:
    """Forget the previous sample, so the next call reports no delta."""
    _cpu_baseline.clear()


def set_cpu_baseline(usage_usec: float, ts: float) -> None:
    """Seed the previous sample explicitly (test seam).

    Production code never calls this — `read_cgroup_cpu_pct` samples the real
    cgroup itself. It exists so a test can present a known previous reading and
    then check the rate the real one produces against it.
    """
    _cpu_baseline.set(float(usage_usec), float(ts))


def read_cgroup_cpu_pct() -> float:
    """Current CPU usage of this cgroup as a percentage of its quota.

    The first call after a reset has nothing to difference against and returns
    0.0 — honestly, rather than inventing a figure. Later calls report the rate
    observed since the previous sample, provided enough time has passed for
    that rate to mean anything (see MIN_CPU_SAMPLE_INTERVAL_S); polled faster
    than that, the last real reading is returned unchanged.
    """
    usage = read_cgroup_cpu_usage_usec()
    if usage is None:
        return 0.0

    pct, _fresh = _cpu_baseline.sample(
        float(usage),
        time.monotonic(),
        read_cgroup_cpu_quota(),
        MIN_CPU_SAMPLE_INTERVAL_S,
    )
    return pct


def read_process_rss_mb() -> float:
    """Read the process RSS (Resident Set Size) in MB from /proc/self/status or Windows API."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # Format: "VmRSS:    123456 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        rss_kb = int(parts[1])
                        return rss_kb / 1024.0
    except Exception:
        pass

    # Windows fallback
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            h = k32.GetCurrentProcess()
            func = getattr(k32, "K32GetProcessMemoryInfo", None)
            if func is None:
                psapi = ctypes.WinDLL("psapi", use_last_error=True)
                func = psapi.GetProcessMemoryInfo
            func.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            func.restype = wintypes.BOOL
            if func(h, ctypes.byref(pmc), pmc.cb):
                return round(pmc.WorkingSetSize / (1024.0 * 1024.0), 2)
        except Exception:
            pass

    return 0.0


def read_cgroup_memory_limit_mb() -> Optional[float]:
    """Memory this cgroup is allowed, in MB, or None when unlimited.

    Deliberately does NOT fall back to the host's total RAM. An unconstrained
    process has no memory budget of its own, and reporting the machine's RAM as
    its "limit" is how a 64 GB host ends up looking like a constrained target.
    """
    root = _cgroup_root()

    v2 = _read_text(os.path.join(root, "memory.max"))
    if v2:
        if v2 != "max":
            try:
                return int(v2) / (1024 * 1024)
            except ValueError:
                pass
    else:
        v1 = _read_int(os.path.join(root, "memory", "memory.limit_in_bytes"))
        if v1 is not None:
            # v1 signals "no limit" with a huge sentinel rather than a keyword.
            huge = (1 << 62)
            if v1 < huge:
                return v1 / (1024 * 1024)

    return None


def get_target_label() -> str:
    """Describe the target honestly, from the limits actually in force.

    Per backend.md §2.1 this is always an *emulated constrained target* named
    with its real container budget. Anything unreadable is said to be
    unreadable; the word "unknown" is never dressed up as a measurement.
    """
    quota = read_cgroup_cpu_quota()
    mem_mb = read_cgroup_memory_limit_mb()

    cpu_text = "no CPU limit" if quota is None else f"{quota:.1f} CPU"
    mem_text = "no memory limit" if mem_mb is None else f"{int(mem_mb)} MB"

    return f"emulated constrained target ({cpu_text} / {mem_text} container)"


def get_telemetry(model_load_ms: Optional[float] = None) -> dict:
    """Collect all telemetry into the shape expected by API.md §11.

    Args:
        model_load_ms: Optional model load time in milliseconds (from startup).

    Returns:
        Dict matching the telemetry response shape.
    """
    p50, p95 = get_query_percentiles()
    return {
        "target_label": get_target_label(),
        "cpu_pct": read_cgroup_cpu_pct(),
        "ram_mb": read_process_rss_mb(),
        "ram_limit_mb": read_cgroup_memory_limit_mb(),
        "model_load_ms": model_load_ms if model_load_ms is not None else 0.0,
        "query_latency_p50_ms": p50,
        "query_latency_p95_ms": p95,
    }