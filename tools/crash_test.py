"""D1 (T10) — crash-safety harness for the edge node.

Proves that captured facts survive a hard crash (SIGKILL), not just a graceful
shutdown. It runs a REAL edge node in a subprocess against a throwaway shard
directory, captures N facts, SIGKILLs the process, restarts it against the same
directory, and counts how many facts came back.

Verify-first (D1): the shard directory is `./shards` relative to the process cwd
and now also honours `EDGE_SHARD_DIR` (see edge_node.main.SHARD_BASE_PATH), so
this harness points the subprocess at a temp dir and never touches the repo's
./shards.

Usage:
    python tools/crash_test.py                 # N=50, writes results/crash_safety.json
    python tools/crash_test.py --n 12 --quiet   # smaller run (used by the test)

Exit code is non-zero if any captured fact was lost across the crash.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
RESULTS_PATH = REPO_ROOT / "results" / "crash_safety.json"
DEVICE_ID = "crash-dev"
# Text-only, hub-less, extractive config: a fast, offline subprocess start.
DEFAULT_CONFIG = REPO_ROOT / "config" / "tiny-edge.yaml"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_edge(shard_dir: str, port: int, config_path: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["EDGE_SHARD_DIR"] = shard_dir
    env["EDGE_CONFIG_PATH"] = config_path
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    # No hub, no network egress — capture + local persistence only.
    env.pop("EDGE_HUB_URL", None)
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "edge_node.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, cwd=shard_dir,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_health(base: str, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=2.0)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def run_crash_test(n: int = 50, config_path: Optional[str] = None, quiet: bool = False) -> Dict[str, Any]:
    config_path = config_path or str(DEFAULT_CONFIG)
    log = (lambda *a: None) if quiet else print

    with tempfile.TemporaryDirectory(prefix="crash_test_") as shard_dir:
        port = _free_port()
        base = f"http://127.0.0.1:{port}"

        # --- round 1: capture, then SIGKILL --------------------------------
        proc = _start_edge(shard_dir, port, config_path)
        try:
            if not _wait_health(base):
                proc.kill()
                raise RuntimeError("edge subprocess never became healthy")

            latencies = []
            for i in range(n):
                t0 = time.perf_counter()
                r = httpx.post(
                    f"{base}/devices/{DEVICE_ID}/capture",
                    json={"device_id": DEVICE_ID,
                          "corroboration_key": f"crash.k{i}",
                          "value": f"durable observation number {i} at the north dock"},
                    timeout=10.0,
                )
                r.raise_for_status()
                latencies.append((time.perf_counter() - t0) * 1000.0)

            pre = httpx.get(f"{base}/devices/{DEVICE_ID}/memory", timeout=10.0).json()["total"]
            log(f"captured {n}, visible before crash: {pre}")
        finally:
            proc.kill()
            proc.wait(timeout=30)

        # --- round 2: restart against the same dir, count survivors --------
        proc2 = _start_edge(shard_dir, port, config_path)
        try:
            if not _wait_health(base):
                proc2.kill()
                raise RuntimeError("edge subprocess never became healthy on restart")
            # Touch the device so its on-disk shards are loaded, then count.
            httpx.post(f"{base}/devices/{DEVICE_ID}/query",
                       json={"device_id": DEVICE_ID, "text": "observation"}, timeout=10.0)
            recovered = httpx.get(
                f"{base}/devices/{DEVICE_ID}/memory?limit=10000", timeout=10.0
            ).json()["total"]
            log(f"recovered after SIGKILL + restart: {recovered}/{n}")
        finally:
            proc2.kill()
            proc2.wait(timeout=30)

    lat_sorted = sorted(latencies)
    p50 = lat_sorted[len(lat_sorted) // 2] if lat_sorted else 0.0
    result = {
        "scenario": "capture_then_sigkill_then_restart",
        "captured": n,
        "recovered": recovered,
        "lost": n - recovered,
        "durable": recovered >= n,
        "capture_latency_ms_p50": round(p50, 2),
        "shard_dir_override": "EDGE_SHARD_DIR",
        "config": os.path.basename(config_path),
        "flush_added": True,
        "note": (
            "optimize() alone did NOT survive a SIGKILL (facts were lost); adding "
            "shard.flush() after each capture write and after mark_synced made "
            "captures durable with no WAL. mark_synced persists by the same "
            "flush path."
        ),
        "platform": sys.platform,
        "python": sys.version.split()[0],
    }
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out", default=str(RESULTS_PATH))
    args = ap.parse_args()

    result = run_crash_test(n=args.n, config_path=args.config, quiet=args.quiet)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    if not args.quiet:
        print(json.dumps(result, indent=2))
    return 0 if result["durable"] else 1


if __name__ == "__main__":
    sys.exit(main())
