"""Global pytest fixtures for Aegis Edge tests.

Key fixture: `clean_shards` — automatically wipes the `./shards/` directory
created during test runs so disk space doesn't accumulate between invocations.
Tests that write real Qdrant Edge shards to disk can exhaust the drive if these
directories are left behind.  The fixture runs at session scope (once per `pytest`
invocation, after all tests finish) to give individual tests access to their data
during the run while still cleaning up on exit.
"""

import os
import shutil
import tempfile
import pytest


@pytest.fixture(autouse=True, scope="session")
def clean_shards():
    """Delete ./shards after the test session to reclaim disk space."""
    yield
    shard_path = os.path.join(os.getcwd(), "shards")
    if os.path.isdir(shard_path):
        shutil.rmtree(shard_path, ignore_errors=True)

    # Also purge hub_snapshot_* and edge_* dirs from %TEMP%
    tmpdir = tempfile.gettempdir()
    for name in os.listdir(tmpdir):
        if name.startswith(("hub_snapshot_", "edge_snapshot_", "edge_shard_")):
            full = os.path.join(tmpdir, name)
            shutil.rmtree(full, ignore_errors=True)
