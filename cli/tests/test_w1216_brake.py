"""The brake counts every failed job (2026-11-14 review): two with no line made, or five failures of any kind in a row,
stop the worker; a completed job resets it; the user's own stop counts for nothing; the wait doubles up to a cap."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_net_worker_core import JOB1, FakeApi, job, make_worker, nw  # noqa: E402


def failing(worker, reason="line_failed", none_made=False):
    """Make the next handle_job end in a JobFail; -> the wait it asked for."""
    waits = []
    worker.sleep_impl = lambda s, ctx=None: waits.append(s)

    def work(ctx, j):
        f = nw.JobFail(reason, "it failed", True, True)
        f.none_made = none_made
        raise f
    worker._work = work
    worker.handle_job(job(JOB1))
    return waits[-1] if waits else 0


def test_constants():
    assert nw.DEAD_JOBS_LIMIT == 2 and nw.FAILED_JOBS_LIMIT == 5 and nw.DEAD_JOB_WAIT_SECONDS == 120


def test_two_jobs_with_no_line_made_stop(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    assert failing(w, none_made=True) == 120
    with pytest.raises(nw.ExitRun) as e:
        failing(w, none_made=True)
    assert e.value.code == nw.EXIT_BROKEN
    assert (w.work / nw.BROKEN_FILE).read_text(encoding="utf-8") == "it failed"


def test_five_mixed_failures_in_a_row_stop(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    for reason, nm in (("stalled", False), ("upload_failed", False), ("line_failed", True), ("worker_error", False)):
        failing(w, reason, nm)
    with pytest.raises(nw.ExitRun) as e:
        failing(w, "stalled")
    assert e.value.code == nw.EXIT_BROKEN and (w.work / nw.BROKEN_FILE).exists()


def test_a_non_empty_failure_between_two_empty_ones_breaks_the_empty_run(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    failing(w, none_made=True)
    failing(w, "upload_failed")
    failing(w, none_made=True)           # not two empty jobs in a row, and only three failures in all: no stop


def test_a_completed_job_resets_the_count(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    for _ in range(4):
        failing(w, "stalled")
    w._work = nw.Worker._work.__get__(w)             # back to the real job code
    # a real completion: the fake engine makes every line and the fake site accepts it
    api = FakeApi([job(JOB1)])
    w.api = api
    monkeypatch.setattr(nw, "http_request", lambda *a, **k: (200, b""))
    w.handle_job(api.jobs.pop(0))
    assert "complete" in api.routes() and w._failed_jobs == 0 and w._dead_jobs == 0
    for _ in range(4):
        failing(w, "stalled")            # four more are fine: the count started again


def test_a_stop_by_the_user_does_not_count(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    for _ in range(10):
        assert failing(w, "released") == 0
    assert getattr(w, "_failed_jobs", 0) == 0


def test_a_yield_hand_back_does_not_count(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    w.sleep_impl = lambda s, ctx=None: None

    def work(ctx, j):
        raise nw.Yield()
    w._work = work
    for _ in range(8):
        w.handle_job(job(JOB1))
    assert getattr(w, "_failed_jobs", 0) == 0


def test_the_wait_doubles_and_is_capped(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    f = nw.JobFail("stalled", "x", True, True)
    got = []
    monkeypatch.setattr(nw, "FAILED_JOBS_LIMIT", 100)
    for _ in range(7):
        got.append(w.brake(f))
    assert got == [120, 240, 480, 960, 1800, 1800, 1800]


def test_gpu_fatal_keeps_a_longer_wait_when_it_is_longer(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    monkeypatch.setattr(nw, "GPU_FATAL_WAIT_SECONDS", 900)
    assert w.brake(nw.JobFail("gpu_fatal", "x", True, False)) == 900


def test_an_unexpected_error_and_a_setup_failure_count_too(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    w.sleep_impl = lambda s, ctx=None: None

    def boom(ctx, j):
        raise RuntimeError("x")
    w._work = boom
    w.handle_job(job(JOB1))
    monkeypatch.setattr(nw, "atomic_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    w.handle_job(job(JOB1))
    assert w._failed_jobs == 2
