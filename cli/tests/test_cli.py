import io
import json
import logging
import os
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speakstone_worker import board, enroll, idle, service, setup, stats  # noqa: E402
from speakstone_worker.settings import in_pause_window  # noqa: E402

KEY = "sswk_" + "A" * 43


class FakeResp:
    def __init__(self, status, body):
        self.status, self._b = status, json.dumps(body).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


# ------------------------------------------------------------------ enrol
def test_enroll_success_sends_payload_and_returns_key():
    seen = {}

    def opener(req, timeout=0):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return FakeResp(201, {"worker_id": "w1", "key": KEY, "label": "Brave Otter", "tier": "new"})

    r = enroll.enroll("https://x.test/", "PC", "RTX 3070 Ti", 8192, "Windows 11", "1.0.1", opener=opener)
    assert seen["url"] == "https://x.test/api/worker/enroll"
    assert set(seen["body"]) == {"machine_name", "gpu", "vram_mib", "os", "worker_version"}
    assert r["key"] == KEY and r["label"] == "Brave Otter"


@pytest.mark.parametrize("code,word", [(429, "Too many"), (503, "not taking"), (422, "did not accept")])
def test_enroll_error_mapping(code, word):
    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b'{"error":"' + KEY.encode() + b'"}'))
    with pytest.raises(enroll.EnrollError) as e:
        enroll.enroll("https://x.test", "PC", "g", 8192, "os", "1", opener=opener)
    assert word in str(e.value) and KEY not in str(e.value)


@pytest.mark.parametrize("status,body,text", [
    (422, b'{"code":"gpu_too_small"}', "at least 8 GB of memory"),
    (503, b'{"code":"network_full"}', "try again tomorrow"),
    (429, b'{"code":"rate"}', "Too many"),
    (422, b'not json', "did not accept")])
def test_enroll_maps_server_codes(status, body, text):
    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(body))
    with pytest.raises(enroll.EnrollError) as e:
        enroll.enroll("https://x.test", "PC", "g", 8192, "os", "1", opener=opener)
    assert text in str(e.value) and "HTTP" not in str(e.value)


def test_enroll_unreachable():
    def opener(req, timeout=0):
        raise urllib.error.URLError("down")
    with pytest.raises(enroll.EnrollError, match="Could not reach"):
        enroll.enroll("https://x.test", "PC", "g", 8192, "os", "1", opener=opener)


def test_api_base_env_override(monkeypatch):
    from speakstone_worker import paths
    monkeypatch.setenv("SPEAKSTONE_API_BASE", "http://localhost:9/")
    assert paths.api_base() == "http://localhost:9"


# ------------------------------------------------------------------ setup
WORKER_BODY = 'WORKER_VERSION = "1"\n' + "# worker script\n" * 100       # big enough not to look like a placeholder


def make_env(tmp_path, calls, logs, fail_at=None, gpu=8192):
    res = tmp_path / "res"
    res.mkdir(parents=True, exist_ok=True)
    for n in setup.WORKER_FILES:
        (res / n).write_text(WORKER_BODY)
    inst = tmp_path / "inst"

    def run(cmd, env, cwd, on_line):
        c = [str(x) for x in cmd]
        calls.append((c, dict(env)))
        if "python" in c and "install" in c and not (inst / "venv").exists():
            pass
        if len(c) > 2 and c[2] == "init":                    # net_worker.py init writes the config
            (inst / "worker_config.json").write_text('{"worker_key": "test-key"}')
        if "venv" in c:
            (inst / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (inst / "venv" / "Scripts" / "python.exe").write_text("")
        if "snapshot_download(" in " ".join(c) and "IndexTTS-2" in " ".join(c):
            for f in setup.MODEL_FILES:
                (inst / "IndexTTS-2").mkdir(parents=True, exist_ok=True)
                (inst / "IndexTTS-2" / f).write_text("x")
        if "hf_hub_download(" in " ".join(c):
            fake_hf_cache(inst)
        if fail_at and fail_at in " ".join(c):
            return 1
        on_line("ok")
        return 0

    def download(url, dest, cb):
        import zipfile
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("uv.exe" if "uv" in url else ("it/pyproject.toml" if "index-tts" in url else "bin/ffmpeg.exe"),
                       "x" * 200_000)

    def do_enroll():
        return {"key": KEY, "label": "Brave Otter", "worker_id": "w1", "tier": "new"}

    return setup.Env(inst, res, download=download, run=run, enroll=do_enroll, gpu_total_mib=lambda: gpu,
                     progress=lambda s, f, m: logs.append((s, m)), is_win=True,
                     base_env={"USERPROFILE": str(tmp_path / "nohome"), "HOME": str(tmp_path / "nohome")})


@pytest.fixture(autouse=True)
def big_disk(monkeypatch):
    import shutil
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(1, 1, 500 * 1024 ** 3))


def test_torch_pin_present_and_never_unpinned_reinstall(tmp_path):
    calls, logs = [], []
    env = make_env(tmp_path, calls, logs)
    setup.run_setup(env)
    torch_cmds = [c for c, _ in calls if "torch==2.8.0+cu128" in c]
    assert torch_cmds and "torchaudio==2.8.0+cu128" in torch_cmds[0]
    assert "--index-url" in torch_cmds[0] and setup.TORCH_INDEX in torch_cmds[0]
    assert not any("--reinstall" in c and "torch" in c for c, _ in calls)
    assert setup.TORCH_PINS == ("torch==2.8.0+cu128", "torchaudio==2.8.0+cu128")
    # the pins go in before the engine, so the engine install finds them and cannot swap them
    order = [c for c, _ in calls if "pip" in c and "install" in c]
    first_torch = next(i for i, c in enumerate(order) if "torch==2.8.0+cu128" in c)
    engine = next(i for i, c in enumerate(order) if "-e" in c)
    assert first_torch < engine


def test_every_pip_install_carries_the_constraint_file(tmp_path):
    calls = []
    env = make_env(tmp_path, calls, [])
    setup.run_setup(env)
    pips = [c for c, _ in calls if "pip" in c and "install" in c]
    assert len(pips) == 4                      # huggingface_hub alone, torch, omegaconf/protobuf, then the engine packages
    for c in pips:
        i = c.index("-c")
        assert c[i + 1] == str(env.constraints_file)
        # only the torch install itself names the cu128 index (uv takes a package from the first index that has it)
        assert (setup.TORCH_INDEX in c) == any(pin in c for pin in setup.TORCH_PINS)
    assert env.constraints_file.read_text().split() == list(setup.TORCH_PINS)


def test_torch_install_skipped_when_right_version_present(tmp_path):
    calls, logs = [], []
    env = make_env(tmp_path, calls, logs)
    real = env.run

    def run(cmd, e, cwd, on_line):
        if "import torch;print(torch.__version__)" in [str(x) for x in cmd]:
            calls.append(([str(x) for x in cmd], {}))
            on_line("2.8.0+cu128")
            return 0
        return real(cmd, e, cwd, on_line)
    env.run = run
    setup.run_setup(env)
    assert not any("torch==2.8.0+cu128" in c for c, _ in calls)
    assert any("--upgrade" in c for c, _ in calls)           # the small packages are still checked
    assert "Right version already installed, skipping" in [m for _, m in logs]


