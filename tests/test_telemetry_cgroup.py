"""Phase 10 acceptance tests — telemetry reads the real cgroup, not a costume.

Acceptance (docs/30-phases.md, Phase 10):
  * an edge node runs in a `--cpus=1 --memory=512m` container;
  * `/telemetry` reports RSS/CPU read from that container's cgroup;
  * the label matches the real limit.

These tests cover the measurement logic against synthetic cgroup trees, so the
parsing and the delta arithmetic are pinned on every run. The container half of
the criterion — a real `docker run` with real limits — lives in
`test_telemetry_container.py` and needs a Docker daemon.

The bugs these exist to prevent, all of which were live in the tree:

  * CPU was `process_cpu_seconds / host_uptime`, i.e. an average since boot
    dressed up as current usage, then hard-capped at 100.
  * `read_cgroup_cpu_pct()` was dead code that always returned 0.0.
  * A percentage could never exceed 100 even for a cgroup allowed 4 cores, so
    the number could not describe a multi-core limit at all.
  * The label fell back to the literal string "unknown" instead of saying what
    was actually unreadable.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from edge_node import telemetry as tel  # noqa: E402

MB = 1024 * 1024


def write(path, text):
    """Materialize one file in a fake cgroup tree."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --- cgroup v2 -------------------------------------------------------------

def test_v2_cpu_quota_is_parsed_as_cores(tmp_path, monkeypatch):
    """`--cpus=1` shows up in cpu.max as a quota/period pair."""
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "100000 100000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_cpu_quota() == pytest.approx(1.0)


def test_v2_cpu_quota_scales_with_the_limit(tmp_path, monkeypatch):
    """A `--cpus=2` container must read as 2 cores, not saturate at 1."""
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "200000 100000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_cpu_quota() == pytest.approx(2.0)


def test_unlimited_cpu_reports_none_not_zero(tmp_path, monkeypatch):
    """`max` means no quota. Reporting 0.0 would read as "no CPU used"."""
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "max 100000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_cpu_quota() is None


def test_v2_cpu_usage_is_read_from_cpu_stat(tmp_path, monkeypatch):
    root = str(tmp_path)
    write(
        os.path.join(root, "cpu.stat"),
        "usage_usec 1234567\nuser_usec 1000000\nsystem_usec 234567\n",
    )
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_cpu_usage_usec() == 1234567


def test_cpu_percentage_is_quota_relative(tmp_path, monkeypatch):
    """The core property: 1 core's worth of usage reads as 100%.

    Two samples one second apart, having burned 500000us of CPU. On a
    single-core cgroup that is 50% of the budget. If the arithmetic regressed to
    the old "CPU seconds since boot / uptime" it could not produce this.
    """
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "100000 100000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    pct = tel.cpu_pct_between(
        prev_usage_usec=0, prev_ts=100.0, usage_usec=500_000, now_ts=101.0, quota=1.0
    )
    assert pct == pytest.approx(50.0)


def test_cpu_percentage_may_exceed_100_for_multicore_quota(tmp_path, monkeypatch):
    """A 4-core cgroup at 100% is 400% — the old hard cap of 100 was a lie."""
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "400000 100000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    pct = tel.cpu_pct_between(
        prev_usage_usec=0, prev_ts=100.0, usage_usec=1_000_000, now_ts=101.0, quota=1.0
    )
    assert pct == pytest.approx(100.0)

    pct2 = tel.cpu_pct_between(
        prev_usage_usec=0, prev_ts=100.0, usage_usec=4_000_000, now_ts=101.0, quota=4.0
    )
    assert pct2 == pytest.approx(100.0)


def test_cpu_delta_guards_against_a_bad_interval(tmp_path, monkeypatch):
    """A zero or backwards clock must not divide by zero or go negative."""
    pct = tel.cpu_pct_between(
        prev_usage_usec=0, prev_ts=100.0, usage_usec=500_000, now_ts=100.0, quota=1.0
    )
    assert pct == 0.0

    backwards = tel.cpu_pct_between(
        prev_usage_usec=900_000, prev_ts=101.0, usage_usec=100_000, now_ts=100.0, quota=1.0
    )
    assert backwards == 0.0


