"""Linux: programs the packaged app starts must not load the app's private libraries; and a PC that cannot make a
single line stops instead of taking job after job."""
import importlib.util
import sys
from pathlib import Path

from speakstone_worker import cli, paths

ROOT = Path(__file__).resolve().parents[2]


def load_worker():
    spec = importlib.util.spec_from_file_location("net_worker_linux", ROOT / "net_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_packaged_linux_app_gives_children_the_users_own_library_path():
    e = paths.clean_child_env({"LD_LIBRARY_PATH": "/tmp/_MEIabc", "LD_LIBRARY_PATH_ORIG": "/usr/local/cuda/lib64", "X": "1"},
                              frozen=True, windows=False)
    assert e["LD_LIBRARY_PATH"] == "/usr/local/cuda/lib64" and "LD_LIBRARY_PATH_ORIG" not in e and e["X"] == "1"


def test_packaged_linux_app_removes_its_private_path_when_the_user_had_none():
    e = paths.clean_child_env({"LD_LIBRARY_PATH": "/tmp/_MEIabc"}, frozen=True, windows=False)
    assert "LD_LIBRARY_PATH" not in e


def test_source_runs_and_windows_are_left_alone():
    src = {"LD_LIBRARY_PATH": "/opt/mine", "LD_LIBRARY_PATH_ORIG": "/x"}
    assert paths.clean_child_env(src, frozen=False, windows=False) == src
    assert paths.clean_child_env(src, frozen=True, windows=True) == src
    assert paths.clean_child_env(src, frozen=True, windows=False) is not src


def test_the_worker_and_setup_use_the_clean_environment():
    runner = (ROOT / "cli" / "speakstone_worker" / "runner.py").read_text(encoding="utf-8")
    setup = (ROOT / "cli" / "speakstone_worker" / "setup.py").read_text(encoding="utf-8")
    assert "paths.clean_child_env(base)" in runner
    assert "default_factory=lambda: _clean_env()" in setup and "env=_clean_env()" in setup


def test_brake_constants_and_exit_code():
    nw = load_worker()
    assert nw.EXIT_BROKEN == 7 and nw.DEAD_JOBS_LIMIT == 2 and nw.DEAD_JOB_WAIT_SECONDS >= 60
    assert 7 in cli.NO_RESTART_CODES                     # the service does not start a broken worker again at once
    assert "could not make a single line" in cli.BROKEN_SENTENCE


def test_a_job_with_no_line_made_is_marked_so_the_brake_can_count_it():
    text = (ROOT / "net_worker.py").read_text(encoding="utf-8")
    assert "fail.none_made = len(missing) == ctx.n" in text
    assert 'raise ExitRun(EXIT_BROKEN' in text and "self._dead_jobs = 0" in text
