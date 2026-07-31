"""Per-palace MCP-server instance guard (PID lockfile registry).

Enforces the topology rule from ``docs/chroma-client-server.md``: in HTTP
mode the standalone Chroma server exclusively owns the palace files and
mcp_server processes are thin clients, N of which may serve one palace
concurrently (Claude Code per-session + Claude Desktop is the normal
steady state). The dangerous topology is an *embedded-mode* mcp_server
opening palace files another process may own — two writers on the same
sqlite/HNSW files is the corruption scenario Block 5 exists to remove,
and a stray second instance caused the 2026-07-30 chat-side outage.

Mechanism: a per-palace registry directory of PID lockfiles under
``~/.mempalace/locks/mcp_instances_<key>/``. Each server writes
``<pid>.pid`` (body: its argv) on startup and removes it at exit.
Startup scans the registry first: lockfiles whose PID is no longer alive
are reaped (stale-lock detection via process liveness), and live holders
either abort startup with an error naming them (embedded mode,
exclusive) or emit a warning naming them (HTTP mode, observability for
stray-instance triage).

The scan-then-register sequence is not atomic across two simultaneous
exclusive starts; the registry guards against the human-scale stray
process, not a microsecond race. PID reuse can make a stale lockfile
look live — the recorded argv is included in the error so the operator
can judge.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import sys

logger = logging.getLogger(__name__)


class PalaceInstanceBusy(RuntimeError):
    """Another live mcp_server already holds this palace in exclusive mode."""


def _palace_key(palace_path: str) -> str:
    """Normalized per-palace key — same recipe as ``palace.mine_palace_lock``."""
    resolved = os.path.realpath(os.path.expanduser(palace_path))
    return hashlib.sha256(os.path.normcase(resolved).encode()).hexdigest()[:16]


def _registry_dir(palace_path: str) -> str:
    d = os.path.join(
        os.path.expanduser("~"),
        ".mempalace",
        "locks",
        f"mcp_instances_{_palace_key(palace_path)}",
    )
    os.makedirs(d, exist_ok=True)
    return d


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe. Undeterminable → treat as alive (fail safe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _live_holders(reg_dir: str, self_pid: int) -> list[tuple[int, str]]:
    """Return live (pid, argv) peers; reap lockfiles of dead holders."""
    holders: list[tuple[int, str]] = []
    try:
        names = os.listdir(reg_dir)
    except OSError:
        return holders
    for name in sorted(names):
        stem, ext = os.path.splitext(name)
        if ext != ".pid" or not stem.isdigit():
            continue
        pid = int(stem)
        if pid == self_pid:
            continue
        path = os.path.join(reg_dir, name)
        if not _pid_alive(pid):
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                argv = fh.read().strip()
        except OSError:
            argv = ""
        holders.append((pid, argv))
    return holders


def _format_holders(holders: list[tuple[int, str]]) -> str:
    return ", ".join(
        f"PID {pid}" + (f" ({argv})" if argv else "") for pid, argv in holders
    )


class InstanceRegistration:
    """Handle for this process's PID lockfile; removed at exit."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._released = False
        atexit.register(self.release)

    @property
    def path(self) -> str:
        return self._path

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            os.unlink(self._path)
        except OSError:
            pass


def register_instance(palace_path: str, *, exclusive: bool) -> InstanceRegistration:
    """Register this mcp_server in the palace's PID registry.

    ``exclusive=True`` (embedded mode): raise :class:`PalaceInstanceBusy`
    naming the live holder(s) if any other instance already serves this
    palace. ``exclusive=False`` (HTTP mode): live peers are supported —
    log a warning naming them and register alongside.
    """
    reg_dir = _registry_dir(palace_path)
    pid = os.getpid()
    holders = _live_holders(reg_dir, pid)
    resolved = os.path.realpath(os.path.expanduser(palace_path))
    if holders:
        if exclusive:
            raise PalaceInstanceBusy(
                f"another mempalace-mcp instance already serves palace {resolved}: "
                f"{_format_holders(holders)}. Embedded mode allows exactly one "
                "server per palace (two writers corrupt the sqlite/HNSW files); "
                "stop the holding process or wait for it to exit, then retry."
            )
        logger.warning(
            "Another mempalace-mcp instance is serving palace %s: %s. "
            "Concurrent thin HTTP clients are a supported topology, but if "
            "palace tools go unresponsive, check for a stray instance "
            "(docs/chroma-client-server.md, operations runbook).",
            resolved,
            _format_holders(holders),
        )
    own_path = os.path.join(reg_dir, f"{pid}.pid")
    try:
        with open(own_path, "w", encoding="utf-8") as fh:
            fh.write(" ".join(sys.argv[:3]).strip())
    except OSError:
        # Registration is observability, not a correctness gate once the
        # exclusivity check above has passed — never block startup on it.
        logger.warning("Could not write instance lockfile %s", own_path, exc_info=True)
    return InstanceRegistration(own_path)
