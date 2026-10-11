"""C4 — repo & Docker hygiene (P2-5).

These assertions encode the hygiene contract the C4 task sets, so a regression
(dropping the HEALTHCHECK, un-pinning a dependency, re-introducing a root user,
or letting the stale `hub` bytecode creep back) fails the suite loudly instead
of silently shipping a worse image.

They are plain file-content checks: no Docker daemon required, so they run in
the normal offline suite.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), "r", encoding="utf-8") as fh:
        return fh.read()


def test_gateway_dockerfile_runs_as_non_root():
    """The gateway image must drop to an unprivileged user before CMD."""
    dockerfile = _read(os.path.join("cloud-gateway", "Dockerfile"))
    user_lines = [
        ln.strip()
        for ln in dockerfile.splitlines()
        if ln.strip().upper().startswith("USER ")
    ]
    assert user_lines, "cloud-gateway/Dockerfile has no USER directive"
    # The effective (last) USER must not be root / uid 0.
    last_user = user_lines[-1].split(None, 1)[1].strip()
    assert last_user not in ("root", "0"), f"gateway runs as {last_user!r}"


def test_gateway_dockerfile_has_healthcheck():
    dockerfile = _read(os.path.join("cloud-gateway", "Dockerfile"))
    assert "HEALTHCHECK" in dockerfile, "cloud-gateway/Dockerfile has no HEALTHCHECK"
    # The probe should target the gateway's own /health endpoint.
    assert "/health" in dockerfile


def test_gateway_dockerfile_copies_every_gateway_module():
    """Every cloud-gateway/*.py must be COPY'd into the image.

    A sibling imported by app.py but not copied (evidence.py, added in D10) makes
    the container exit at import with ModuleNotFoundError — the gateway crash-
    loops and every edge push fails to resolve it. Guard the whole class.
    """
    dockerfile = _read(os.path.join("cloud-gateway", "Dockerfile"))
    gw_dir = os.path.join(REPO_ROOT, "cloud-gateway")
    modules = sorted(f for f in os.listdir(gw_dir) if f.endswith(".py"))
    assert modules, "no cloud-gateway modules found"
    for mod in modules:
        assert f"cloud-gateway/{mod}" in dockerfile, (
            f"cloud-gateway/{mod} is imported by app.py but not COPY'd in "
            f"cloud-gateway/Dockerfile"
        )


def test_dockerignore_exists_and_excludes_noise():
    content = _read(".dockerignore")
    for needle in ("__pycache__", ".venv", ".git", ".env"):
        assert needle in content, f".dockerignore does not exclude {needle!r}"


def test_root_requirements_fully_pinned():
    """Every requirement line must be an exact (==) pin for reproducible installs."""
    content = _read("requirements.txt")
    reqs = [
        ln.strip()
        for ln in content.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert reqs, "requirements.txt has no requirement lines"
    for req in reqs:
        assert "==" in req, f"requirement is not pinned: {req!r}"
        assert not re.search(r">=|<=|~=|>|<", req.split("==")[0] + req.split("==")[1]), (
            f"requirement has a non-exact specifier: {req!r}"
        )
    joined = "\n".join(reqs)
    # The two invariant pins must be preserved exactly.
    assert "qdrant-edge-py==0.8.0" in joined
    assert "qdrant-client==1.12.1" in joined


def test_no_stale_hub_bytecode():
    """`hub.py` was removed; its compiled bytecode must not linger."""
    pkg = os.path.join(REPO_ROOT, "src", "edge_node")
    hub_source = os.path.join(pkg, "hub.py")
    cache = os.path.join(pkg, "__pycache__")
    if os.path.exists(hub_source):
        return  # if the module is ever re-introduced this check is moot
    if not os.path.isdir(cache):
        return
    stale = [name for name in os.listdir(cache) if name.startswith("hub.")]
    assert not stale, f"stale hub bytecode present: {stale}"