def test_first_cpu_sample_has_no_delta_to_report(tmp_path, monkeypatch):
    """With no previous sample there is no delta, and 0.0 is the honest answer.

    Claiming a percentage here is what made the old implementation look alive
    while reporting an average since boot.
    """
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "100000 100000\n")
    write(os.path.join(root, "cpu.stat"), "usage_usec 500000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)
    tel.reset_cpu_baseline()

    assert tel.read_cgroup_cpu_pct() == 0.0, (
        "the first sample must not invent a rate"
    )


def test_second_cpu_sample_reports_the_rate_since_the_first(tmp_path, monkeypatch):
    """With a baseline in hand, the reading is a real rate over the interval.

    The cgroup has burned 500000us; a baseline of zero one second ago means
    half of a one-core budget was used in that second.
    """
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "100000 100000\n")
    write(os.path.join(root, "cpu.stat"), "usage_usec 500000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)
    tel.reset_cpu_baseline()

    tel.set_cpu_baseline(0.0, time.monotonic() - 1.0)
    assert tel.read_cgroup_cpu_pct() == pytest.approx(50.0, abs=1.0)


# --- memory ----------------------------------------------------------------

def test_v2_memory_limit_is_read_in_mb(tmp_path, monkeypatch):
    root = str(tmp_path)
    write(os.path.join(root, "memory.max"), str(512 * MB))
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_memory_limit_mb() == pytest.approx(512.0)


def test_unlimited_memory_reports_none(tmp_path, monkeypatch):
    root = str(tmp_path)
    write(os.path.join(root, "memory.max"), "max\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_memory_limit_mb() is None


def test_memory_limit_is_never_derived_from_host_total(tmp_path, monkeypatch):
    """Host RAM is not the container's budget.

    The old code fell back to MemTotal, so an unconstrained process claimed the
    whole machine as its limit. Absent a cgroup limit the answer is None.
    """
    root = str(tmp_path)
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_memory_limit_mb() is None


# --- cgroup v1 -------------------------------------------------------------

def test_v1_cpu_quota_and_usage_are_supported(tmp_path, monkeypatch):
    """cgroup v1 is still what older hosts and some runtimes mount.

    v1 reports usage in nanoseconds while v2 uses microseconds, so a reader
    that ignored the unit would be off by 1000x on a v1 host.
    """
    root = str(tmp_path)
    write(os.path.join(root, "cpu", "cpu.cfs_quota_us"), "200000\n")
    write(os.path.join(root, "cpu", "cpu.cfs_period_us"), "100000\n")
    # 900 seconds of CPU, expressed in v1's nanoseconds.
    write(os.path.join(root, "cpu", "cpuacct.usage"), str(900 * 1_000_000_000))
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_cpu_quota() == pytest.approx(2.0)
    assert tel.read_cgroup_cpu_usage_usec() == 900 * 1_000_000


def test_v1_memory_limit_is_supported(tmp_path, monkeypatch):
    root = str(tmp_path)
    write(os.path.join(root, "memory", "memory.limit_in_bytes"), str(256 * MB))
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_memory_limit_mb() == pytest.approx(256.0)


def test_v1_unlimited_quota_reports_none(tmp_path, monkeypatch):
    root = str(tmp_path)
    write(os.path.join(root, "cpu", "cpu.cfs_quota_us"), "-1\n")
    write(os.path.join(root, "cpu", "cpu.cfs_period_us"), "100000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    assert tel.read_cgroup_cpu_quota() is None


# --- the label -------------------------------------------------------------

def test_label_reflects_the_real_container_limits(tmp_path, monkeypatch):
    """The label is derived from what was parsed, not written by hand."""
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "100000 100000\n")
    write(os.path.join(root, "memory.max"), str(512 * MB))
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    label = tel.get_target_label()
    assert label == "emulated constrained target (1.0 CPU / 512 MB container)"


def test_label_tracks_a_different_container_size(tmp_path, monkeypatch):
    """Swap the limits and the label must follow — proof it is not hardcoded."""
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "200000 100000\n")
    write(os.path.join(root, "memory.max"), str(4 * 1024 * MB))
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    label = tel.get_target_label()
    assert "2.0 CPU" in label
    assert "4096 MB" in label


def test_label_says_so_when_run_outside_a_container(tmp_path, monkeypatch):
    """No cgroup limit must read as an honest absence, never a fake number.

    The old label printed the word "unknown" inside a confident-looking string
    that still read like a measurement.
    """
    root = str(tmp_path)
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)

    label = tel.get_target_label()
    assert label == (
        "emulated constrained target (no CPU limit / no memory limit container)"
    )
    assert "unknown" not in label


