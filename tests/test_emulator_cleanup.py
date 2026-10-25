"""An emulator a run boots must stop when the run ends, however it ends."""
import subprocess
import sys
import time

import pytest

from codeandconfirm.coordinator import Coordinator, stop_owned_emulators
from codeandconfirm.device import android_adb
from codeandconfirm.device.base import DeviceError
from codeandconfirm.runstore import Run
from codeandconfirm.util import pid_alive


def _fake_emulator() -> subprocess.Popen:
    # Same launch shape as boot_emulator: its own session, so it outlives the parent unless someone stops it.
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)


def _gone(proc: subprocess.Popen) -> bool:
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return not pid_alive(proc.pid)


@pytest.fixture
def run_(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path / "home"))
    return Run("run-emu", create=True, repo_root=tmp_path)


def test_cleanup_stops_emulator_the_run_booted(run_):
    proc = _fake_emulator()
    try:
        run_.own("emulator", "emulator-5580", pid=proc.pid)
        run_.record_pid("emulator-5580", proc.pid)
        run_.phase_end("reserving", "ok")
        co = Coordinator(None, run_.dir, log=lambda s: None)
        co.run = run_
        co._cleanup()
        assert _gone(proc)
        r = Run(run_.id)
        assert "emulator-5580" not in r.state.owned.get("emulator", {})
        assert "emulator-5580" not in r.pids()
        # the device is gone, so a resume must reserve it again
        assert r.phase_status("reserving") == "pending"
    finally:
        proc.kill()


def test_emulator_the_run_did_not_boot_stays_up(run_):
    proc = _fake_emulator()
    try:
        run_.phase_end("reserving", "ok")
        assert stop_owned_emulators(run_) == []
        co = Coordinator(None, run_.dir, log=lambda s: None)
        co.run = run_
        co._cleanup()
        assert proc.poll() is None
        assert Run(run_.id).phase_status("reserving") == "ok"
    finally:
        proc.kill()


def test_failed_boot_stops_the_emulator(tmp_path, monkeypatch):
    procs = []
    real_popen = subprocess.Popen

    def popen(args, *a, **kw):
        if "-avd" not in args:  # leave every other subprocess (ps, kill checks) alone
            return real_popen(args, *a, **kw)
        procs.append(_fake_emulator())
        return procs[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(android_adb.AndroidEmulator, "available", lambda self: (False, "not booted"))
    monkeypatch.setattr(android_adb, "run", lambda *a, **kw: type("R", (), {"stdout": "0"})())
    with pytest.raises(DeviceError, match="did not finish booting"):
        android_adb.boot_emulator("avd", 5580, sdk=str(tmp_path), timeout_s=0.1)
    try:
        assert _gone(procs[0])
    finally:
        procs[0].kill()