def test_setup_state_is_verified_against_files(tmp_path):
    env = make_env(tmp_path, [], [])
    setup.run_setup(env)
    assert setup.is_set_up(env.install)
    assert not (env.install / "uv_cache").exists()
    (env.install / "uv_cache").mkdir()
    shutil_rmtree = __import__("shutil").rmtree
    shutil_rmtree(env.model_dir)
    assert not setup.is_set_up(env.install)
    calls = []
    env2 = make_env(tmp_path, calls, [])
    ran = setup.run_setup(env2)
    assert "model" in ran and "torch" not in ran and "uv" not in ran
    # losing the venv re-runs what lives in it
    shutil_rmtree(env2.venv)
    assert not setup.is_set_up(env2.install)
    ran = setup.run_setup(make_env(tmp_path, [], []))
    assert {"python", "torch", "packages"} <= set(ran)
    assert setup.is_set_up(env2.install)


def test_model_download_skips_qwen_and_is_skipped_when_complete(tmp_path):
    calls = []
    env = make_env(tmp_path, calls, [])
    setup.run_setup(env)
    code = [c[-1] for c, _ in calls if "snapshot_download('IndexTeam" in c[-1]][0]
    assert "ignore_patterns=['qwen*/*', 'qwen*']" in code
    n = len(calls)
    setup.step_model(env)
    assert len(calls) == n


def test_model_and_extras_reused_from_user_cache(tmp_path):
    home = tmp_path / "nohome"
    snap = home / ".cache" / "huggingface" / "hub" / "models--IndexTeam--IndexTTS-2" / "snapshots" / "s1"
    for f in (*setup.MODEL_FILES, "qwen0.6bemo4-merge/model.safetensors"):
        (snap / f).parent.mkdir(parents=True, exist_ok=True)
        (snap / f).write_text("x")
    user_hub = home / ".cache" / "huggingface" / "hub"
    for repo, files in setup.EXTRAS_NEED.items():
        for f in files:
            (user_hub / repo / "snapshots" / "abc" / f).parent.mkdir(parents=True, exist_ok=True)
            (user_hub / repo / "snapshots" / "abc" / f).write_text("x")
    calls, logs = [], []
    env = make_env(tmp_path, calls, logs)
    setup.run_setup(env)
    joined = [" ".join(c) for c, _ in calls]
    assert not any("snapshot_download(" in j for j in joined) and not any("hf_hub_download(" in j for j in joined)
    assert setup.model_complete(env.model_dir) and not (env.model_dir / "qwen0.6bemo4-merge").exists()
    assert setup.extras_cached(env.install)
    assert any("Found on this PC, copying" in m for _, m in logs)


def test_incomplete_user_cache_is_not_reused(tmp_path):
    hub = tmp_path / "nohome" / ".cache" / "huggingface" / "hub"
    for repo, files in setup.EXTRAS_NEED.items():
        for f in files:
            (hub / repo / "snapshots" / "abc" / f).parent.mkdir(parents=True, exist_ok=True)
            (hub / repo / "snapshots" / "abc" / f).write_text("x")
    (hub / "models--funasr--campplus" / "blobs").mkdir(parents=True)
    (hub / "models--funasr--campplus" / "blobs" / "dead.incomplete").write_text("x")
    env = make_env(tmp_path, [], [])
    setup.step_extras(env)                       # the fake run does the download for the one that was left out
    assert not (env.install / "hf" / "hub" / "models--funasr--campplus" / "blobs" / "dead.incomplete").exists()
    assert not setup._repo_complete(hub / "models--funasr--campplus", ["campplus_cn_common.bin"])


def test_ssw_model_dir_env_is_used(tmp_path):
    src = tmp_path / "mine"
    src.mkdir()
    for f in setup.MODEL_FILES:
        (src / f).write_text("x")
    calls = []
    env = make_env(tmp_path, calls, [])
    env.base_env["SSW_MODEL_DIR"] = str(src)
    setup.step_model(env)
    assert setup.model_complete(env.model_dir) and calls == []


def test_key_only_in_child_env_not_argv_logs_or_files(tmp_path, caplog):
    calls, logs = [], []
    env = make_env(tmp_path, calls, logs)
    setup.run_setup(env)
    init = [(c, e) for c, e in calls if len(c) > 2 and c[2] == "init"]
    assert len(init) == 1 and init[0][1]["SSW_WORKER_KEY"] == KEY
    assert all(KEY not in " ".join(c) for c, _ in calls)
    assert all(KEY not in m for _, m in logs)
    assert all("SSW_WORKER_KEY" not in e for c, e in calls if len(c) < 3 or c[2] != "init")
    for f in env.install.rglob("*"):
        if f.is_file() and f.stat().st_size < 1_000_000:
            assert KEY not in f.read_text(errors="ignore"), f
    assert json.loads(env.enrolled_file.read_text())["label"] == "Brave Otter"
    assert env.progress and "Joined the queue as Brave Otter" in [m for _, m in logs]


def test_setup_resumes_after_interruption(tmp_path):
    calls, logs = [], []
    env = make_env(tmp_path, calls, logs, fail_at="snapshot_download")
    with pytest.raises(setup.SetupError):
        setup.run_setup(env)
    assert "model" not in setup.load_state(env) and "torch" in setup.load_state(env)
    calls.clear()
    env2 = make_env(tmp_path, calls, logs)
    ran = setup.run_setup(env2)
    assert "torch" not in ran and "uv" not in ran and "model" in ran and "enroll" in ran
    assert setup.is_set_up(env2.install)


def test_gpu_too_small_and_missing(tmp_path):
    with pytest.raises(setup.SetupError, match="8 GB"):
        setup.run_setup(make_env(tmp_path, [], [], gpu=4096))
    with pytest.raises(setup.SetupError, match="No NVIDIA"):
        setup.run_setup(make_env(tmp_path / "b", [], [], gpu=None))


def test_model_download_disables_hf_transfer(tmp_path):
    calls = []
    setup.run_setup(make_env(tmp_path, calls, []))
    model = [e for c, e in calls if "snapshot_download" in " ".join(c)][0]
    assert model["HF_HUB_ENABLE_HF_TRANSFER"] == "0"


# ------------------------------------------------------------------ parallel setup lanes
def _is_torch(c):
    return "torch==2.8.0+cu128" in c


def _is_packages(c):
    return "-e" in c and "pip" in c


def _is_model(c):
    return "snapshot_download('IndexTeam" in c[-1]


def _is_extras(c):
    return "hf_hub_download(" in c[-1]


def _is_hf_only(c):
    return "pip" in c and "install" in c and c[-1] == setup.HF_HUB_SPEC and "-e" not in c


def wrap_run(env, hook):
    real = env.run

    def run(cmd, e, cwd, on_line):
        hook([str(x) for x in cmd])
        return real(cmd, e, cwd, on_line)
    env.run = run


def test_lanes_really_overlap(tmp_path):
    env = make_env(tmp_path, [], [])
    torch_in, model_in = threading.Event(), threading.Event()
    met = {}

    def hook(c):
        if _is_torch(c):
            torch_in.set()
            met["torch_saw_model"] = model_in.wait(5)
        elif _is_model(c):
            model_in.set()
            met["model_saw_torch"] = torch_in.wait(5)
    wrap_run(env, hook)
    setup.run_setup(env)
    assert met == {"torch_saw_model": True, "model_saw_torch": True}
    assert setup.is_set_up(env.install)


