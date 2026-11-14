"""1.2.8 robustness fixes: stall watchdog, error-mode, update retry, proven tool download, trimming, one version."""
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "cli"))

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


# ------------------------------------------------------------------ 4. the audio tool download is proven
def _ffmpeg_env(tmp_path, sums="good", probe=(0, "ffmpeg version 7.0\n"), body=b"x" * 200_000):
    import hashlib
    import zipfile
    from speakstone_worker import setup
    seen = {"urls": []}

    def download(url, dest, cb):
        seen["urls"].append(url)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith("/checksums.sha256"):
            if sums == "missing":
                raise setup.SetupError("HTTP 404")
            arc = (dest.parent / "ffmpeg.zip").read_bytes()
            h = hashlib.sha256(arc).hexdigest() if sums == "good" else "0" * 64
            dest.write_text(f"{h}  {setup.FFMPEG_URLS['win'].rsplit('/', 1)[1]}\n{'1' * 64}  other.zip\n")
            return
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("bin/ffmpeg.exe", body)

    env = setup.Env(tmp_path / "inst", tmp_path / "res", download=download, is_win=True,
                    probe=lambda cmd, timeout: seen.setdefault("probe", (list(map(str, cmd)), timeout)) and probe)
    return env, setup, seen


def test_ffmpeg_download_is_checksummed_and_probed(tmp_path):
    env, setup, seen = _ffmpeg_env(tmp_path)
    setup.step_ffmpeg(env)
    assert seen["urls"] == [setup.FFMPEG_URLS["win"], setup.FFMPEG_URLS["win"].rsplit("/", 1)[0] + "/checksums.sha256"]
    assert (env.ffmpeg_dir / "ffmpeg.exe").stat().st_size == 200_000
    assert seen["probe"][0][1:] == ["-version"] and seen["probe"][1] == 20
    assert not (env.install / "ffmpeg.zip").exists()


def test_ffmpeg_checksum_mismatch_deletes_archive_and_raises(tmp_path):
    env, setup, _ = _ffmpeg_env(tmp_path, sums="bad")
    with pytest.raises(setup.SetupError, match="checksum"):
        setup.step_ffmpeg(env)
    assert not (env.install / "ffmpeg.zip").exists() and not (env.ffmpeg_dir / "ffmpeg.exe").exists()


def test_ffmpeg_without_a_checksum_list_is_not_installed(tmp_path):
    env, setup, _ = _ffmpeg_env(tmp_path, sums="missing")
    with pytest.raises(setup.SetupError, match="checked"):
        setup.step_ffmpeg(env)
    assert not (env.install / "ffmpeg.zip").exists() and not (env.ffmpeg_dir / "ffmpeg.exe").exists()


@pytest.mark.parametrize("probe", [(1, "ffmpeg version 7\n"), (0, "something else"), (-1, "")])
def test_ffmpeg_that_will_not_start_is_removed(tmp_path, probe):
    env, setup, _ = _ffmpeg_env(tmp_path, probe=probe)
    with pytest.raises(setup.SetupError, match="would not start"):
        setup.step_ffmpeg(env)
    assert not (env.ffmpeg_dir / "ffmpeg.exe").exists()


def test_ffmpeg_placeholder_is_replaced_but_a_real_file_is_kept(tmp_path):
    env, setup, seen = _ffmpeg_env(tmp_path)
    env.ffmpeg_dir.mkdir(parents=True)
    (env.ffmpeg_dir / "ffmpeg.exe").write_bytes(b"x")                   # a placeholder
    setup.step_ffmpeg(env)
    assert (env.ffmpeg_dir / "ffmpeg.exe").stat().st_size == 200_000 and seen["urls"]
    seen["urls"].clear()
    setup.step_ffmpeg(env)                                              # now a real size: nothing is fetched
    assert seen["urls"] == []


def test_checksum_line_parser():
    from speakstone_worker import setup
    h = "ab" * 32
    assert setup._checksum_for(f"{h}  a.zip\n{'cd' * 32} *b.zip\n", "a.zip") == h
    assert setup._checksum_for(f"{h}  a.zip\n{'cd' * 32} *b.zip\n", "b.zip") == "cd" * 32
    assert setup._checksum_for(f"{h}  a.zip\n", "c.zip") is None
    assert setup._checksum_for("nothex  a.zip\n", "a.zip") is None


# ------------------------------------------------------------------ 5. setup steps cannot hang for ever
def test_silent_child_is_killed_with_a_plain_sentence():
    from speakstone_worker import setup
    seen = []
    code = "import time; print('hello', flush=True); time.sleep(60)"
    t0 = time.time()
    with pytest.raises(setup.SetupError, match="printed nothing for 2 seconds and was stopped; start setup again"):
        setup.real_run([sys.executable, "-c", code], dict(__import__("os").environ), None, seen.append, stall=2, poll=0.1)
    assert time.time() - t0 < 20 and seen == ["hello"]


def test_chatty_child_is_not_stalled_and_lines_and_exit_code_pass_through():
    from speakstone_worker import setup
    seen = []
    code = "import time\nfor i in range(4):\n    print('line', i, flush=True); time.sleep(0.6)\nraise SystemExit(3)"
    rc = setup.real_run([sys.executable, "-c", code], dict(__import__("os").environ), None, seen.append, stall=1.5, poll=0.1)
    assert rc == 3 and seen == [f"line {i}" for i in range(4)]


def test_stall_limit_from_environment():
    from speakstone_worker import setup
    assert setup.setup_stall_seconds({}) == 7200
    assert setup.setup_stall_seconds({"SSW_SETUP_STALL_SECONDS": "60"}) == 60
    assert setup.setup_stall_seconds({"SSW_SETUP_STALL_SECONDS": "x"}) == 7200
    assert setup._how_long(1800) == "30 minutes"



