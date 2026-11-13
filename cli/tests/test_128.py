"""1.2.8 robustness fixes: stall watchdog, error-mode, update retry, proven tool download, trimming, one version."""
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]

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


# ------------------------------------------------------------------ 2. no blocking error boxes
@pytest.mark.parametrize("modname", ["net_worker", "indextts2_worker", "speakstone_worker.cli"])
def test_error_boxes_are_switched_off_and_never_raise(monkeypatch, modname):
    import ast
    import ctypes
    path = {"net_worker": ROOT_DIR / "net_worker.py", "indextts2_worker": ROOT_DIR / "indextts2_worker.py",
            "speakstone_worker.cli": ROOT_DIR / "cli" / "speakstone_worker" / "cli.py"}[modname]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # the function exists in the file itself (the two engine files are standalone) and main starts by calling it
    assert any(isinstance(n, ast.FunctionDef) and n.name == "no_error_boxes" for n in tree.body)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    first = [n for n in main.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))][0]
    assert isinstance(first.value, ast.Call) and first.value.func.id == "no_error_boxes"
    # run the function from that file against a fake kernel32
    ns = {"sys": sys}
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "no_error_boxes")
    exec(compile(ast.Module([fn], []), str(path), "exec"), ns)
    calls = []
    fake = type("W", (), {"kernel32": type("K", (), {"SetErrorMode": staticmethod(lambda m: calls.append(m))})})
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    ns["no_error_boxes"]()
    assert calls == [0x8003]
    monkeypatch.setattr(fake.kernel32, "SetErrorMode", staticmethod(lambda m: 1 / 0))
    ns["no_error_boxes"]()                                              # an error is swallowed
    monkeypatch.setattr(sys, "platform", "linux")
    ns["no_error_boxes"]()                                              # and other systems do nothing


# ------------------------------------------------------------------ 3. update check retries sooner after a failure
def _flow(tmp_path, errors, logs):
    from test_update_tray import make_flow
    from speakstone_worker import update
    f = make_flow(tmp_path, log=logs.append)
    it = iter(errors)

    def check_fn(env=None):
        e = next(it)
        if e is not None:
            raise e
        return None
    f.check_fn = check_fn
    return f, update


def test_failed_checks_back_off_2_5_15_60_and_log_once(tmp_path):
    logs = []
    from speakstone_worker import update
    down = update.UpdateError("could not reach the release page")
    f, _ = _flow(tmp_path, [down] * 6 + [None], logs)
    gaps = []
    for _ in range(6):
        t0 = f.clock()
        assert f.check() is None
        gaps.append(f.next_check - t0)
    assert gaps == [120, 300, 900, 3600, 3600, 3600]
    assert logs == ["update check: could not reach the release page"]          # one line for the whole run of failures
    t0 = f.clock()
    f.check()                                                                  # success: back to the normal interval
    assert f.next_check - t0 == update.CHECK_EVERY and f.failures == 0


def test_a_different_failure_message_is_logged_and_a_later_run_logs_again(tmp_path):
    logs = []
    from speakstone_worker import update
    a, b = update.UpdateError("a"), update.UpdateError("b")
    f, _ = _flow(tmp_path, [a, a, b, None, a], logs)
    for _ in range(5):
        f.check()
    assert logs == ["update check: a", "update check: b", "update check: a"]


def test_switched_off_keeps_six_hours(tmp_path):
    logs = []
    from speakstone_worker import update
    f, _ = _flow(tmp_path, [update.UpdateError(update.SWITCHED_OFF)], logs)
    t0 = f.clock()
    f.check()
    assert f.next_check - t0 == update.CHECK_EVERY and f.failures == 0
    assert update.SWITCHED_OFF == "updates are switched off (SSW_NO_UPDATE)"


def test_unexpected_error_in_check_is_logged_not_raised(tmp_path):
    logs = []
    f, _ = _flow(tmp_path, [KeyError("boom")], logs)
    assert f.check() is None
    assert len(logs) == 1 and "KeyError" in logs[0] and f.next_check - f.clock() == 120
