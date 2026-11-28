"""A lock that says no_gpu is not a reason to step aside."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_worker():
    spec = importlib.util.spec_from_file_location("net_worker_yield", ROOT / "net_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_lock_with_a_live_pid_is_held_but_not_when_it_says_no_gpu(tmp_path):
    nw = load_worker()
    lock = tmp_path / "busy.lock"
    assert nw.lock_held(lock) is False                      # no file
    lock.write_text(str(os.getppid()), encoding="utf-8")   # the test runner's parent: alive, started before the file
    assert nw.lock_held(lock) is True
    lock.write_text(f"no_gpu ship {os.getppid()}", encoding="utf-8")
    assert nw.lock_held(lock) is False
    lock.write_text("held by a test", encoding="utf-8")     # no pid: held while fresh
    assert nw.lock_held(lock) is True
    lock.write_text("no_gpu packing", encoding="utf-8")
    assert nw.lock_held(lock) is False


def test_a_reused_pid_does_not_hold_the_lock(tmp_path, monkeypatch):
    """A lock left by a crashed run names a PID Windows has since given to another program."""
    nw = load_worker()
    lock = tmp_path / "stale.lock"
    lock.write_text("run_batch 4242", encoding="utf-8")
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))                                  # written an hour ago
    monkeypatch.setattr(nw, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(nw, "_pid_started", lambda pid: old + 1800)   # that PID's process started after the file
    assert nw.lock_held(lock) is False
    monkeypatch.setattr(nw, "_pid_started", lambda pid: old - 60)     # started before it: it is the holder
    assert nw.lock_held(lock) is True
    monkeypatch.setattr(nw, "_pid_started", lambda pid: None)         # cannot tell: a live PID still counts
    assert nw.lock_held(lock) is True


def test_the_worker_never_yields_to_itself(tmp_path):
    nw = load_worker()
    lock = tmp_path / "self.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")
    assert nw.lock_held(lock) is False


def test_start_time_of_this_process_is_read(tmp_path):
    nw = load_worker()
    import time
    started = nw._pid_started(os.getpid())
    if sys.platform.startswith("linux") or os.name == "nt":
        assert started is not None and started <= time.time() + 1 and time.time() - started < 7 * 86400
    assert nw._pid_started(999999999) is None
