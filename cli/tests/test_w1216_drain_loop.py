"""A "finish and leave" request ends the wait loop it interrupts instead of spinning it."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_worker():
    spec = importlib.util.spec_from_file_location("net_worker_drainloop", ROOT / "net_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def fake_worker(nw, drain):
    w = types.SimpleNamespace()
    w.events = []
    w.log = types.SimpleNamespace(event=lambda *a, **k: w.events.append((a, k)))
    w.sleep_impl = lambda seconds, ctx=None: None          # a wait that a drain cut short returns at once
    w._stop_now = lambda ctx=None: False
    w.drain_requested = lambda: drain["on"]
    w.sleep = lambda seconds, ctx=None: nw.Worker.sleep(w, seconds, ctx)
    w.yield_busy = lambda force=False: False
    w.cfg = types.SimpleNamespace(need_free_mib=5200)
    w.skip_vram_wait = False
    w.engine_alive = lambda: False
    w.gpu_query = lambda: {"vram_free_mib": 100}           # another program holds the card's memory
    w.presence = lambda: None
    return w


def test_drain_while_waiting_for_graphics_memory_leaves_after_one_look():
    nw = load_worker()
    w = fake_worker(nw, {"on": True})
    with pytest.raises(nw.Drained):
        nw.Worker._wait_for_vram(w)
    assert len(w.events) == 1                               # one idle line, not twenty a second


def test_without_a_drain_the_wait_goes_on_normally():
    nw = load_worker()
    w = fake_worker(nw, {"on": False})
    looks = {"n": 0}

    def gpu():
        looks["n"] += 1
        return {"vram_free_mib": 100 if looks["n"] < 3 else 9000}
    w.gpu_query = gpu
    nw.Worker._wait_for_vram(w)                             # returns when the memory is free
    assert len(w.events) == 2


def test_a_wait_inside_a_job_is_never_cut_by_a_drain():
    nw = load_worker()
    w = fake_worker(nw, {"on": True})
    ctx = types.SimpleNamespace(check=lambda: None, in_grace=lambda: False)
    nw.Worker.sleep(w, 1, ctx)                              # no Drained: the job in hand is finished first