def test_hf_hub_installed_before_the_download_lane_starts(tmp_path):
    calls = []
    setup.run_setup(make_env(tmp_path, calls, []))
    cmds = [c for c, _ in calls]
    hf = next(i for i, c in enumerate(cmds) if _is_hf_only(c))
    assert hf < next(i for i, c in enumerate(cmds) if _is_model(c))
    assert hf < next(i for i, c in enumerate(cmds) if _is_torch(c))      # and before any other pip install
    assert hf < next(i for i, c in enumerate(cmds) if _is_packages(c))


def test_failure_in_download_lane_lets_the_other_lane_step_finish(tmp_path):
    calls = []
    env = make_env(tmp_path, calls, [], fail_at="snapshot_download")
    wrap_run(env, lambda c: time.sleep(0.4) if _is_torch(c) else None)    # torch is still running when the model fails
    with pytest.raises(setup.SetupError, match="voice model"):
        setup.run_setup(env)
    state = setup.load_state(env)
    assert "torch" in state                                              # its current step finished
    assert not state & {"packages", "model", "extras", "ffmpeg", "enroll", "check"}
    assert json.loads(env.state_file.read_text())["done"] == sorted(state)
    calls.clear()
    ran = setup.run_setup(make_env(tmp_path, calls, []))
    assert set(ran) >= {"packages", "model", "extras", "ffmpeg", "enroll", "check"} and "torch" not in ran
    assert not any(_is_torch(c) for c, _ in calls)
    assert setup.is_set_up(env.install)


def test_failure_in_pip_lane_lets_the_download_finish_and_raises(tmp_path):
    env = make_env(tmp_path, [], [], fail_at="torch==2.8.0+cu128")
    wrap_run(env, lambda c: time.sleep(0.3) if _is_model(c) else None)
    with pytest.raises(setup.SetupError, match="graphics-card libraries"):
        setup.run_setup(env)
    state = setup.load_state(env)
    assert "model" in state and "torch" not in state and "packages" not in state and "extras" not in state


def test_nothing_left_to_do_runs_no_commands(tmp_path):
    setup.run_setup(make_env(tmp_path, [], []))
    calls = []
    assert setup.run_setup(make_env(tmp_path, calls, [])) == ["gpu", "folders"]     # the two cheap checks always run
    assert calls == []


def test_only_runs_one_lane_step(tmp_path):
    setup.run_setup(make_env(tmp_path, [], []))
    import shutil
    shutil.rmtree(tmp_path / "inst" / "hf")
    calls = []
    ran = setup.run_setup(make_env(tmp_path, calls, []), only=("extras",))
    assert ran == ["extras"]
    assert any(_is_extras(c) for c, _ in calls) and not any(_is_torch(c) or _is_model(c) for c, _ in calls)


def test_serial_switch_keeps_the_old_order(tmp_path):
    calls = []
    env = make_env(tmp_path, calls, [])
    env.base_env["SSW_SETUP_SERIAL"] = "1"
    setup.run_setup(env)
    cmds = [c for c, _ in calls]
    pos = [next(i for i, c in enumerate(cmds) if f(c)) for f in (_is_torch, _is_packages, _is_model, _is_extras)]
    assert pos == sorted(pos)
    assert not any(_is_hf_only(c) for c in cmds)                       # packages brings it in, as before
    assert setup.is_set_up(env.install)


def test_fast_hf_transfer_only_for_setup_downloads(tmp_path):
    from speakstone_worker.runner import WorkerRunner
    calls = []
    setup.run_setup(make_env(tmp_path, calls, []))
    fast = [(c, e) for c, e in calls if _is_model(c) or _is_extras(c)]
    assert len(fast) == 2 and all(e["HF_XET_HIGH_PERFORMANCE"] == "1" for _, e in fast)
    assert all(e["HF_HUB_ENABLE_HF_TRANSFER"] == "0" for _, e in fast)
    assert not any("HF_XET_HIGH_PERFORMANCE" in e for c, e in calls if not (_is_model(c) or _is_extras(c)))
    assert "HF_XET_HIGH_PERFORMANCE" not in WorkerRunner(tmp_path).environment({})


# ------------------------------------------------------------------ board
def sample_state():
    now = time.time()
    return {"lines": 1284, "words": 38910, "audio_min": 271, "lines_hour": 212, "words_hour": 4210,
            "since_label": "08:14", "queue": {"queued_lines": 12406, "eta_seconds": 3 * 3600 + 12 * 60},
            "time_left": 3 * 3600 + 12 * 60, "spark": [1, 2, 3, 3, 4, 5, 6, 5, 4, 5, 6, 8],
            "gpu": {"name": "NVIDIA RTX 3070 Ti", "util_pct": 92, "temp_c": 68, "mem_used_mib": 4608,
                    "mem_total_mib": 8192},
            "making": True, "job": "abcdef123456", "job_lines": 12, "job_pct": 0.68, "state": "idle",
            "events": [(now, "done", "job abcdef12 - 12 lines", "42 s"), (now - 90, "retry", "job 1234", "picked up again")]}


def render(renderable, width=100, height=30):
    c = Console(file=io.StringIO(), width=width, height=height, force_terminal=True, color_system="truecolor")
    c.print(renderable)
    return c.file.getvalue()


def plain(renderable, width=100, height=30):
    c = Console(file=io.StringIO(), width=width, height=height, force_terminal=False, color_system=None)
    c.print(renderable)
    return c.file.getvalue()


def test_board_snapshot_contains_every_box():
    out = plain(board.render_board(sample_state()))
    for needle in ("LINES TODAY", "1,284", "WORDS", "38,910", "AUDIO MINUTES", "271", "QUEUE / TIME LEFT", "12,406",
                   "~ 3 h 12 m", "GPU HEALTH", "RTX 3070 Ti", "Utilisation", "92%", "68 C", "4.5/8 GB", "NOW MAKING",
                   "68%", "RECENT EVENTS", "done", "retry", "picked up again", "[P] pause", "[S] stop and exit",
                   "[M] menu"):
        assert needle in out, needle
    assert "▁" in out or "█" in out
    assert "┌" in out                                     # the mock-up's square corners


def test_board_uses_true_colour_gold():
    assert "38;2;233;187;77" in render(board.render_board(sample_state()))


def test_board_when_paused_and_without_gpu_or_queue():
    s = sample_state() | {"state": "paused", "gpu": None, "queue": None, "time_left": None, "events": []}
    out = plain(board.render_board(s))
    assert "Paused" in out and "[P] resume" in out and "Nothing finished yet." in out


def test_menu_and_banner_wording():
    out = plain(board.menu_screen(), width=80)
    for needle in ("[1] Start now", "[2] Install as a service (runs when your PC is idle)", "[3] Settings", "[Q] Quit",
                   "Lend your graphics card to the voice-line queue.",
                   "█████"):
        assert needle in out, needle
    assert "Blizzard" not in out                    # owner, 2026-11-12: not on the menu (the README still carries the line)
    low = out.lower() + plain(board.render_board(sample_state())).lower()
    for bad in (" ai ", "clone", "generated", "synthesis", "reference"):
        assert bad not in low


