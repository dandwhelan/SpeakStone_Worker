"""Where things live. Everything is per user; nothing needs administrator rights."""
import os
import sys
from pathlib import Path


def is_windows():
    return sys.platform == "win32"


def data_dir(env=None):
    env = os.environ if env is None else env
    if is_windows():
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SpeakStoneWorker"
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "speakstone-worker"


def resource_dir():
    """Where net_worker.py, indextts2_worker.py and runpod_worker.py sit (bundled, or the repo root)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "worker"
    return Path(__file__).resolve().parents[2]


def api_base(env=None):
    from . import DEFAULT_API_BASE
    env = os.environ if env is None else env
    return (env.get("SPEAKSTONE_API_BASE") or DEFAULT_API_BASE).rstrip("/")


APP_EXE_NAME = "SpeakStoneWorker.exe"
APP_BIN_NAME = "SpeakStoneWorker"


def app_dir(env=None):
    """The program's own folder on the local disk. The service and the updater use the copy in here, so a program
    started from a download folder or a network share is never what the log-on task points at."""
    return data_dir(env) / "app"


def app_exe(env=None):
    return app_dir(env) / (APP_EXE_NAME if is_windows() else APP_BIN_NAME)


def running_exe():
    """Path of the running one-file program, or None when running from source."""
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def assets_dir():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).resolve().parent / "assets"


def clean_child_env(env=None, frozen=None, windows=None):
    """Environment for a program this app starts. The packaged Linux program runs with LD_LIBRARY_PATH pointing at its
    own private libraries; a child that inherits that (the system ffmpeg, Python, nvidia-smi) loads the wrong
    libstdc++ and fails to start. On a rented Linux box every reference clip conversion died that way and every line
    of every job failed (2026-11-13). The packager keeps the user's own value in LD_LIBRARY_PATH_ORIG: put it back,
    or remove the variable when there was none. Windows and a run from source are left as they are. -> a new dict."""
    e = dict(os.environ if env is None else env)
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    windows = is_windows() if windows is None else windows
    if frozen and not windows:
        orig = e.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            e["LD_LIBRARY_PATH"] = orig
        else:
            e.pop("LD_LIBRARY_PATH", None)
    return e