# ------------------------------------------------------------------ 6. trimming
def _age(path, days):
    import os
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_old_worker_logs_are_deleted_and_recent_and_other_files_stay(tmp_path):
    for name, days in (("worker_2020-01-01.log", 30), ("worker_old.log", 15), ("worker_new.log", 1),
                       ("background.log", 90), ("notes.txt", 90)):
        (tmp_path / name).write_text("x")
        _age(tmp_path / name, days)
    assert nw.prune_logs(tmp_path) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["background.log", "notes.txt", "worker_new.log"]
    assert nw.prune_logs(tmp_path / "missing") == 0                      # never raises


def test_refs_over_the_limit_lose_the_least_recently_used_first(tmp_path):
    import os
    now = time.time()
    for i, name in enumerate(["a", "b", "c", "d"]):
        f = tmp_path / f"{name}.ogg"
        f.write_bytes(b"x" * 100)
        os.utime(f, (now - 1000 + i * 10, now - 1000 + i * 10))          # a oldest ... d newest
    os.utime(tmp_path / "a.ogg", (now, now - 5000))                      # a was USED just now: access time wins
    removed = nw.prune_refs(tmp_path, now=now, limit=350, target=250)
    assert removed == 2 and sorted(p.name for p in tmp_path.iterdir()) == ["a.ogg", "d.ogg"]
    assert nw.prune_refs(tmp_path, now=now, limit=350, target=250) == 0   # under the limit: nothing happens


def test_refs_under_the_limit_are_untouched_and_old_part_files_go(tmp_path):
    (tmp_path / "keep.ogg").write_bytes(b"x" * 100)
    (tmp_path / "old.ogg.part").write_bytes(b"x")
    (tmp_path / "fresh.ogg.part").write_bytes(b"x")
    _age(tmp_path / "old.ogg.part", 2)
    assert nw.prune_refs(tmp_path) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fresh.ogg.part", "keep.ogg"]
    assert nw.REFS_LIMIT_BYTES == 1024 ** 3 and nw.REFS_TARGET_BYTES == 800 * 1024 ** 2


def test_worker_start_prunes_before_any_claim(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(nw, "prune_logs", lambda d: called.append(("logs", Path(d).name)))
    monkeypatch.setattr(nw, "prune_refs", lambda d: called.append(("refs", Path(d).name)))
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "config.yaml").write_text("x")
    w, _ = make_worker(tmp_path, monkeypatch)
    w.cfg.model_dir = str(tmp_path / "model")
    monkeypatch.setattr(nw, "compute_model_hash", lambda *a, **k: "h")
    monkeypatch.setattr(nw, "model_revision", lambda *a, **k: "r")
    w.api = type("A", (), {"get_me": staticmethod(lambda: (200, {"worker": {}})), "post": staticmethod(lambda r, b: (200, {}))})()
    w.startup()
    assert called == [("logs", "logs"), ("refs", "refs")]


def test_background_log_is_cut_to_its_tail_only_when_large(tmp_path):
    from speakstone_worker import cli
    log = tmp_path / "background.log"
    lines = [f"2026-01-01 00:00:00 line {i:06d}\n" for i in range(60_000)]
    log.write_text("".join(lines))
    assert log.stat().st_size > cli.BACKGROUND_LOG_MAX and cli.trim_background_log(log) is True
    data = log.read_text()
    assert 150_000 < len(data) <= cli.BACKGROUND_LOG_KEEP and data.endswith(lines[-1]) and data.startswith("2026")
    assert cli.trim_background_log(log) is False                         # small now: left alone
    assert cli.trim_background_log(tmp_path / "nope.log") is False


def test_worker_version_matches_the_app_version():
    """net_worker.py's WORKER_VERSION (sent to the site, read by setup and the board) must never drift from the app's."""
    import re
    from speakstone_worker import __version__
    m = re.search(r'^WORKER_VERSION\s*=\s*"([^"]+)"', (ROOT_DIR / "net_worker.py").read_text(encoding="utf-8"), re.M)
    assert m and m.group(1) == __version__ == "1.2.14"
    assert nw.WORKER_VERSION == __version__


# ------------------------------------------------------------------ 9. threads do not die silently
def test_tray_poll_survives_a_failing_refresh():
    import threading
    from speakstone_worker import tray
    t = tray.Tray.__new__(tray.Tray)
    t._stop = threading.Event()
    t.poll_seconds = 0.01
    n = []

    def refresh():
        n.append(1)
        if len(n) >= 3:
            t._stop.set()
        raise RuntimeError("log call failed too")
    t.refresh = refresh
    t._poll()
    assert len(n) == 3


def test_stats_3d_loop_survives_a_failing_reading(monkeypatch):
    from speakstone_worker import stats
    g = stats.GpuMonitor(reader=lambda: None, d3_reader=lambda: None, windows=True, start_thread=False)
    calls = []

    def boom():
        calls.append(1)
        if len(calls) >= 3:
            g.d3_ok = False
        raise OSError("typeperf gone")
    g.d3_reader = boom
    monkeypatch.setattr(stats.time, "sleep", lambda s: None)
    g._loop3d()
    assert len(calls) == 3


def test_safe_check_logs_one_line_and_never_raises(tmp_path):
    from speakstone_worker import cli

    class Flow:
        def check(self):
            raise ValueError("weird")
    cli.safe_check(Flow(), tmp_path)
    text = (tmp_path / "work" / "logs" / "background.log").read_text()
    assert text.count("\n") == 1 and "update check failed: ValueError: weird" in text