def test_setup_board_renders_ticks_and_bar():
    out = plain(board.render_setup(list(setup.STEPS), setup.LABELS, {"gpu", "folders"}, "model", 0.57, "x", "3.4 of 6.0 GB"))
    assert "✓" in out and "●" in out and "57%" in out and "3.4 of 6.0 GB" in out


def test_summarize_log_events():
    now = time.time()
    t = lambda off: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - off))
    lines = [f"{t(300)} claim job=job12345678 attempt=1 lines=10",
             f"{t(120)} complete job=job12345678 ok=9 held=1 seconds=42.0 words=300 audio=120",
             f"{t(60)} claim job=job22222222 attempt=1 lines=5"]
    ev = [stats.parse_line(x) for x in lines]
    s = stats.summarize(ev, now)
    assert s["lines"] == 10 and s["words"] == 300 and s["audio_min"] == 2 and s["making"] and s["job"] == "job22222222"
    assert s["events"][0][1] == "done"


# ------------------------------------------------------------------ service / idle
def test_windows_task_xml_and_commands():
    prog, args = service.launch_command(frozen=True, executable=r"C:\Apps\SpeakStoneWorker.exe")
    xml = service.build_task_xml(prog, args)
    assert "LogonTrigger" in xml and "--background" in xml and r"C:\Apps\SpeakStoneWorker.exe" in xml
    assert "LeastPrivilege" in xml and "<Hidden>true</Hidden>" in xml
    # the task starts the app through the console host in headless mode, so no empty black window opens at log-on
    wprog, wargs = service.windowless(r"C:\My Apps\SpeakStoneWorker.exe", ["--background"], {"SystemRoot": r"D:\Win"})
    assert wprog == r"D:\Win\System32\conhost.exe" and wargs == ["--headless", r"C:\My Apps\SpeakStoneWorker.exe", "--background"]
    xml = service.build_task_xml(wprog, wargs)
    assert r"<Command>D:\Win\System32\conhost.exe</Command>" in xml
    assert r'--headless "C:\My Apps\SpeakStoneWorker.exe" --background' in xml.replace("&quot;", '"')


def test_install_windows_calls_schtasks_only(tmp_path):
    calls = []

    class R:
        returncode = 0

    exe = tmp_path / "share" / "SpeakStoneWorker.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"x")
    ok = service.install(tmp_path, runner=lambda c, **k: calls.append(c) or R(), platform="win32", frozen=True,
                         executable=str(exe), app_target=tmp_path / "app" / "SpeakStoneWorker.exe")
    assert ok and calls[0][:3] == ["schtasks", "/Create", "/TN"] and calls[0][3] == service.TASK_NAME and "/XML" in calls[0]
    assert (tmp_path / "task.xml").exists()
    calls.clear()
    assert service.uninstall(runner=lambda c, **k: calls.append(c) or R(), platform="win32")
    assert ["schtasks", "/Delete", "/TN", service.TASK_NAME, "/F"] in calls


def test_install_linux_writes_unit_and_enables(tmp_path):
    calls = []

    class R:
        returncode = 0

    ok = service.install(tmp_path / "d", runner=lambda c, **k: calls.append(c) or R(), platform="linux",
                         unit_dir=tmp_path / "units", frozen=False, executable="/usr/bin/python3")
    unit = (tmp_path / "units" / service.UNIT_NAME).read_text()
    assert ok and "ExecStart=/usr/bin/python3 -m speakstone_worker --background" in unit and "Nice=10" in unit
    assert ["systemctl", "--user", "enable", "--now", service.UNIT_NAME] in calls


def test_idle_decide():
    d = idle.decide
    assert d("idle", False, 700, 10, False, False, False) == "start"
    assert d("idle", False, 700, 10, True, False, False) == "keep"          # full-screen app
    assert d("idle", False, 700, 10, False, True, False) == "keep"          # battery
    assert d("idle", True, 3, 10, False, False, False) == "stop"            # user is back
    assert d("idle", True, 700, 10, False, False, True) == "stop"           # paused
    assert d("idle", False, 100, 10, False, False, False) == "keep"


def test_pause_window_overnight():
    assert in_pause_window(23 * 60, "22:00", "06:00") and in_pause_window(3 * 60, "22:00", "06:00")
    assert not in_pause_window(12 * 60, "22:00", "06:00") and not in_pause_window(5, "", "")


