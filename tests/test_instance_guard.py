"""Tests for the per-palace mcp_server PID lockfile registry.

Covers the 2026-07-30 stray-instance incident class: a second mcp_server
against one palace must be fatal in embedded mode (exclusive owner),
warn-and-proceed in HTTP mode (N thin clients are the supported
topology), and stale lockfiles from dead holders must be reaped via the
process-liveness check rather than blocking startup forever.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mempalace.instance_guard import (
    InstanceRegistration,
    PalaceInstanceBusy,
    _registry_dir,
    register_instance,
)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point ~ at a temp dir so lockfiles never touch the real palace."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser
    return tmp_path


def _dead_pid() -> int:
    """Return a PID guaranteed dead: spawn a no-op child and reap it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _plant_holder(palace: str, pid: int, argv: str = "python -m mempalace.mcp_server") -> str:
    reg_dir = _registry_dir(palace)
    path = os.path.join(reg_dir, f"{pid}.pid")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(argv)
    return path


def test_first_instance_registers_and_releases(home, tmp_path):
    palace = str(tmp_path / "palace")
    reg = register_instance(palace, exclusive=True)
    assert os.path.exists(reg.path)
    assert os.path.basename(reg.path) == f"{os.getpid()}.pid"
    reg.release()
    assert not os.path.exists(reg.path)
    # release is idempotent
    reg.release()


def test_exclusive_contention_names_live_holder(home, tmp_path):
    palace = str(tmp_path / "palace")
    # PID 1 (launchd/init) is always alive and not ours — a live "holder".
    _plant_holder(palace, 1, argv="stray mempalace-mcp")
    with pytest.raises(PalaceInstanceBusy) as excinfo:
        register_instance(palace, exclusive=True)
    msg = str(excinfo.value)
    assert "PID 1" in msg
    assert "stray mempalace-mcp" in msg
    # The loser must not have left its own lockfile behind.
    own = os.path.join(_registry_dir(palace), f"{os.getpid()}.pid")
    assert not os.path.exists(own)


def test_stale_lockfile_is_reaped_and_acquire_succeeds(home, tmp_path):
    palace = str(tmp_path / "palace")
    stale = _plant_holder(palace, _dead_pid())
    reg = register_instance(palace, exclusive=True)
    assert not os.path.exists(stale)
    assert os.path.exists(reg.path)
    reg.release()


def test_http_mode_peer_warns_but_registers(home, tmp_path, caplog):
    palace = str(tmp_path / "palace")
    _plant_holder(palace, 1)
    with caplog.at_level("WARNING", logger="mempalace.instance_guard"):
        reg = register_instance(palace, exclusive=False)
    assert isinstance(reg, InstanceRegistration)
    assert os.path.exists(reg.path)
    assert any("PID 1" in r.getMessage() for r in caplog.records)
    reg.release()


def test_different_palaces_do_not_contend(home, tmp_path):
    _plant_holder(str(tmp_path / "palace_a"), 1)
    reg = register_instance(str(tmp_path / "palace_b"), exclusive=True)
    assert os.path.exists(reg.path)
    reg.release()


def test_palace_path_is_normalized(home, tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    _plant_holder(str(palace), 1)
    # A dressed-up spelling of the same path must hit the same registry.
    dressed = str(tmp_path / "." / "palace")
    with pytest.raises(PalaceInstanceBusy):
        register_instance(dressed, exclusive=True)


def test_non_pid_files_in_registry_are_ignored(home, tmp_path):
    palace = str(tmp_path / "palace")
    reg_dir = _registry_dir(palace)
    with open(os.path.join(reg_dir, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write("not a lockfile")
    reg = register_instance(palace, exclusive=True)
    assert os.path.exists(reg.path)
    reg.release()
