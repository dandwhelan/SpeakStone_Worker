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


def test_a_job_waits_for_the_lock_to_stay_free(tmp_path, monkeypatch):
    """A watcher drops and retakes its lock in bursts: no job starts until the lock has been free for a while."""
    nw = load_worker()
    from test_net_worker_core import make_worker
    w, _ = make_worker(tmp_path, monkeypatch)
    w.cfg.need_free_mib = 0
    lock = tmp_path / "watcher.lock"
    w.cfg.yield_lock = str(lock)
    t = {"now": 1000.0}
    w.clock = lambda: t["now"]
    slept = []

    def sleep(s, ctx=None):
        slept.append(s)
        t["now"] += s
        if t["now"] >= 1040:                      # the burst ends 40 s in
            lock.unlink(missing_ok=True)
    w.sleep_impl = sleep
    monkeypatch.setattr(nw, "_pid_started", lambda pid: None)
    lock.write_text(str(os.getppid()), encoding="utf-8")
    w.wait_for_vram()
    # last seen held at the check before the burst ended (1020): the settle time runs from there
    assert t["now"] >= 1020 + nw.LOCK_SETTLE_SECONDS
    assert t["now"] < 1040 + nw.LOCK_SETTLE_SECONDS + nw.YIELD_POLL_SECONDS
    events = [ln for f in (tmp_path / "logs").glob("worker_*.log") for ln in f.read_text().splitlines()]
    assert any("reason=yield" in e and "settle=" in e for e in events)


def test_a_lock_never_seen_held_costs_no_wait(tmp_path, monkeypatch):
    nw = load_worker()
    from test_net_worker_core import make_worker
    w, _ = make_worker(tmp_path, monkeypatch)
    w.cfg.need_free_mib = 0
    w.cfg.yield_lock = str(tmp_path / "absent.lock")
    slept = []
    w.sleep_impl = lambda s, ctx=None: slept.append(s)
    w.wait_for_vram()
    assert slept == [] and w.lock_settle_left() == 0