def test_runner_stop_uses_stop_file_and_hides_key(tmp_path):
    from speakstone_worker.runner import WorkerRunner

    class P:
        def poll(self):
            return None

    seen = {}

    def popen(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return P()

    r = WorkerRunner(tmp_path, popen=popen)
    os.environ["SSW_WORKER_KEY"] = KEY
    try:
        r.start()
    finally:
        del os.environ["SSW_WORKER_KEY"]
    assert "SSW_WORKER_KEY" not in seen["env"] and KEY not in " ".join(seen["cmd"])
    r.request_stop()
    assert (tmp_path / "work" / "STOP").exists()


# ------------------------------------------------------------------ first-job warm-up
def test_extras_step_prefetches_engine_files_into_install_cache(tmp_path):
    calls = []
    setup.run_setup(make_env(tmp_path, calls, []))
    cmd, e = [(c, e) for c, e in calls if "hf_hub_download(" in " ".join(map(str, c))][0]
    code = cmd[-1]
    for needle in ("facebook/w2v-bert-2.0", "amphion/MaskGCT", "semantic_codec/model.safetensors", "funasr/campplus",
                   "campplus_cn_common.bin", "nvidia/bigvgan_v2_22khz_80band_256x"):
        assert needle in code, needle
    assert e["HF_HUB_ENABLE_HF_TRANSFER"] == "0" and e["HF_HOME"].endswith("hf")
    assert "torch" not in code and "cuda" not in code.lower()
    assert "extras" in setup.STEPS and "2 GB" in setup.LABELS["extras"]


def test_runner_uses_install_hf_cache(tmp_path):
    from speakstone_worker.runner import WorkerRunner
    assert WorkerRunner(tmp_path).environment({})["HF_HOME"] == str(tmp_path / "hf")


def test_board_getting_ready_while_idle_gpu_and_no_lines():
    s = sample_state()
    s.update(job_lines=0)
    s["gpu"] = dict(s["gpu"], util_pct=3)
    out = plain(board.render_board(s))
    assert "Getting ready: first job loads the voice engine (a minute or two)" in out
    s["gpu"]["util_pct"] = 80
    assert "Getting ready" not in plain(board.render_board(s))
    assert "Getting ready" not in plain(board.render_board(sample_state()))


# ------------------------------------------------------------------ closing the window must not orphan the worker
class FakeK32:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok
        self.CreateJobObjectW = type("F", (), {"restype": None})()
        self.OpenProcess = type("F", (), {"restype": None})()

    def _f(self, name, ret):
        def f(*a):
            self.calls.append((name, a))
            return ret
        return f

    def __getattr__(self, name):
        raise AttributeError(name)


def make_k32(ok=True):
    k = FakeK32(ok)
    flags = {}

    def create(*a):
        k.calls.append(("create", a))
        return 77
    create.restype = None
    k.CreateJobObjectW = create

    def setinfo(job, cls, ref, size):
        import ctypes
        flags["cls"] = cls
        flags["flags"] = ctypes.cast(ref, ctypes.POINTER(ctypes.c_int64 * 4)).contents[2] & 0xFFFFFFFF
        return 1 if ok else 0
    k.SetInformationJobObject = setinfo
    k.flags = flags
    return k


def test_job_object_is_kill_on_close():
    from speakstone_worker import runner as r
    k = make_k32()
    assert r.create_kill_on_close_job(k) == 77
    assert k.flags["cls"] == 9 and k.flags["flags"] & 0x2000
    assert r.create_kill_on_close_job(make_k32(ok=False)) is None


def test_assign_to_job_uses_process_handle():
    from speakstone_worker import runner as r
    k = FakeK32()
    k.OpenProcess = lambda *a: 5
    k.OpenProcess.restype = None
    k.AssignProcessToJobObject = lambda j, h: 1
    k.CloseHandle = lambda h: 1
    assert r.assign_to_job(77, 1234, k) is True


def test_close_events_cover_close_logoff_shutdown():
    from speakstone_worker import runner as r
    assert set(r.CLOSE_EVENTS) == {2, 5, 6}


class _Proc:
    def __init__(self, pid=4242):
        self.pid, self.alive, self.killed = pid, True, False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False


def test_shutdown_fast_writes_stop_first_then_kills_leftovers(tmp_path, monkeypatch):
    from speakstone_worker import runner as r
    killed = []
    monkeypatch.setattr(r, "kill_pid_tree", lambda pid: killed.append(pid))
    t = [0.0]
    w = r.WorkerRunner(tmp_path, popen=lambda *a, **k: _Proc(), clock=lambda: t[0])
    w.proc = _Proc()
    w.shutdown_fast(wait=1.0, sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert (tmp_path / "work" / "STOP").exists()
    assert killed == [4242]                      # still alive after the wait, so ended
    killed.clear()
    w.proc.alive = False
    w.shutdown_fast(wait=1.0, sleep=lambda s: None)
    assert killed == []


def test_existing_worker_detected_by_pid_file(tmp_path, monkeypatch):
    from speakstone_worker import runner as r
    w = r.WorkerRunner(tmp_path)
    assert w.find_existing() is None
    (tmp_path / "work").mkdir()
    w.pid_file.write_text("999", encoding="utf-8")
    monkeypatch.setattr(r, "pid_is_ours", lambda pid, py: pid == 999)
    assert w.find_existing() == 999
    w.adopt(999)
    assert w.running
    monkeypatch.setattr(r, "pid_is_ours", lambda pid, py: False)
    assert w.find_existing() is None and not w.pid_file.exists()
    assert not w.running


def test_start_records_pid_and_new_session_on_linux(tmp_path, monkeypatch):
    from speakstone_worker import runner as r
    seen = {}

    def popen(cmd, **kw):
        seen.update(kw)
        return _Proc(31337)
    monkeypatch.setattr(r.sys, "platform", "linux")
    w = r.WorkerRunner(tmp_path, popen=popen)
    assert w.start()
    assert seen.get("start_new_session") is True
    assert w.pid_file.read_text() == "31337"


def test_pid_is_ours_rejects_dead_or_other_pid(tmp_path):
    from speakstone_worker import runner as r
    assert r.pid_is_ours(2 ** 22 + 12345, tmp_path / "python") is False


# ------------------------------------------------------------------ two workers on one card (one key each)
def gpu_rd(total, used=0):
    return {"name": "RTX 3080 Ti Laptop", "mem_total_mib": total, "mem_used_mib": used, "util_pct": 0, "temp_c": 50}


def test_second_worker_gate():
    ok, _ = setup.second_worker_allowed(gpu_rd(16384, 1200))
    assert ok
    assert not setup.second_worker_allowed(gpu_rd(8192))[0]
    assert not setup.second_worker_allowed(gpu_rd(12288, 4000))[0]          # only 8 GB free
    assert setup.second_worker_allowed(gpu_rd(12288, 1500))[0]
    assert not setup.second_worker_allowed(None)[0]


def test_enroll_second_uses_own_name_key_config_and_work_dir(tmp_path):
    calls, names = [], []
    env = make_env(tmp_path, calls, [])
    env.install.mkdir(parents=True, exist_ok=True)
    env.machine_name = "LAPTOP"

    def enr(machine_name=None):
        names.append(machine_name)
        return {"key": KEY, "label": "Calm Heron", "worker_id": "w2", "tier": "public"}
    env.enroll = enr
    orig = env.run

    def run(cmd, e, cwd, on_line):
        c = [str(x) for x in cmd]
        if "init" in c:
            (env.install / "worker_config_2.json").write_text("{}")
        calls.append((c, dict(e)))
        return 0
    env.run = run
    setup.enroll_second(env)
    init = [(c, e) for c, e in calls if "init" in c][0]
    assert names == ["LAPTOP-2"]
    assert "worker_config_2.json" in " ".join(init[0]) and "work_2" in " ".join(init[0])
    assert init[1]["SSW_WORKER_KEY"] == KEY and KEY not in " ".join(init[0])
    setup.enroll_second(env)                                               # key kept: no second sign-up
    assert names == ["LAPTOP-2"]


def test_set_workers_gate_toggle_and_429_message(tmp_path, monkeypatch):
    from speakstone_worker import cli, settings
    monkeypatch.setattr(setup, "artifact_ok", lambda install, name: True)     # the state file alone is not enough now
    st = settings.Settings(tmp_path / "cli.json")
    assert st["workers"] == 1
    assert "12 GB" in cli.set_workers(None, tmp_path, st, gpu_reader=lambda: gpu_rd(8192))
    assert st["workers"] == 1
    # not set up -> asks for setup first
    assert "setup" in cli.set_workers(None, tmp_path, st, gpu_reader=lambda: gpu_rd(16384, 100))
    (tmp_path / "setup_state.json").write_text(json.dumps({"done": [s for s in setup.STEPS if s != "gpu"]}))

    def boom(install):
        raise enroll.EnrollError(enroll.friendly_error(429))
    assert "Too many sign-ups" in cli.set_workers(None, tmp_path, st, gpu_reader=lambda: gpu_rd(16384, 100), enroll_runner=boom)
    assert st["workers"] == 1
    assert "Two workers" in cli.set_workers(None, tmp_path, st, gpu_reader=lambda: gpu_rd(16384, 100), enroll_runner=lambda i: None)
    assert st["workers"] == 2
    assert "One worker" in cli.set_workers(None, tmp_path, st)
    assert settings.Settings(tmp_path / "cli.json")["workers"] == 1


def test_group_runs_two_with_separate_dirs_and_stops_both(tmp_path, monkeypatch):
    from speakstone_worker import runner as r
    (tmp_path / "worker_config.json").write_text('{"worker_key": "test-key"}')
    (tmp_path / "worker_config_2.json").write_text("{}")
    cmds, pids = [], iter([111, 222])

    def popen(cmd, **kw):
        cmds.append(cmd)
        return _Proc(next(pids))
    g = r.WorkerGroup(tmp_path, 2, popen=popen)
    assert g.start() and len(cmds) == 2
    assert cmds[0][-1].endswith("worker_config.json") and cmds[1][-1].endswith("worker_config_2.json")
    assert (tmp_path / "work" / "worker.pid").read_text() == "111"
    assert (tmp_path / "work_2" / "worker.pid").read_text() == "222"
    g.request_stop()
    assert (tmp_path / "work" / "STOP").exists() and (tmp_path / "work_2" / "STOP").exists()
    killed = []
    monkeypatch.setattr(r, "kill_pid_tree", lambda pid: killed.append(pid))
    g.shutdown_fast(wait=0.0, sleep=lambda s: None)
    assert sorted(killed) == [111, 222]
    monkeypatch.setattr(r, "pid_is_ours", lambda pid, py: True)
    assert r.WorkerGroup(tmp_path, 2).find_existing() == {1: 111, 2: 222}


def test_group_with_one_ignores_second_but_still_knows_it(tmp_path, monkeypatch):
    from speakstone_worker import runner as r
    (tmp_path / "worker_config.json").write_text('{"worker_key": "test-key"}')
    (tmp_path / "worker_config_2.json").write_text("{}")
    cmds = []
    g = r.WorkerGroup(tmp_path, 1, popen=lambda c, **k: cmds.append(c) or _Proc(5))
    g.start()
    assert len(cmds) == 1
    (tmp_path / "work_2").mkdir()
    (tmp_path / "work_2" / "worker.pid").write_text("999")
    monkeypatch.setattr(r, "pid_is_ours", lambda pid, py: pid == 999)
    assert g.find_existing() == {2: 999}


def test_second_without_key_falls_back_to_one(tmp_path):
    from speakstone_worker import runner as r
    (tmp_path / "worker_config.json").write_text('{"worker_key": "test-key"}')
    assert r.WorkerGroup(tmp_path, 2).count == 1


def test_board_sums_both_workers_and_shows_header(tmp_path):
    from speakstone_worker import cli
    now = time.time()
    for d, n in (("work", 3), ("work_2", 4)):
        (tmp_path / d / "logs").mkdir(parents=True)
        t = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 60))
        (tmp_path / d / "logs" / "worker_x.log").write_text(
            f"{t} complete job=aaaaaaaa ok={n} held=0 words={n * 10} audio=30 seconds=20\n")
    one = cli.board_state(tmp_path, None, None, "idle", now, now=now, workers=1)
    two = cli.board_state(tmp_path, None, None, "idle", now, now=now, workers=2)
    if one["lines"] == 3:                                    # log-line format matched
        assert two["lines"] == 7 and two["workers"] == 2
    s = sample_state()
    s["workers"] = 2
    assert "2 workers" in plain(board.render_board(s))
    assert "2 workers" not in plain(board.render_board(sample_state()))


