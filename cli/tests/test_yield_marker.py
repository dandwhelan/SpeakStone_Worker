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
    lock.write_text(str(os.getpid()), encoding="utf-8")
    assert nw.lock_held(lock) is True
    lock.write_text(f"no_gpu ship {os.getpid()}", encoding="utf-8")
    assert nw.lock_held(lock) is False
    lock.write_text("held by a test", encoding="utf-8")     # no pid: held while fresh
    assert nw.lock_held(lock) is True
    lock.write_text("no_gpu packing", encoding="utf-8")
    assert nw.lock_held(lock) is False
