"""handle_job: an unexpected error is a retryable "worker_error" failure instead of an escape (2026-11-14 review)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from test_net_worker_core import JOB1, JOB2, FakeApi, events, job, make_worker, nw, puts  # noqa: E402,F401


def fails(api):
    return [b for r, b in api.calls if r == "fail"]


def test_unexpected_error_sends_a_retryable_fail_kills_the_engine_and_the_loop_goes_on(tmp_path, monkeypatch, puts):
    api = FakeApi([job(JOB1), job(JOB2)])
    w, _ = make_worker(tmp_path, monkeypatch, api)
    killed = []
    monkeypatch.setattr(nw, "kill_tree", lambda p: killed.append(p))
    real_work = w._work
    calls = {"n": 0}

    def work(ctx, j):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom at C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) + "secret.txt")
        return real_work(ctx, j)
    w._work = work
    w.handle_job(api.jobs.pop(0))
    f = fails(api)
    assert len(f) == 1 and f[0]["reason"] == "worker_error" and f[0]["retryable"] is True
    assert "RuntimeError" in f[0]["message"] and "someone" not in f[0]["message"] and "Traceback" not in f[0]["message"]
    assert killed and not w.safe_job_dir(JOB1).exists()
    assert len(events(tmp_path, "error")) == 1 and "where=job" in events(tmp_path, "error")[0]
    w.handle_job(api.jobs.pop(0))                              # the loop is still usable: the next job completes
    assert "complete" in api.routes() and not w.safe_job_dir(JOB2).exists()


def test_keyboard_interrupt_still_propagates(tmp_path, monkeypatch):
    api = FakeApi([job(JOB1)])
    w, _ = make_worker(tmp_path, monkeypatch, api)

    def work(ctx, j):
        raise KeyboardInterrupt()
    w._work = work
    with pytest.raises(KeyboardInterrupt):
        w.handle_job(api.jobs.pop(0))
    assert fails(api) == []


def test_a_failure_before_the_heartbeat_gives_the_job_back(tmp_path, monkeypatch):
    api = FakeApi([job(JOB1)])
    w, _ = make_worker(tmp_path, monkeypatch, api)

    def full_disk(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(nw, "atomic_write_json", full_disk)
    w.handle_job(api.jobs.pop(0))
    f = fails(api)
    assert len(f) == 1 and f[0]["reason"] == "worker_error" and f[0]["retryable"] is True
    assert f[0]["lease_id"] == "lease" and f[0]["job_id"] == JOB1
    assert not w.safe_job_dir(JOB1).exists()


def test_a_failure_before_the_heartbeat_survives_an_unreachable_site(tmp_path, monkeypatch):
    api = FakeApi([job(JOB1)])
    w, _ = make_worker(tmp_path, monkeypatch, api)
    monkeypatch.setattr(nw, "atomic_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    def down(route, body):
        raise nw.NetError("offline")
    api.post = down
    w.handle_job(job(JOB1))                                    # no exception reaches the loop