def test_merge_summaries_adds_up():
    a = stats.summarize([], 1000.0)
    b = stats.summarize([], 1000.0)
    a.update(lines=3, words=30, spark=[1.0] * stats.BUCKETS, making=True, job="j", job_lines=2, job_started=5.0)
    b.update(lines=4, words=40, spark=[2.0] * stats.BUCKETS)
    m = stats.merge_summaries([a, b])
    assert m["lines"] == 7 and m["words"] == 70 and m["spark"][0] == 3.0 and m["making"] and m["job"] == "j"


# ------------------------------------------------------------------ smoothed GPU utilisation
def test_rolling_avg_window():
    assert stats.rolling_avg([], 100) is None
    s = [(80, 50), (91, 93), (95, 96), (99, 94)]
    assert stats.rolling_avg(s, 100) == pytest.approx((93 + 96 + 94) / 3)      # the 50 at t=80 is outside 10 s
    assert stats.rolling_avg(s, 100, window=30) == pytest.approx((50 + 93 + 96 + 94) / 4)


def test_gpu_monitor_averages_and_3d_optional():
    t = [0.0]
    seq = iter([93, 96, 20, 95])
    mon = stats.GpuMonitor(reader=lambda: {"util_pct": next(seq), "temp_c": 1, "mem_used_mib": 1, "mem_total_mib": 2,
                                           "name": "x"},
                           d3_reader=lambda: 62.4, clock=lambda: t[0], windows=True, start_thread=False)
    out = []
    for i in range(4):
        t[0] = i * 2.0
        mon.sample3d()
        out.append(mon.read())
    assert out[0]["util_pct"] == 93 and out[1]["util_pct"] == 94 and out[3]["util_pct"] == 76
    assert out[3]["util3d_pct"] == 62
    off = stats.GpuMonitor(reader=lambda: {"util_pct": 50, "temp_c": 1, "mem_used_mib": 1, "mem_total_mib": 2, "name": "x"},
                           d3_reader=lambda: None, clock=lambda: 1.0, windows=True, start_thread=False)
    off.sample3d()
    assert "util3d_pct" not in off.read() and off.d3_ok is False


def test_parse_typeperf_3d_sums_per_luid_and_hides_on_failure():
    def col(luid, pid):
        return rf'"\\PC\GPU Engine(pid_{pid}_luid_0x00000000_0x0000{luid}_phys_0_eng_0_engtype_3D)\Utilization Percentage"'
    txt = '"(PDH-CSV 4.0)",' + ",".join([col("AAAA", 1), col("AAAA", 2), col("BBBB", 3)]) + chr(10)
    txt += '"11/11/2026 10:00:00.000","40.5","20.5","5"' + chr(10)
    assert stats.parse_typeperf_3d("Error: no counters") is None
    assert stats.read_3d(runner=lambda *a, **k: (_ for _ in ()).throw(OSError())) is None
    assert "3D" in plain(board.render_board(dict(sample_state(), gpu=dict(sample_state()["gpu"], util3d_pct=62))))


# ------------------------------------------------------------------ screen transitions
def test_menu_is_one_panel_that_fits_small_windows():
    for w, h in ((100, 30), (80, 24)):
        out = plain(board.menu_panel(False), width=w, height=h)
        lines = out.rstrip("\n").split("\n")
        assert len(lines) <= h, (w, h, len(lines))
        assert all(len(ln) <= w for ln in lines)
        assert "[1] Start now" in out and "[Q] Quit" in out


def test_board_and_setup_render_in_small_windows():
    for w, h in ((100, 30), (80, 24), (60, 20)):
        plain(board.render_board(sample_state()), width=w, height=h)
        out = plain(board.render_setup(list(setup.STEPS), setup.LABELS, {"gpu"}, "model", 0.3, "x"), width=w, height=h)
        assert "FIRST-TIME SETUP" in out


def test_fresh_screen_clears_shows_cursor_and_drops_keys(monkeypatch):
    from speakstone_worker import cli
    dropped = []
    monkeypatch.setattr(cli, "drain", lambda: dropped.append(1))
    c = Console(file=io.StringIO(), width=100, height=30, force_terminal=True, color_system=None)
    c.print("old text")
    c.show_cursor(False)
    cli.fresh_screen(c)
    out = c.file.getvalue()
    assert dropped == [1] and "\x1b[2J" in out and out.endswith("\x1b[?25h") or "\x1b[?25h" in out


def test_drain_empties_pending_keys_on_windows(monkeypatch):
    import types
    from speakstone_worker import keys
    pending = list("abc")
    fake = types.SimpleNamespace(kbhit=lambda: bool(pending), getwch=lambda: pending.pop(0))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(keys.sys, "platform", "win32")
    keys.drain()
    assert pending == []


def test_live_boards_use_the_alternate_screen():
    src = (Path(__file__).resolve().parents[1] / "speakstone_worker" / "cli.py").read_text(encoding="utf-8")
    assert src.count('screen=VT["on"]') == 2        # live board and setup board, only when ANSI works


