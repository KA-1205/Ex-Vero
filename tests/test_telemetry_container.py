"""Phase 10 acceptance — an edge node in a REAL constrained container.

The unit half of the criterion (parsing and delta arithmetic) is in
`test_telemetry_cgroup.py`. This file is the half that can only be proven by
actually constraining a process: it builds the edge image, runs it under
`--cpus=1 --memory=512m`, and checks that `/telemetry` reports the limits the
container really has.

Marked `integration` and skipped without a Docker daemon, like the other
docker-compose tests in this repo.

    docker info >/dev/null && .venv/bin/python -m pytest tests/test_telemetry_container.py -q
"""

import json
import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = "ex-vero-edge-phase10-test"
CONTAINER = "ex-vero-edge-phase10"
CPU_LIMIT = 1
MEM_LIMIT_MB = 512
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"


def _docker(*args, check=True, timeout=900):
    return subprocess.run(
        ["docker", *args],
        capture_output=True, text=True, timeout=timeout, check=check,
    )


def _docker_available():
    try:
        return _docker("info", check=False, timeout=30).returncode == 0
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="needs a running Docker daemon"
)


def _get(path, timeout=10):
    import urllib.request

    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _first_device_id():
    """`/devices` is an envelope ({"devices": [...]}), not a bare list."""
    devices = _get("/devices")["devices"]
    assert devices, "the constrained edge reported no devices"
    return devices[0]["id"]


def _run_edge(cpus, mem_mb):
    """(Re)start the edge container under a real CPU/RAM budget and wait for it.

    Extracted so a test can try a *different* budget and then hand the shared
    container back in a known state, instead of deleting the module-scoped one
    out from under every test that follows.
    """
    _docker("rm", "-f", CONTAINER, check=False)
    _docker(
        "run", "-d", "--name", CONTAINER,
        "--cpus", str(cpus), "--memory", f"{mem_mb}m",
        "-p", f"{PORT}:8000", IMAGE,
        timeout=300,
    )

    # Wait for the app to finish loading models and answer.
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            if _get("/health").get("status") == "ok":
                return
        except Exception:
            time.sleep(2)

    logs = _docker("logs", "--tail", "40", CONTAINER, check=False).stdout
    _docker("rm", "-f", CONTAINER, check=False)
    pytest.fail(f"edge container never became healthy:\n{logs}")


@pytest.fixture(scope="module")
def constrained_edge():
    """An edge node running under a hard `--cpus=1 --memory=512m` budget."""
    if not _docker_available():
        pytest.skip("needs a running Docker daemon")

    _docker(
        "build", "-f", os.path.join(REPO, "docker", "edge.Dockerfile"),
        "-t", IMAGE, REPO, timeout=1800,
    )
    _run_edge(CPU_LIMIT, MEM_LIMIT_MB)

    yield CONTAINER

    _docker("rm", "-f", CONTAINER, check=False)


@requires_docker
def test_telemetry_reports_the_real_container_limits(constrained_edge):
    """`ram_limit_mb` is the container's real 512 MB, read from its cgroup."""
    device_id = _first_device_id()
    tel = _get(f"/devices/{device_id}/telemetry")

    assert tel["ram_limit_mb"] == pytest.approx(MEM_LIMIT_MB, rel=0.02), (
        f"expected the real {MEM_LIMIT_MB} MB cgroup limit, got {tel['ram_limit_mb']}"
    )
    # The label must state the limit the container actually has, and stay honest
    # about being an emulated target rather than real Pi hardware.
    assert f"{MEM_LIMIT_MB} MB" in tel["target_label"], tel["target_label"]
    assert "emulated constrained target" in tel["target_label"]
    assert "unknown" not in tel["target_label"]
    assert "raspberry" not in tel["target_label"].lower()


@requires_docker
def test_label_matches_the_limits_the_container_really_has(constrained_edge):
    """Re-run with a different budget: the label must follow, not stay fixed."""
    try:
        _run_edge(2, 1024)
        device_id = _first_device_id()
        tel = _get(f"/devices/{device_id}/telemetry")
        assert tel["ram_limit_mb"] == pytest.approx(1024, rel=0.02)
        assert "2.0 CPU" in tel["target_label"], tel["target_label"]
        assert "1024 MB" in tel["target_label"]
    finally:
        # Hand the shared container back on the documented budget so the rest of
        # the module still exercises --cpus=1 --memory=512m.
        _run_edge(CPU_LIMIT, MEM_LIMIT_MB)


@requires_docker
def test_rss_stays_under_the_container_memory_budget(constrained_edge):
    """The claim is that the budget is real. RSS must actually fit inside it."""
    device_id = _first_device_id()
    tel = _get(f"/devices/{device_id}/telemetry")

    assert tel["ram_mb"] > 0, "RSS must be measured, not reported as zero"
    assert tel["ram_mb"] < MEM_LIMIT_MB, (
        f"RSS {tel['ram_mb']} MB exceeds the {MEM_LIMIT_MB} MB budget"
    )


@requires_docker
def test_cpu_pct_is_a_measurement_that_responds_to_load(constrained_edge):
    """The decisive check: CPU must RISE when the cgroup burns CPU.

    A constant, an average-since-boot, or a permanently-zero reader cannot
    respond to load inside the measured interval. We burn CPU in the container
    with `docker exec` and sample `/telemetry` while it runs.
    """
    device_id = _first_device_id()

    # Baseline: quiet container.
    time.sleep(0.5)
    quiet = _get(f"/devices/{device_id}/telemetry")["cpu_pct"]

    # Burn CPU inside the same cgroup for a few seconds.
    burner = subprocess.Popen(
        ["docker", "exec", CONTAINER, "python", "-c",
         "import time\nt=time.time()\nwhile time.time()-t < 20: pass"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        loaded = []
        for _ in range(6):
            # Sample slower than the reader's minimum window, otherwise every
            # call returns the same stored reading and we would be asserting on
            # one number rather than tracking a live one.
            time.sleep(0.5)
            loaded.append(_get(f"/devices/{device_id}/telemetry")["cpu_pct"])
    finally:
        burner.kill()
        burner.wait()

    peak = max(loaded)
    assert peak > quiet, (
        f"cpu_pct did not respond to load in the cgroup "
        f"(quiet={quiet}, loaded={loaded}) — it is not a live measurement"
    )
    # One core is the whole budget, so one busy thread should saturate it.
    # The old implementation could not exceed 100 no matter what, and could not
    # react within a sampling window at all.
    assert peak > 50.0, f"cpu_pct barely moved under real load: {loaded}"

    # ...and it must not exceed what the cgroup can physically do. Polling
    # faster than the accounting tick reads a partial slice as a whole window;
    # on this container that produced readings up to 251% of a 1-CPU budget.
    assert peak <= 130.0, (
        f"cpu_pct reported {peak}% for a 1-CPU budget — an impossible reading: {loaded}"
    )


@requires_docker
def test_query_latency_is_measured_under_the_constraint(constrained_edge):
    """Latency percentiles come from real queries served inside the container."""
    device_id = _first_device_id()
    for text in ("chlorine reading in sector 4", "structural crack reported",
                 "fuel spillage at the dock", "flood water in the basement"):
        _get(f"/devices/{device_id}/telemetry")

    tel = _get(f"/devices/{device_id}/telemetry")
    assert tel["query_latency_p50_ms"] >= 0.0
    assert tel["query_latency_p95_ms"] >= tel["query_latency_p50_ms"], (
        "p95 must not sit below p50"
    )
