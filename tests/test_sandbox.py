"""The sandbox is the thing standing between a generated test and the user's
machine, so its construction is pinned rather than trusted.

The live behaviour these encode was verified against Docker on 2026-09-18:
network blocked for test code but available for dependency resolution, a
read-only root filesystem, uid 501 rather than root, no host filesystem
visible, and a 4GB allocation killed at the 2g limit (exit 137).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from crew_org.config import load_org
from crew_org.tools.sandbox import Mode, Sandbox


@pytest.fixture
def box() -> Sandbox:
    return Sandbox(mode=Mode.REQUIRED, engine="docker")


def argv(box: Sandbox, *, network: bool = False) -> list[str]:
    return box.command(Path("/tmp/wt"), ["uv", "run", "pytest"], network=network)


# --- what the container is given ----------------------------------------


def test_test_code_gets_no_network(box):
    a = argv(box, network=False)
    assert a[a.index("--network") + 1] == "none"


def test_only_dependency_resolution_gets_a_network(box):
    a = argv(box, network=True)
    assert a[a.index("--network") + 1] == "bridge"


def test_it_does_not_run_as_root(box):
    assert "--user" in argv(box)
    assert "0:0" not in argv(box)


def test_the_root_filesystem_is_read_only(box):
    assert "--read-only" in argv(box)


def test_all_capabilities_are_dropped(box):
    a = argv(box)
    assert a[a.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in a[a.index("--security-opt") + 1]


def test_resources_are_bounded(box):
    a = argv(box)
    for flag in ("--memory", "--pids-limit", "--cpus"):
        assert flag in a, f"{flag} is not set"
    # Swap must match memory, or the limit is trivially escaped.
    assert a[a.index("--memory") + 1] == a[a.index("--memory-swap") + 1]


def test_only_the_worktree_and_cache_are_mounted(box):
    mounts = [argv(box)[i + 1] for i, x in enumerate(argv(box)) if x == "-v"]
    assert any(m.endswith(":/work") for m in mounts)
    assert any(m.endswith(":/cache") for m in mounts)
    assert len(mounts) == 2


def test_the_cache_is_a_host_directory_not_a_named_volume(box):
    """A named volume is created root-owned, and the container runs as the
    invoking user, so uv could not write into one."""
    mounts = [argv(box)[i + 1] for i, x in enumerate(argv(box)) if x == "-v"]
    cache = next(m for m in mounts if m.endswith(":/cache"))
    assert cache.startswith("/"), f"{cache} is a named volume, not a path"


def test_the_container_is_removed_after_the_run(box):
    assert "--rm" in argv(box)


# --- refusing to run unprotected ----------------------------------------


def test_a_missing_engine_blocks_rather_than_falling_back(monkeypatch, box):
    """A silent fallback to the host is worse than no sandbox: it looks
    protected while running arbitrary code as the user."""
    monkeypatch.setattr("crew_org.tools.sandbox.shutil.which", lambda _: None)
    reason = box.unavailable_reason()
    assert reason and "not on PATH" in reason
    assert "'off'" in reason


def test_a_dead_daemon_is_reported(monkeypatch, box):
    monkeypatch.setattr("crew_org.tools.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no daemon")
    )
    assert "no daemon is answering" in (box.unavailable_reason() or "")


def test_a_hanging_daemon_does_not_hang_the_tick(monkeypatch, box):
    monkeypatch.setattr("crew_org.tools.sandbox.shutil.which", lambda _: "/usr/bin/docker")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=15)

    monkeypatch.setattr(subprocess, "run", hang)
    assert "did not respond" in (box.unavailable_reason() or "")


def test_mode_off_is_explicit_and_does_not_block():
    assert Sandbox(mode=Mode.OFF).unavailable_reason() is None


def test_the_shipped_config_requires_a_sandbox():
    assert Sandbox.from_config(load_org()).mode is Mode.REQUIRED