class _K32:
    def __init__(self, set_ok=True):
        self.modes, self.set_ok = {}, set_ok

    def GetStdHandle(self, n):
        return n

    def GetConsoleMode(self, h, ref):
        ref._obj.value = 0x0003
        return 1

    def SetConsoleMode(self, h, m):
        self.modes[h] = m
        return 1 if self.set_ok else 0


def test_enable_vt_sets_output_and_input_flags():
    from speakstone_worker import cli
    k = _K32()
    assert cli.enable_vt(k) is True
    assert k.modes[-11] == 0x0003 | 0x0004 and k.modes[-10] == 0x0003 | 0x0200
    assert cli.enable_vt(_K32(set_ok=False)) is False


def test_fresh_screen_does_a_hard_cls_on_windows(monkeypatch):
    from speakstone_worker import cli
    ran = []
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.subprocess, "run", lambda c, **k: ran.append(c))
    monkeypatch.setattr(cli, "drain", lambda: None)
    cli.fresh_screen(Console(file=io.StringIO(), width=80, height=24))
    assert ran == [["cmd", "/c", "cls"]]


# ------------------------------------------------------------------ no per-job downloads
def fake_hf_cache(install, complete=True):
    hub = Path(install) / "hf" / "hub"
    spec = {"models--facebook--w2v-bert-2.0": ["config.json", "preprocessor_config.json", "model.safetensors"],
            "models--nvidia--bigvgan_v2_22khz_80band_256x": ["config.json", "bigvgan_generator.pt"],
            "models--amphion--MaskGCT": ["semantic_codec/model.safetensors"],
            "models--funasr--campplus": ["campplus_cn_common.bin"]}
    for i, (repo, files) in enumerate(spec.items()):
        for f in files[: (len(files) if complete or i else 0)]:
            p = hub / repo / "snapshots" / "abc" / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")


def test_extras_cached_and_offline_env(tmp_path):
    from speakstone_worker.runner import WorkerRunner
    assert setup.extras_cached(tmp_path) is False
    fake_hf_cache(tmp_path)
    assert setup.extras_cached(tmp_path) is True
    e = WorkerRunner(tmp_path).environment({})
    assert e["HF_HOME"] == str(tmp_path / "hf") and "HF_HUB_OFFLINE" not in e
    # both workers get the same cache and the same model folder
    assert WorkerRunner(tmp_path, index=2).environment({})["HF_HOME"] == e["HF_HOME"]
    assert WorkerRunner(tmp_path, index=2).environment({})["INDEXTTS2_DIR"] == e["INDEXTTS2_DIR"]


def test_partial_cache_is_not_cached(tmp_path):
    fake_hf_cache(tmp_path, complete=False)
    assert setup.extras_cached(tmp_path) is False


