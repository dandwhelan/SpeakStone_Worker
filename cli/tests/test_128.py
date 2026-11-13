"""1.2.8 robustness fixes: stall watchdog, error-mode, update retry, proven tool download, trimming, one version."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_net_worker_core import JOB1, FakeApi, job, make_ctx, make_worker, nw, events  # noqa: E402,F401


# ------------------------------------------------------------------ 1. stall watchdog
def test_stall_seconds_default_override_and_floor():
    assert nw.stall_seconds({}) == 900
    assert nw.stall_seconds({"SSW_STALL_SECONDS": "600"}) == 600
    assert nw.stall_seconds({"SSW_STALL_SECONDS": "5"}) == 120          # a typo must not kill a model load
    assert nw.stall_seconds({"SSW_STALL_SECONDS": "banana"}) == 900


def test_frozen_engine_is_killed_logged_and_job_fails_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(nw, "STALL_MIN_SECONDS", 1)
    monkeypatch.setenv("SSW_STALL_SECONDS", "2")
    api = FakeApi()
    w, state = make_worker(tmp_path, monkeypatch, api, mode="hang")
    ctx = make_ctx(w, JOB1)
    t0 = time.time()
    with pytest.raises(nw.JobFail) as ei:
        w.run_synth(ctx)
    assert ei.value.reason == "stalled" and ei.value.retryable is True
    assert time.time() - t0 < 30
    assert not w.engine_alive() and ctx.proc is None and ctx.stall_limit is None
    ev = events(tmp_path, "stall")
    assert len(ev) == 1 and f"job={JOB1}" in ev[0] and "seconds=" in ev[0]


def test_stalled_job_is_reported_to_the_site_as_stalled(tmp_path, monkeypatch):
    monkeypatch.setattr(nw, "STALL_MIN_SECONDS", 1)
    monkeypatch.setenv("SSW_STALL_SECONDS", "2")
    api = FakeApi()
    w, state = make_worker(tmp_path, monkeypatch, api, mode="hang")
    w.handle_job(job(JOB1))
    fails = [b for r, b in api.calls if r == "fail"]
    assert [f["reason"] for f in fails] == ["stalled"] and fails[0]["retryable"] is True
    assert not w.engine_alive()


def test_progress_resets_the_clock(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    ctx = make_ctx(w, JOB1)
    now = [1000.0]
    ctx.clock = lambda: now[0]
    ctx.stall_begin(100)
    now[0] += 99
    assert ctx.stalled_for() is None
    (ctx.out / "L000.ogg").write_bytes(b"OggS")                        # a new clip is progress
    now[0] += 50
    assert ctx.stalled_for() is None
    with open(ctx.dir / "worker.log", "ab") as f:                      # so is a new line of engine output
        f.write(b"loading\n")
    now[0] += 99
    assert ctx.stalled_for() is None
    now[0] += 101
    assert ctx.stalled_for() == 101
    ctx.stall_end()
    assert ctx.stalled_for() is None


def test_marker_counter_counts_as_progress(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    ctx = make_ctx(w, JOB1)
    now, lines = [0.0], [0]
    ctx.clock = lambda: now[0]
    ctx.stall_begin(10, lambda: lines[0])
    now[0] += 9
    lines[0] += 1
    assert ctx.stalled_for() is None
    now[0] += 9
    assert ctx.stalled_for() is None
    now[0] += 3
    assert ctx.stalled_for() is not None
