"""Running untrusted code.

Everything the crew generates is untrusted: an LLM wrote it, and testing it
means executing it. The default is therefore a container with no network, no
capabilities, a non-root user and hard resource limits.

When no container engine is available the sandbox **fails** rather than falling
back to the host. A silent fallback is worse than no sandbox at all, because it
looks protected while running arbitrary code against the user's own account.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

IMAGE = "ghcr.io/astral-sh/uv:python3.12-bookworm-slim"

# A host directory rather than a named volume: a named volume is created
# root-owned, and the container runs as the invoking user, so uv cannot write
# its cache into one. This directory is created by us and therefore ours.
CACHE_DIR = Path("var/uv-cache")

DEFAULT_TIMEOUT = 300
PROBE_TIMEOUT = 15
MEMORY = "2g"
CPUS = "2"
PIDS = "512"


class Mode(StrEnum):
    REQUIRED = "required"
    """Container or nothing. The default."""

    OFF = "off"
    """Run on the host. Only ever an explicit, named choice."""


@dataclass(frozen=True)
class Sandbox:
    mode: Mode = Mode.REQUIRED
    engine: str = "docker"
    image: str = IMAGE

    @classmethod
    def from_config(cls, org: dict) -> Sandbox:
        section = org.get("sandbox") or {}
        return cls(
            mode=Mode(section.get("mode", Mode.REQUIRED)),
            engine=section.get("engine", "docker"),
            image=section.get("image", IMAGE),
        )

    @property
    def available(self) -> bool:
        return shutil.which(self.engine) is not None

    def unavailable_reason(self) -> str | None:
        """Why this sandbox cannot run, if it cannot."""
        if self.mode is Mode.OFF:
            return None
        if not self.available:
            return (
                f"{self.engine!r} is not on PATH, and the sandbox is {self.mode}. "
                "Generated code is not run on the host by default — start the engine, "
                "or set sandbox.mode to 'off' in org.yaml to accept that risk explicitly."
            )
        # `docker info` is slow enough on Docker Desktop to time out; asking for
        # the server version is the cheap way to learn whether a daemon answers.
        try:
            probe = subprocess.run(
                [self.engine, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"{self.engine} did not respond within {PROBE_TIMEOUT}s. Is the engine started?"
        if probe.returncode != 0:
            return f"{self.engine} is installed but no daemon is answering. Start it."
        return None

    @property
    def cache_dir(self) -> Path:
        path = CACHE_DIR.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def command(self, worktree: Path, inner: list[str], *, network: bool) -> list[str]:
        """Build the container invocation for one command."""
        return [
            self.engine,
            "run",
            "--rm",
            # No network for anything but dependency resolution.
            "--network",
            "bridge" if network else "none",
            # Files stay owned by the user, and nothing runs as root.
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            # The container filesystem is immutable; only the worktree and
            # /tmp are writable.
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,size=512m",
            "--memory",
            MEMORY,
            "--memory-swap",
            MEMORY,
            "--pids-limit",
            PIDS,
            "--cpus",
            CPUS,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{worktree.resolve()}:/work",
            "-v",
            f"{self.cache_dir}:/cache",
            "-w",
            "/work",
            "-e",
            "UV_CACHE_DIR=/cache",
            "-e",
            "HOME=/tmp",
            "-e",
            "XDG_CACHE_HOME=/cache",
            self.image,
            *inner,
        ]