def test_synth_env_keeps_hf_home_and_offline():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import net_worker
    cfg = net_worker.Config()
    cfg.model_dir, cfg.worker_key = "M", ""
    w = net_worker.Worker.__new__(net_worker.Worker)
    w.cfg = cfg
    old = {k: os.environ.get(k) for k in ("HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    os.environ.update(HF_HOME="C:/inst/hf", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    try:
        env = w.synth_env()
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    assert env["HF_HOME"] == "C:/inst/hf" and "HF_HUB_OFFLINE" not in env


def test_forget_extras_then_setup_state(tmp_path):
    (tmp_path / "setup_state.json").write_text(json.dumps({"done": ["model", "extras"]}))
    setup.forget_extras(tmp_path)
    assert json.loads((tmp_path / "setup_state.json").read_text())["done"] == ["model"]


def _worker_obj():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import net_worker
    cfg = net_worker.Config()
    cfg.model_dir, cfg.worker_key = "M", ""
    w = net_worker.Worker.__new__(net_worker.Worker)
    w.cfg = cfg
    return net_worker, w


def test_never_offline_and_hf_urls_stay_readable(tmp_path, monkeypatch):
    nw, w = _worker_obj()
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_HOME", "C:/inst/hf")
    e = w.synth_env()
    assert "HF_HUB_OFFLINE" not in e and "TRANSFORMERS_OFFLINE" not in e and e["HF_HOME"] == "C:/inst/hf"
    log = nw.Log(tmp_path, key="", echo=False)
    msg = "We couldn't connect to 'https://huggingface.co/funasr/campplus/resolve/main/x.bin' and https://evil.example/a"
    assert "huggingface.co/funasr/campplus" in log._clean(msg) and "evil.example" not in log._clean(msg)
    from speakstone_worker.runner import WorkerRunner
    assert "HF_HUB_OFFLINE" not in WorkerRunner(tmp_path).environment({"HF_HUB_OFFLINE": "1"})


def test_sync_worker_files_replaces_stale_install_copy(tmp_path):
    res, inst = tmp_path / "res", tmp_path / "inst"
    res.mkdir()
    inst.mkdir()
    for n in setup.WORKER_FILES:
        (res / n).write_text("new " + n)
    (inst / "net_worker.py").write_text("old")
    assert sorted(setup.sync_worker_files(inst, res)) == sorted(setup.WORKER_FILES)
    assert (inst / "net_worker.py").read_text() == "new net_worker.py"
    assert setup.sync_worker_files(inst, res) == []                        # nothing to do the second time


def test_run_end_error_shows_in_recent_events():
    now = time.time()
    ev = [(now - 5, "claim", {"job": "abcdef123456", "lines": "3"}),
          (now - 3, "run_end", {"job": "abcdef123456", "exit": "1", "error": "OSError: campplus_cn_common.bin missing"}),
          (now - 2, "offline_retry", {"job": "abcdef123456", "error": "LocalEntryNotFoundError"})]
    s = stats.summarize(ev, now)
    texts = " ".join(e[2] + " " + e[3] for e in s["events"])
    assert "OSError: campplus_cn_common.bin missing" in texts


# ---- model cache folder (every job fetched the extra model files again, 2026-11-12) ----
def _engine_funcs():
    import ast
    src = (Path(__file__).resolve().parents[2] / "indextts2_worker.py").read_text(encoding="utf-8")
    ns = {"os": os, "Path": Path}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in ("hf_cache_dir", "pin_hf_cache"):
            exec(compile(ast.Module([node], []), "indextts2_worker.py", "exec"), ns)
    return src, ns


def test_model_cache_is_absolute_and_pinned_before_the_engine_import(tmp_path):
    src, ns = _engine_funcs()
    env = {"HF_HOME": str(tmp_path / "hf")}
    got = ns["pin_hf_cache"](env)
    assert got == str((tmp_path / "hf" / "hub").resolve()) and env["HF_HUB_CACHE"] == got and Path(got).is_absolute()
    assert ns["pin_hf_cache"]({"HF_HOME": "x", "HF_HUB_CACHE": str(tmp_path / "c")}) == str((tmp_path / "c").resolve())
    assert ns["pin_hf_cache"]({}) is None
    assert src.index("HF_CACHE = pin_hf_cache()") < src.index("import indextts.infer_v2")


def test_runner_sets_an_absolute_model_cache(tmp_path):
    from speakstone_worker.runner import WorkerRunner
    e = WorkerRunner(tmp_path).environment({})
    assert e["HF_HUB_CACHE"] == str(tmp_path / "hf" / "hub")
    assert WorkerRunner(tmp_path, index=2).environment({})["HF_HUB_CACHE"] == e["HF_HUB_CACHE"]


# ---- remove the program ----
def test_remove_everything_deletes_only_an_install_folder(tmp_path):
    from speakstone_worker import uninstall
    other = tmp_path / "Documents"
    other.mkdir()
    (other / "a.txt").write_text("x")
    assert uninstall.remove_everything(other)["ok"] is False and (other / "a.txt").exists()
    inst = tmp_path / "SpeakStoneWorker"
    (inst / "hf" / "hub").mkdir(parents=True)
    (inst / "setup_state.json").write_text("{}")
    (inst / "hf" / "hub" / "big.bin").write_bytes(b"0" * 5000)
    order = []
    r = uninstall.remove_everything(inst, stop_workers=lambda: order.append("stop"),
                                    remove_service=lambda: order.append("service"))
    assert r["ok"] and r["freed"] >= 5000 and not inst.exists() and order == ["stop", "service"]


def test_remove_program_needs_the_word_delete(tmp_path):
    from speakstone_worker import cli
    con = Console(file=io.StringIO(), width=120)
    calls = []
    assert cli.remove_program(con, tmp_path, ask=lambda _p: "yes", remover=lambda *a, **k: calls.append(1),
                              sleep=lambda _s: None) is False
    assert not calls
    assert cli.remove_program(con, tmp_path, ask=lambda _p: "DELETE",
                              remover=lambda *a, **k: {"ok": True, "freed": 2e9, "left": ""}) is True
    assert "2.0 GB freed" in con.file.getvalue()


# ------------------------------------------------------------------ stop kills the tree; background restarts
def test_poll_stop_ends_the_whole_process_tree(tmp_path, monkeypatch):
    from speakstone_worker import runner as rn
    killed = []
    monkeypatch.setattr(rn, "kill_pid_tree", lambda pid: killed.append(pid))

    class P:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            killed.append("terminate")
    now = [1.0]
    r = rn.WorkerRunner(tmp_path, clock=lambda: now[0])
    r.proc = P()
    r.request_stop()
    now[0] = 10
    assert r.poll_stop(grace=120) is False and killed == []
    now[0] = 500
    assert r.poll_stop(grace=120) is False and killed == [4242]


class FakeRunner:
    def __init__(self):
        self.running, self.exit_code, self.stop_asked_at, self.nice, self.starts = False, None, 0.0, False, 0

    def start(self):
        self.starts += 1
        self.running, self.exit_code = True, None

    def request_stop(self):
        pass

    def poll_stop(self, grace=120):
        return not self.running

    def shutdown_fast(self):
        pass

    def stop_and_wait(self):
        pass


def _tick(runner, gate):
    from speakstone_worker import cli
    from speakstone_worker.settings import Settings
    st = {"idle_minutes": 1, "pause_from": "00:00", "pause_to": "00:00", "block_fullscreen": False,
          "block_on_battery": False}
    rd = {"idle_seconds": lambda: 9999, "fullscreen": lambda: False, "battery": lambda: False}
    return cli.background_tick(runner, st, readers=rd, gate=gate)


def test_refused_key_exit_is_not_restarted_for_half_an_hour():
    from speakstone_worker import cli
    clock = [1000.0]
    gate, r = cli.RestartGate(clock=lambda: clock[0]), FakeRunner()
    assert _tick(r, gate) == "start" and r.starts == 1
    r.running, r.exit_code = False, 3                  # the worker quit: key refused
    assert _tick(r, gate) == "wait" and r.starts == 1
    clock[0] += 29 * 60
    assert _tick(r, gate) == "wait" and r.starts == 1
    clock[0] += 2 * 60
    assert _tick(r, gate) == "start" and r.starts == 2


def test_other_crashes_back_off_30_60_up_to_10_minutes():
    from speakstone_worker import cli
    clock = [0.0]
    gate = cli.RestartGate(clock=lambda: clock[0])
    waits = []
    for _ in range(7):
        gate.observe(True, None)
        t0 = clock[0]
        gate.observe(False, 1)
        waits.append(gate.until - t0)
    assert waits == [30, 60, 120, 240, 480, 600, 600]
    gate.observe(True, None)
    gate.observe(False, 0)                             # a clean exit resets it
    assert gate.crashes == 0


def test_stop_we_asked_for_is_not_a_crash():
    from speakstone_worker import cli
    gate = cli.RestartGate(clock=lambda: 0.0)
    r = FakeRunner()
    _tick(r, gate)
    r.running, r.exit_code, r.stop_asked_at = False, 1, 5.0
    _tick(r, gate)
    assert not gate.blocked()


def test_background_survives_a_failing_tick_and_logs_it(tmp_path, monkeypatch):
    from speakstone_worker import cli
    monkeypatch.setattr(cli, "hide_console", lambda: None)
    monkeypatch.setattr(setup, "is_set_up", lambda i: True)
    monkeypatch.setattr(setup, "extras_cached", lambda i: True)
    monkeypatch.setattr(setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli, "install_close_handlers", lambda f: None)
    calls = []

    def tick(runner, st, gate=None, control=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("odd")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "background_tick", tick)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "WorkerGroup", lambda install, n: FakeRunner())
    assert cli.background(tmp_path, {"workers": 1}, tray_start=lambda *a: None) == 0
    assert len(calls) == 2
    assert "RuntimeError: odd" in (tmp_path / "work" / "logs" / "background.log").read_text()


def test_batch_lines_are_grouped_by_voice_sample():
    """A voice's lines sit together (the engine redoes its voice setup on every change of sample), nothing is lost."""
    import ast
    src = (Path(__file__).resolve().parents[2] / "indextts2_worker.py").read_text(encoding="utf-8")
    ns = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "by_voice":
            exec(compile(ast.Module([node], []), "indextts2_worker.py", "exec"), ns)
    rows = [({"canonical_reference_clip": v, "sound_file": f"{i}.ogg"}, f"fp{i}") for i, v in enumerate("ABACBA")]
    got = ns["by_voice"](rows)
    assert [r["canonical_reference_clip"] for r, _ in got] == list("AAABBC")
    assert [r["sound_file"] for r, _ in got] == ["0.ogg", "2.ogg", "5.ogg", "1.ogg", "4.ogg", "3.ogg"]
    assert sorted(fp for _, fp in got) == sorted(fp for _, fp in rows)
    assert ns["by_voice"]([]) == []


def test_an_empty_config_file_is_not_a_finished_sign_up(tmp_path):
    """A PC had worker_config.json = {} with setup marked finished: the app never joined and never said why."""
    from speakstone_worker import setup
    cfg = tmp_path / "worker_config.json"
    assert not setup.config_has_key(cfg)
    cfg.write_text("{}", encoding="utf-8")
    assert not setup.config_has_key(cfg) and not setup.artifact_ok(tmp_path, "enroll")
    cfg.write_text("not json", encoding="utf-8")
    assert not setup.config_has_key(cfg)
    cfg.write_text('{"worker_key": "  "}', encoding="utf-8")
    assert not setup.config_has_key(cfg)
    cfg.write_text('{"worker_key": "k"}', encoding="utf-8")
    assert setup.config_has_key(cfg) and setup.artifact_ok(tmp_path, "enroll")