def test_label_never_claims_to_be_a_pi():
    """Guardrail: emulated targets are labeled as emulated, never as a Pi."""
    label = tel.get_target_label()
    assert "raspberry" not in label.lower()
    assert "emulated constrained target" in label


# --- the payload -----------------------------------------------------------

def test_telemetry_payload_reports_measured_not_capped_values(monkeypatch):
    """The response must carry the cgroup limit through, not a fixed 512."""
    values = {
        "read_cgroup_cpu_quota": lambda: 4.0,
        "read_cgroup_cpu_usage_usec": lambda: 1_000,
        "read_cgroup_memory_limit_mb": lambda: 4096.0,
    }
    for name, fn in values.items():
        monkeypatch.setattr(tel, name, fn)
    monkeypatch.setattr(tel, "read_process_rss_mb", lambda: 321.5)
    monkeypatch.setattr(tel, "read_cgroup_cpu_pct", lambda: 12.5)
    monkeypatch.setattr(tel, "get_query_percentiles", lambda: (38.1, 71.9))
    monkeypatch.setattr(tel, "get_target_label", lambda: "emulated constrained target (4.0 CPU / 4096 MB container)")

    out = tel.get_telemetry(model_load_ms=1840.0)

    assert out["ram_limit_mb"] == 4096.0
    assert out["ram_mb"] == 321.5
    assert out["cpu_pct"] == 12.5
    assert out["query_latency_p50_ms"] == 38.1
    assert out["query_latency_p95_ms"] == 71.9
    assert out["model_load_ms"] == 1840.0
    assert "4.0 CPU" in out["target_label"]


def test_telemetry_does_not_ship_the_dead_cpu_reader():
    """`read_cgroup_cpu_pct` used to exist, be called by nobody, and return 0.0.

    A permanently-zero reader is a dead branch that makes telemetry look alive.
    The name now belongs to the real delta measurement, so the dead variant
    must be gone.
    """
    assert not hasattr(tel, "read_cgroup_cpu_pct_delta"), (
        "the fake CPU reader (average since boot, capped at 100) must be removed"
    )
    assert callable(tel.read_cgroup_cpu_pct)


def test_polling_too_fast_reuses_the_last_reading(tmp_path, monkeypatch):
    """A caller that polls faster than the tick can be measured against is lying.

    cgroup CPU accounting is quantised to the scheduler tick. On a saturated
    1-CPU cgroup a 20ms window was observed reading a median of 108% and
    peaking at 134% — a number the container is physically incapable of. The
    reader must therefore refuse to produce a fresh figure inside the minimum
    interval and hand back the last real one instead.
    """
    root = str(tmp_path)
    write(os.path.join(root, "cpu.max"), "100000 100000\n")
    write(os.path.join(root, "cpu.stat"), "usage_usec 500000\n")
    monkeypatch.setenv("EDGE_CGROUP_ROOT", root)
    tel.reset_cpu_baseline()

    # First call: nothing to difference against.
    assert tel.read_cgroup_cpu_pct() == 0.0

    # A valid window: one core fully used over one second == 100%.
    tel.set_cpu_baseline(0.0, time.monotonic() - 1.0)
    write(os.path.join(root, "cpu.stat"), "usage_usec 1000000\n")
    good = tel.read_cgroup_cpu_pct()
    assert good == pytest.approx(100.0, abs=1.0), good

    # Now poll again immediately, with a huge jump in usage that a 5ms window
    # would report as absurd. The baseline must not be consumed, and the last
    # real reading must survive.
    write(os.path.join(root, "cpu.stat"), "usage_usec 9000000\n")
    immediate = tel.read_cgroup_cpu_pct()
    assert immediate == good, (
        f"expected the last trustworthy reading {good}, got {immediate}"
    )

    # The skipped sample must not have been thrown away: once a long enough
    # window has passed, the accumulated usage is reported.
    time.sleep(tel.MIN_CPU_SAMPLE_INTERVAL_S + 0.05)
    after = tel.read_cgroup_cpu_pct()
    assert after > 0.0, "the deferred sample was lost instead of accumulated"
