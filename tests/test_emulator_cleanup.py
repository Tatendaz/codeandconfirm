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


def _fake_emulator(port: int = 5580) -> subprocess.Popen:
    # Same launch shape as boot_emulator: its own session, so it outlives the parent unless someone stops it, and
    # the `-avd … -port <port>` arguments cleanup checks before it kills a pid.
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "-avd", "test", "-port", str(port)],
                            start_new_session=True)


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
        procs.append(real_popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True))
        return procs[-1]

    (tmp_path / "platform-tools").mkdir()
    (tmp_path / "platform-tools" / "adb").touch()  # sdk_root needs this marker
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(android_adb.AndroidEmulator, "available", lambda self: (False, "not booted"))
    monkeypatch.setattr(android_adb, "run", lambda *a, **kw: type("R", (), {"stdout": "0"})())
    with pytest.raises(DeviceError, match="did not finish booting"):
        android_adb.boot_emulator("avd", 5580, sdk=str(tmp_path), timeout_s=0.1)
    try:
        assert _gone(procs[0])
    finally:
        procs[0].kill()


def test_cleanup_never_kills_a_process_that_reused_the_pid(run_):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:
        run_.own("emulator", "emulator-5580", pid=other.pid)   # the emulator exited and its pid was reused
        run_.phase_end("reserving", "ok")
        co = Coordinator(None, run_.dir, log=lambda s: None)
        co.run = run_
        co._cleanup()
        assert other.poll() is None
        r = Run(run_.id)
        assert "emulator-5580" not in r.state.owned.get("emulator", {})
        assert r.phase_status("reserving") == "pending"   # the owned device is gone either way
    finally:
        other.kill()


def test_cleanup_skips_an_emulator_on_another_port(run_):
    proc = _fake_emulator(port=5582)
    try:
        assert stop_owned_emulators(run_) == []
        run_.own("emulator", "emulator-5580", pid=proc.pid)
        assert stop_owned_emulators(run_) == []
        assert proc.poll() is None
    finally:
        proc.kill()


def test_cancel_never_kills_a_process_that_reused_the_emulator_pid(run_):
    from codeandconfirm.coordinator import cancel_run
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:
        run_.own("emulator", "emulator-5580", pid=other.pid)
        run_.record_pid("emulator-5580", other.pid)
        run_.state.coordinator_pid = None
        run_.save()
        res = cancel_run(run_.id, log=lambda s: None)
        assert other.poll() is None
        assert not any(k.startswith("emulator") for k in res["killed"])
    finally:
        other.kill()


def test_cancel_stops_the_emulator_before_it_releases_the_device_lock(run_, monkeypatch):
    from codeandconfirm import coordinator
    proc = _fake_emulator()
    seen = []
    real_release = coordinator.release_all_for

    def release(run_id):
        seen.append(proc.poll() is not None or not pid_alive(proc.pid))
        return real_release(run_id)

    monkeypatch.setattr(coordinator, "release_all_for", release)
    try:
        run_.own("emulator", "emulator-5580", pid=proc.pid)
        run_.state.coordinator_pid = None
        run_.save()
        coordinator.cancel_run(run_.id, log=lambda s: None)
        assert seen == [True]
    finally:
        proc.kill()


def test_a_different_start_time_means_a_different_process(run_):
    proc = _fake_emulator()
    try:
        assert android_adb.is_emulator_process(proc.pid, "emulator-5580", android_adb.process_start(proc.pid))
        run_.own("emulator", "emulator-5580", pid=proc.pid, started="Thu Jan  1 00:00:00 1970")
        assert stop_owned_emulators(run_) == []
        assert proc.poll() is None
    finally:
        proc.kill()


def test_cleanup_goes_on_when_the_backend_cannot_be_stopped(run_, monkeypatch):
    from codeandconfirm import coordinator

    def stop_backend(*a, **kw):
        raise PermissionError("pid owned by another user")

    monkeypatch.setattr(coordinator, "stop_backend", stop_backend)
    proc = _fake_emulator()
    try:
        run_.own("emulator", "emulator-5580", pid=proc.pid)
        co = Coordinator(None, run_.dir, log=lambda s: None)
        co.run = run_
        co._cleanup()
        assert _gone(proc)
    finally:
        proc.kill()


def test_cancel_marks_the_device_gone_so_resume_reserves_again(run_):
    from codeandconfirm.coordinator import cancel_run
    proc = _fake_emulator()
    try:
        run_.own("emulator", "emulator-5580", pid=proc.pid)
        run_.phase_end("reserving", "ok")
        run_.state.coordinator_pid = None
        run_.save()
        cancel_run(run_.id, log=lambda s: None)
        assert _gone(proc)
        assert Run(run_.id).phase_status("reserving") == "pending"
    finally:
        proc.kill()
