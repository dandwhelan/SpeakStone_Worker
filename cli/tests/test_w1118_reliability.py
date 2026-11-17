"""2026-11-18 reliability fixes: site/network failures and out-of-memory do not trip the brake; the CLI latch is lifted
once after 6 hours; a free GPU keeps the idle engine 30 minutes; synth words per hour is logged, not sent."""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_net_worker_core import JOB1, FakeApi, events, job, make_worker, nw, puts  # noqa: E402,F401
from test_w1216_brake import failing  # noqa: E402
from test_a1216_restart import make_group, tick  # noqa: E402
from speakstone_worker import cli  # noqa: E402


# ---- 1. site / network faults are not the PC's
@pytest.mark.parametrize("reason", ["upload_failed", "ref_download", "net_error"])
def test_site_faults_never_stop_the_worker_and_back_off(tmp_path, monkeypatch, reason):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    waits = [failing(w, reason) for _ in range(12)]          # far past FAILED_JOBS_LIMIT
    assert getattr(w, "_failed_jobs", 0) == 0 and not (w.work / nw.BROKEN_FILE).exists()
    assert waits[:7] == [5, 10, 20, 40, 80, 160, 300] and waits[-1] == nw.BACKOFF_CAP


def test_complete_5xx_is_a_site_fault_but_a_422_is_not(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    for _ in range(8):
        w.brake(nw.JobFail("other", "x", False, True, site_fault=True))
    assert getattr(w, "_failed_jobs", 0) == 0
    w.brake(nw.JobFail("other", "x", False, True, site_fault=False))
    assert w._failed_jobs == 1


def test_a_net_error_escaping_the_job_is_a_site_fault(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    w.sleep_impl = lambda s, ctx=None: None

    def work(ctx, j):
        raise nw.NetError("timed out")
    w._work = work
    for _ in range(8):
        w.handle_job(job(JOB1))
    assert getattr(w, "_failed_jobs", 0) == 0
    assert w.api.calls[-1][0] == "fail" and w.api.calls[-1][1]["reason"] == "net_error"


def test_engine_faults_still_count(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    for _ in range(4):
        failing(w, "line_failed")
    with pytest.raises(nw.ExitRun):
        failing(w, "stalled")
    assert (w.work / nw.BROKEN_FILE).exists()


def test_site_faults_between_engine_faults_do_not_reset_or_add(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    failing(w, "stalled")
    for _ in range(10):
        failing(w, "upload_failed")
    assert w._failed_jobs == 1


# ---- 2. out of memory
def oom_worker(tmp_path, monkeypatch, oom_runs):
    """A worker whose engine runs write 'CUDA out of memory' to the job log (and make nothing) for the first oom_runs."""
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    calls = {"run": 0, "vram": 0}
    real = w.run_synth

    def run_synth(ctx):
        calls["run"] += 1
        if calls["run"] <= oom_runs:
            with open(ctx.dir / "worker.log", "ab") as f:
                f.write(b"torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB\n")
            return 1
        return real(ctx)
    w.run_synth = run_synth
    monkeypatch.setattr(w, "wait_for_vram", lambda: calls.__setitem__("vram", calls["vram"] + 1))
    return w, calls


def test_oom_once_restarts_the_engine_waits_for_vram_and_finishes(tmp_path, monkeypatch, puts):
    w, calls = oom_worker(tmp_path, monkeypatch, 1)
    try:
        w.handle_job(job(JOB1))
        assert calls["run"] == 2 and calls["vram"] == 1
        assert "complete" in w.api.routes() and "fail" not in w.api.routes()
        assert events(tmp_path, "oom")
    finally:
        w.engine_shutdown("test")


def test_oom_twice_releases_the_job_and_does_not_count(tmp_path, monkeypatch, puts):
    w, calls = oom_worker(tmp_path, monkeypatch, 100)
    waits = []
    w.sleep_impl = lambda s, ctx=None: waits.append(s)
    try:
        for _ in range(8):                                    # more than FAILED_JOBS_LIMIT
            w.handle_job(job(JOB1))
        fails = [b for r, b in w.api.calls if r == "fail"]
        assert len(fails) == 8 and all(b["reason"] == "released" and b["retryable"] for b in fails)
        assert getattr(w, "_failed_jobs", 0) == 0 and not (w.work / nw.BROKEN_FILE).exists()
        assert calls["vram"] == 8 and min(waits) >= nw.GPU_FATAL_WAIT_SECONDS
        assert not (w.jobs_dir / JOB1).exists()
    finally:
        w.engine_shutdown("test")


def test_log_has_takes_any_of_several_texts(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("old\nOutOfMemoryError: boom\n", encoding="utf-8")
    assert nw.Worker.log_has(p, 0, nw.OOM_MARKERS)
    assert not nw.Worker.log_has(p, p.stat().st_size, nw.OOM_MARKERS)
    assert nw.Worker.log_has(p, 0, "old")


# ---- 4. idle engine
def test_idle_limit_is_30_minutes_without_a_yield_lock_and_5_with_one(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    assert w.engine_host().idle_seconds == 1800
    (tmp_path / "b").mkdir()
    w2, _ = make_worker(tmp_path / "b", monkeypatch, FakeApi())
    w2.cfg.yield_lock = str(tmp_path / "gpu.lock")
    assert w2.engine_host().idle_seconds == 300 == nw.ENGINE_IDLE_SECONDS


# ---- 5. synth words per hour: logged, never sent
def test_gen_wph_is_in_the_complete_log_and_not_in_the_live_keys(tmp_path, monkeypatch, puts):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    try:
        w.handle_job(job(JOB1))
        done = events(tmp_path, "complete")
        assert len(done) == 1 and "gen_wph=" in done[0]
        assert "gen_wph" not in nw.LIVE_KEYS and "gen_words_per_hour" not in w.live_fields(None)
    finally:
        w.engine_shutdown("test")


# ---- CLI latch: lifted once after 6 hours
def test_latch_lifts_once_after_six_hours_and_latches_again_if_it_breaks(tmp_path):
    clock = [1000.0]
    g, sp = make_group(tmp_path)
    gates = cli.RestartGates(lambda: clock[0])
    assert tick(g, gates) == "start"
    sp.procs[0].code = 7
    assert tick(g, gates) == "wait"
    clock[0] += cli.LATCH_RETRY_SECONDS - 60
    assert tick(g, gates) == "wait" and len(sp.procs) == 1 and cli.latched_now(g, gates)
    clock[0] += 120
    assert tick(g, gates) == "start" and len(sp.procs) == 2          # the one more attempt
    assert not (tmp_path / "work" / cli.LATCH_FILE).exists()
    sp.procs[1].code = 7                                              # it breaks again
    assert tick(g, gates) == "wait"
    clock[0] += 3600
    assert tick(g, gates) == "wait" and len(sp.procs) == 2            # latched again: a fresh 6 hours
    clock[0] += cli.LATCH_RETRY_SECONDS
    assert tick(g, gates) == "start" and len(sp.procs) == 3


def test_latch_found_on_disk_after_a_restart_uses_the_files_age(tmp_path):
    g, sp = make_group(tmp_path)
    latch = tmp_path / "work" / cli.LATCH_FILE
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text("exit 7")
    assert tick(g, cli.RestartGates(time.time)) == "wait"            # fresh file: held
    old = time.time() - cli.LATCH_RETRY_SECONDS - 10
    os.utime(latch, (old, old))
    assert tick(g, cli.RestartGates(time.time)) == "start"
