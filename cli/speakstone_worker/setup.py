"""First-run setup as small, resumable steps, for Windows and Linux.

Every outside effect (download, run a program, enrol, read the GPU) is passed in through `Env`, so tests run the
whole list with fakes. The worker key goes to `net_worker.py init` through SSW_WORKER_KEY in the child's
environment only: never a command line, never a log line, never a file of ours.

  uv (portable) -> Python 3.11 -> index-tts 830f6f8 -> packages -> torch (PINNED) -> model -> extras -> ffmpeg -> enrol -> check
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

INDEX_TTS_COMMIT = "830f6f8"
MIN_VRAM_MIB = 7900                      # 8 GB cards report 8192 MiB; 7900 leaves room for reserved memory
UV_URLS = {
    "win": "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip",
    "linux": "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz",
}
INDEX_TTS_URL = f"https://github.com/index-tts/index-tts/archive/{INDEX_TTS_COMMIT}.zip"
FFMPEG_URLS = {
    "win": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "linux": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
}
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
# The owner hit this: an unpinned `--reinstall torch` pulled 2.11, which the voice engine does not work with.
TORCH_PINS = ("torch==2.8.0+cu128", "torchaudio==2.8.0+cu128")
MODEL_REPO = "IndexTeam/IndexTTS-2"
# What the engine (commit 830f6f8, indextts/infer_v2.py + utils/maskgct_utils.py) fetches on its first job.
EXTRA_SNAPSHOTS = (
    ("facebook/w2v-bert-2.0", ["*.json", "*.safetensors", "*.txt"]),
    ("nvidia/bigvgan_v2_22khz_80band_256x", ["config.json", "bigvgan_generator.pt"]),
)
EXTRA_FILES = (
    ("amphion/MaskGCT", "semantic_codec/model.safetensors"),
    ("funasr/campplus", "campplus_cn_common.bin"),
)
# What must be present for each prefetched repo (one snapshot holding all of these files counts as complete).
EXTRAS_NEED = {"models--facebook--w2v-bert-2.0": ["config.json", "preprocessor_config.json", "model.safetensors"],
               "models--nvidia--bigvgan_v2_22khz_80band_256x": ["config.json", "bigvgan_generator.pt"],
               "models--amphion--MaskGCT": ["semantic_codec/model.safetensors"],
               "models--funasr--campplus": ["campplus_cn_common.bin"]}
MODEL_FILES = ("config.yaml", "gpt.pth", "s2mel.pth", "bpe.model")
TORCH_VERSION = "2.8.0+cu128"
WORKER_FILES = ("net_worker.py", "indextts2_worker.py", "runpod_worker.py")

# torch comes before the engine so the engine install finds the pinned build already there and cannot swap it.
STEPS = ("gpu", "folders", "worker_files", "uv", "python", "index_tts", "torch", "packages", "model", "extras", "ffmpeg",
         "enroll", "check")
LABELS = {
    "gpu": "Checking your graphics card",
    "folders": "Preparing the install folder",
    "worker_files": "Copying the worker",
    "uv": "Fetching the installer tool",
    "python": "Installing Python 3.11 (private copy)",
    "index_tts": "Fetching the voice engine",
    "packages": "Installing the voice engine",
    "torch": "Installing the graphics-card libraries",
    "model": "Downloading the voice model (about 5.5 GB)",
    "extras": "Fetching extra voice files (about 2 GB)",
    "ffmpeg": "Fetching audio tools",
    "enroll": "Joining the queue",
    "check": "Final check",
    "hf_hub": "Preparing the downloads",        # not in STEPS: a small helper install that starts the second lane
}
# Setup runs two lanes at once after the engine is fetched: the pip installs (torch is the big one) and the Hugging
# Face downloads. They touch different files and different servers, so overlapping them saves most of the wait.
LANE_A = ("torch", "packages")
LANE_B = ("model", "extras", "ffmpeg")
HF_HUB_SPEC = "huggingface_hub>=0.32,<1.0"      # 0.32 is the first with the fast transfer package built in
RUN_TRIES = 3                    # each setup command is tried this many times before setup stops
HF_FAST_ENV = {"HF_XET_HIGH_PERFORMANCE": "1"}   # setup-time only: use every core and the full link for the downloads


class SetupError(Exception):
    pass



def _clean_env():
    from . import paths
    return paths.clean_child_env()

@dataclass
class Env:
    install: Path
    resource_dir: Path
    download: callable = None                           # (url, dest_path, progress(frac, got, total)) -> None
    run: callable = None                                # (cmd list, env dict, cwd, on_line) -> exit code
    enroll: callable = None                             # () -> {"key","label","worker_id","tier"}
    gpu_total_mib: callable = None                      # () -> int | None
    progress: callable = field(default=lambda step, frac, msg: None)
    base_env: dict = field(default_factory=lambda: _clean_env())   # Linux: without the app's private libraries
    is_win: bool = field(default_factory=lambda: sys.platform == "win32")
    api_base: str = ""                                  # only passed to the worker when it is not the default
    machine_name: str = ""
    probe: callable = None                              # (cmd list, timeout seconds) -> (exit code, output text); None = real

    @property
    def _exe(self):
        return ".exe" if self.is_win else ""

    @property
    def tools(self):
        return self.install / "tools"

    @property
    def uv(self):
        return self.tools / ("uv" + self._exe)

    @property
    def venv(self):
        return self.install / "venv"

    @property
    def py(self):
        return self.venv / ("Scripts/python.exe" if self.is_win else "bin/python")

    @property
    def model_dir(self):
        return self.install / "IndexTTS-2"

    @property
    def engine_dir(self):
        return self.install / "index-tts"

    @property
    def ffmpeg_dir(self):
        return self.install / "ffmpeg"

    @property
    def work_dir(self):
        return self.install / "work"

    @property
    def constraints_file(self):
        return self.install / "constraints.txt"

    @property
    def state_file(self):
        return self.install / "setup_state.json"

    @property
    def config_file(self):
        return self.install / "worker_config.json"

    @property
    def work_dir_2(self):
        return self.install / "work_2"

    @property
    def config_file_2(self):
        return self.install / "worker_config_2.json"

    @property
    def enrolled_file_2(self):
        return self.install / "enrolled_2.json"

    @property
    def enrolled_file(self):
        return self.install / "enrolled.json"           # label / tier only, never the key

    def tool_env(self, extra=None):
        e = dict(self.base_env)
        e["UV_PYTHON_INSTALL_DIR"] = str(self.install / "python")
        e["UV_CACHE_DIR"] = str(self.install / "uv_cache")
        e["HF_HOME"] = str(self.install / "hf")
        e["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        # Package and model downloads now share the line. uv gives up on a file after 30 s without data, which the
        # model download can cause (first Linux test: the torch install failed that way, then passed by hand).
        e.setdefault("UV_HTTP_TIMEOUT", "300")
        e["PYTHONUNBUFFERED"] = "1"
        e["PATH"] = str(self.ffmpeg_dir) + os.pathsep + e.get("PATH", "")
        e.pop("SSW_WORKER_KEY", None)
        e.update(extra or {})
        return e


# ---------------------------------------------------------------- real effects
def real_download(url, dest, progress=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "SpeakStoneWorkerCLI"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            got += len(b)
            if progress:
                progress(got / total if total else None, got, total)
    os.replace(part, dest)


def real_probe(cmd, timeout):
    """Run a short command and hand back (exit code, its output). Never hangs: a timeout is a failure (-1)."""
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        r = subprocess.run([str(c) for c in cmd], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           env=_clean_env(), **kw)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return r.returncode, (r.stdout or b"").decode("utf-8", "replace")


SETUP_STALL_SECONDS = 7200      # a step that prints nothing for this long is stuck. Two hours: a big package on a slow line prints nothing while it downloads


def setup_stall_seconds(env=None):
    env = os.environ if env is None else env
    try:
        return max(1.0, float(env.get("SSW_SETUP_STALL_SECONDS") or SETUP_STALL_SECONDS))
    except (TypeError, ValueError):
        return float(SETUP_STALL_SECONDS)


def _kill_tree(p):
    """Kill a child and everything it started (Windows: taskkill /T, elsewhere: its process group). Never raises."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)
        else:
            import signal
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        p.kill()
        p.wait(timeout=15)
    except Exception:
        pass


def real_run(cmd, env, cwd, on_line, stall=None, clock=None, poll=0.5):
    """Run a step and pass each output line to on_line, as always. New: if the child prints nothing for `stall` seconds
    (default SETUP_STALL_SECONDS, SSW_SETUP_STALL_SECONDS overrides) its process tree is killed and SetupError says so,
    so a step cannot hang for ever. `clock` and `poll` are arguments so tests need not wait half an hour."""
    import queue
    import time
    stall = setup_stall_seconds() if stall is None else stall
    clock = clock or time.monotonic
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {"start_new_session": True}
    p = subprocess.Popen([str(c) for c in cmd], env=env, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", **kw)
    lines = queue.Queue()

    def pump():
        try:
            for line in p.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)
    threading.Thread(target=pump, daemon=True, name="step-output").start()
    stuck = SetupError(f"this step printed nothing for {_how_long(stall)} and was stopped; start setup again")
    last = clock()
    while True:
        try:
            line = lines.get(timeout=max(0.01, min(poll, stall)))
        except queue.Empty:
            if clock() - last >= stall:
                _kill_tree(p)
                raise stuck
            continue
        if line is None:
            break
        last = clock()
        if on_line:
            on_line(line.rstrip())
    try:
        return p.wait(timeout=stall)                    # output ended; a child that then never exits is stuck too
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        raise stuck


def _how_long(seconds):
    if seconds >= 120:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds)} seconds" if seconds != 1 else "1 second"


# ---------------------------------------------------------------- resume state
def load_state(env):
    try:
        return set(json.loads(env.state_file.read_text(encoding="utf-8")).get("done", []))
    except (OSError, ValueError):
        return set()


_STATE_LOCK = threading.Lock()


def save_state(env, done):
    # Two lanes finish steps at the same time; one writer at a time keeps the file whole.
    with _STATE_LOCK:
        env.install.mkdir(parents=True, exist_ok=True)
        env.state_file.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")


_PCT = re.compile(r"(\d{1,3})%")


def _run(env, step, cmd, extra_env=None, cwd=None):
    """Run a command. The command line is reported, the environment never is."""
    env.progress(step, None, "> " + " ".join(str(c) for c in cmd[:6]))

    def on_line(ln):
        frac = None
        if step in ("model", "extras"):                          # huggingface_hub's tqdm: "Fetching 20 files:  45%|..."
            m = _PCT.findall(ln)
            if m:
                frac = min(100, int(m[-1])) / 100
        env.progress(step, frac, ln[-200:])

    # Downloads fail for passing reasons (a stalled connection, a busy server). Every command here picks up where it
    # stopped (uv keeps finished files, huggingface_hub resumes part files), so trying again costs little.
    for attempt in range(1, RUN_TRIES + 1):
        rc = env.run(cmd, env.tool_env(extra_env), cwd, on_line)
        if rc == 0:
            return
        if attempt < RUN_TRIES:
            env.progress(step, None, f"That did not finish (exit {rc}). Trying again ({attempt + 1} of {RUN_TRIES})...")
    raise SetupError(f"{LABELS[step]} failed (exit {rc}). See the lines above.")


def _extract(archive, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    archive = str(archive)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.namelist():
                if not (root / m).resolve().is_relative_to(root):
                    raise SetupError("downloaded archive has an unsafe path")
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                if not (root / m.name).resolve().is_relative_to(root) or m.issym() or m.islnk():
                    raise SetupError("downloaded archive has an unsafe path")
            t.extractall(dest)


def _plat(env):
    return "win" if env.is_win else "linux"


# ---------------------------------------------------------------- steps
def step_gpu(env):
    mib = env.gpu_total_mib()
    if mib is None:
        raise SetupError("No NVIDIA graphics card was found (nvidia-smi did not answer). "
                         "Install the current NVIDIA driver and try again.")
    if mib < MIN_VRAM_MIB:
        raise SetupError(f"Your graphics card has {mib // 1024} GB of memory; at least 8 GB is needed.")
    env.progress("gpu", 1, f"graphics card memory: {mib} MiB")


def step_folders(env):
    for d in (env.install, env.tools, env.work_dir):
        d.mkdir(parents=True, exist_ok=True)
    # About 20 GB for a first install. Once the model is in place only the smaller pieces can be missing (the extra
    # voice files are about 2 GB, a lost Python with torch about 6 GB), so a repair run on a fuller drive still goes.
    need = 8 if model_complete(env.model_dir) else 20
    free = shutil.disk_usage(env.install).free
    if free < need * 1024 ** 3:
        raise SetupError(f"Only {free // 1024 ** 3} GB free on that drive; about {need} GB is needed.")


def step_worker_files(env):
    for n in WORKER_FILES:
        src = env.resource_dir / n
        if not src.exists():
            raise SetupError(f"{n} is missing from this app.")
        shutil.copy2(src, env.install / n)


def step_uv(env):
    if env.uv.exists():
        return
    z = env.tools / ("uv.zip" if env.is_win else "uv.tar.gz")
    env.download(UV_URLS[_plat(env)], z, lambda f, g=0, t=0: env.progress("uv", f, ""))
    _extract(z, env.tools / "uv_x")
    found = next((p for p in (env.tools / "uv_x").rglob("uv" + env._exe) if p.is_file()), None)
    if not found:
        raise SetupError("the installer tool was not in the download")
    shutil.copy2(found, env.uv)
    if not env.is_win:
        env.uv.chmod(0o755)
    shutil.rmtree(env.tools / "uv_x", ignore_errors=True)
    z.unlink(missing_ok=True)


def step_python(env):
    _run(env, "python", [env.uv, "python", "install", "3.11"])
    if not env.py.exists():
        _run(env, "python", [env.uv, "venv", env.venv, "--python", "3.11"])


def step_index_tts(env):
    if _engine_ok(env.install):
        return
    z = env.install / "index-tts.zip"
    env.download(INDEX_TTS_URL, z, lambda f, g=0, t=0: env.progress("index_tts", f, ""))
    _extract(z, env.install / "_it")
    sub = next((p for p in (env.install / "_it").iterdir() if p.is_dir()), None)
    if not sub:
        raise SetupError("the voice engine download was empty")
    shutil.rmtree(env.engine_dir, ignore_errors=True)
    shutil.move(str(sub), str(env.engine_dir))
    shutil.rmtree(env.install / "_it", ignore_errors=True)
    z.unlink(missing_ok=True)


def write_constraints(env):
    """The two torch pins in a file every `uv pip install` is given with -c, so no later install can move them."""
    env.install.mkdir(parents=True, exist_ok=True)
    env.constraints_file.write_text("\n".join(TORCH_PINS) + "\n", encoding="utf-8")


def _pip(env, *args, index=None):
    """`uv pip install` for the install's own Python, always held to the torch pins. Only the torch install itself names
    the cu128 index: uv takes a package from the first index that has it, and that index also carries a few old builds
    of common packages (numpy, pillow), so listing it for the other installs could make them fail to resolve. Torch is
    installed first, so the pin is already met when the engine is installed; if it ever is not, the install stops
    instead of fetching a different torch."""
    write_constraints(env)
    cmd = [env.uv, "pip", "install", "--python", env.py, "-c", env.constraints_file, *args]
    return cmd + [index, TORCH_INDEX] if index else cmd


def torch_version(env):
    """Version string of the torch in the install's Python, or None when it is missing or will not import."""
    if not env.py.exists():
        return None
    lines = []
    try:
        rc = env.run([env.py, "-c", "import torch;print(torch.__version__)"], env.tool_env(), None, lines.append)
    except OSError:
        return None
    if rc != 0:
        return None
    return lines[-1].strip() if lines else None


def step_packages(env):
    _run(env, "packages", _pip(env, "-e", env.engine_dir, "soundfile", "numpy", "huggingface_hub"))


def step_torch(env):
    # Pinned. Never `--reinstall torch` unpinned: it pulls the newest release (2.11 at the time), which breaks the engine.
    if torch_version(env) == TORCH_VERSION:
        env.progress("torch", 1, "Right version already installed, skipping")
    else:
        _run(env, "torch", _pip(env, *TORCH_PINS, index="--index-url"))
    _run(env, "torch", _pip(env, "--upgrade", "omegaconf>=2.3", "protobuf<3.21"))


def _env_get(env, name):
    return env.base_env.get(name) or ""


def user_hf_hub(env):
    """The user's own Hugging Face cache (not the install's): HF_HUB_CACHE, else HF_HOME/hub, else ~/.cache/huggingface/hub."""
    if _env_get(env, "HF_HUB_CACHE"):
        return Path(_env_get(env, "HF_HUB_CACHE"))
    if _env_get(env, "HF_HOME"):
        return Path(_env_get(env, "HF_HOME")) / "hub"
    home = _env_get(env, "USERPROFILE") or _env_get(env, "HOME") or str(Path.home())
    return Path(home) / ".cache" / "huggingface" / "hub"


def _nonempty(p):
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def model_complete(model_dir):
    return all(_nonempty(Path(model_dir) / f) for f in MODEL_FILES)


def _engine_ok(install):
    try:
        return (Path(install) / "index-tts" / "pyproject.toml").stat().st_size > 100
    except OSError:
        return False


def _repo_complete(repo_dir, files):
    """A cache repo folder with a snapshot holding every needed file and no half-finished blob."""
    repo_dir = Path(repo_dir)
    if any(repo_dir.glob("blobs/*.incomplete")):
        return False
    return any(all(_nonempty(sn / f) for f in files) for sn in (repo_dir / "snapshots").glob("*"))


def _copy_tree(src, dst, ignore=None):
    """Copy a folder; keep the cache's links where the OS allows, otherwise copy the real files."""
    try:
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True, ignore=ignore)
    except (OSError, shutil.Error):
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=True, ignore=ignore)


def _model_sources(env):
    out = []
    if _env_get(env, "SSW_MODEL_DIR"):
        out.append(Path(_env_get(env, "SSW_MODEL_DIR")))
    out += sorted((user_hf_hub(env) / ("models--" + MODEL_REPO.replace("/", "--")) / "snapshots").glob("*"))
    return out


def step_model(env):
    if model_complete(env.model_dir):
        env.progress("model", 1, "Voice model already here, skipping")
        return
    for src in _model_sources(env):
        if model_complete(src):
            env.progress("model", None, "Found on this PC, copying")
            _copy_tree(src, env.model_dir, ignore=shutil.ignore_patterns("qwen*"))
            if model_complete(env.model_dir):
                return
    # The text-emotion folder (qwen*, about 1.2 GB) is never loaded by the worker, so it is not fetched.
    # !r quotes the path safely: a user folder with an apostrophe (C:\Users\O'Brien) broke the old r'...' form
    code = (f"from huggingface_hub import snapshot_download; "
            f"snapshot_download({MODEL_REPO!r}, local_dir={str(env.model_dir)!r}, ignore_patterns=['qwen*/*', 'qwen*'])")
    _run(env, "model", [env.py, "-c", code], extra_env=HF_FAST_ENV)


def extras_code():
    """Short script for the install's own Python: only downloads, never loads the voice model or the graphics card.
    Already-fetched files are skipped and a part-finished download carries on, so a re-run is cheap."""
    lines = ["from huggingface_hub import snapshot_download, hf_hub_download"]
    lines += [f"snapshot_download({repo!r}, allow_patterns={pats!r})" for repo, pats in EXTRA_SNAPSHOTS]
    lines += [f"hf_hub_download({repo!r}, filename={fn!r})" for repo, fn in EXTRA_FILES]
    return "; ".join(lines)


def step_extras(env):
    hub = env.install / "hf" / "hub"
    mine = user_hf_hub(env)
    if mine.resolve() != hub.resolve():
        for repo, files in EXTRAS_NEED.items():
            if _repo_complete(hub / repo, files) or not _repo_complete(mine / repo, files):
                continue
            env.progress("extras", None, "Found on this PC, copying " + repo.split("--", 1)[1].replace("--", "/"))
            hub.mkdir(parents=True, exist_ok=True)
            _copy_tree(mine / repo, hub / repo)
    if extras_cached(env.install):
        return
    # Same cache the worker uses (HF_HOME inside the install folder, set by tool_env and by the runner).
    _run(env, "extras", [env.py, "-c", extras_code()], extra_env=HF_FAST_ENV)


def _checksum_for(text, filename):
    """The sha256 listed for `filename` in a `<hex>  <name>` checksum file, or None."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def step_ffmpeg(env):
    """Download the audio tools and prove them: the archive must match the checksum the same release publishes, and the
    program must start and say it is ffmpeg. A file that merely downloaded is not an install."""
    exe = "ffmpeg" + env._exe
    if _big_enough([env.ffmpeg_dir / exe], MIN_TOOL_BYTES):       # a tiny placeholder is replaced, not trusted
        return
    if not env.is_win and shutil.which("ffmpeg"):        # a system ffmpeg is fine
        return
    url = FFMPEG_URLS[_plat(env)]
    z = env.install / ("ffmpeg.zip" if env.is_win else "ffmpeg.tar.xz")
    sums = env.install / "ffmpeg.checksums.sha256"
    env.download(url, z, lambda f, g=0, t=0: env.progress("ffmpeg", f, ""))
    try:
        env.download(url.rsplit("/", 1)[0] + "/checksums.sha256", sums, lambda f, g=0, t=0: None)
        want = _checksum_for(sums.read_text(encoding="utf-8", errors="replace"), url.rsplit("/", 1)[1])
    except (OSError, SetupError, urllib.error.URLError) as e:
        z.unlink(missing_ok=True)
        raise SetupError(f"the audio tools could not be checked (the checksum list was not available: {e})") from None
    finally:
        sums.unlink(missing_ok=True)
    if want is None or _file_sha256(z) != want:
        z.unlink(missing_ok=True)
        raise SetupError("the audio tools download did not match its checksum and was removed; start setup again")
    _extract(z, env.install / "_ff")
    found = next((p for p in (env.install / "_ff").rglob(exe) if p.is_file()), None)
    if not found:
        raise SetupError("the audio tools were not in the download")
    env.ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    target = env.ffmpeg_dir / exe
    shutil.copy2(found, target)
    if not env.is_win:
        target.chmod(0o755)
    shutil.rmtree(env.install / "_ff", ignore_errors=True)
    z.unlink(missing_ok=True)
    try:
        rc, out = (env.probe or real_probe)([target, "-version"], 20)
    except Exception:
        rc, out = -1, ""
    if rc != 0 or not (out or "").lstrip().startswith("ffmpeg version"):
        target.unlink(missing_ok=True)
        raise SetupError("the audio tools downloaded but would not start, so they were removed; start setup again")


def step_enroll(env):
    """Ask the site for a key and hand it straight to `net_worker.py init` (child environment only)."""
    if config_has_key(env.config_file):
        return
    try:                                    # an empty or broken file is not a key: join again instead of waiting forever
        Path(env.config_file).unlink()
    except OSError:
        pass
    info = env.enroll()
    key = info["key"]
    cmd = [env.py, env.install / "net_worker.py", "init", "--config", env.config_file,
           "--model-dir", env.model_dir, "--work-dir", env.work_dir]
    if env.api_base:
        cmd += ["--api-base", env.api_base]
    if env.machine_name:
        cmd += ["--machine-name", env.machine_name]
    _run(env, "enroll", cmd, extra_env={"SSW_WORKER_KEY": key}, cwd=env.install)
    if not env.config_file.exists():
        raise SetupError("The worker key was not accepted by the worker. Please try again.")
    env.enrolled_file.write_text(json.dumps({"worker_id": info.get("worker_id"), "label": info.get("label"),
                                             "tier": info.get("tier")}), encoding="utf-8")
    env.progress("enroll", 1, f"Joined the queue as {info.get('label') or 'a new worker'}")


# Two workers on one card: each is an ordinary one-lease worker with its own key (the site limits a key to one lease,
# so sharing a key would hand the second process the first one's job).
MIN_TOTAL_MIB_FOR_TWO = 12000            # a "12 GB" card reports 12282-12288 MiB
MIN_FREE_MIB_FOR_TWO = 10000             # about 5 GB for each process; "10 GB" cards report a little under 10240


def second_worker_allowed(gpu):
    """(ok, sentence). `gpu` is stats.gpu_reading()."""
    if not gpu:
        return False, "No NVIDIA graphics card was found."
    if gpu.get("mem_total_mib", 0) < MIN_TOTAL_MIB_FOR_TWO:
        return False, "Two workers need a graphics card with at least 12 GB of memory."
    if gpu["mem_total_mib"] - gpu.get("mem_used_mib", 0) < MIN_FREE_MIB_FOR_TWO:
        return False, "Two workers need about 10 GB of free graphics memory right now. Close other programs and try again."
    return True, ""


def enroll_second(env):
    """Join the queue a second time as '<host>-2' and write worker_config_2.json. Kept if it already exists."""
    if env.config_file_2.exists():
        return
    name = (env.machine_name or "worker") + "-2"
    info = env.enroll(machine_name=name)
    key = info["key"]
    env.work_dir_2.mkdir(parents=True, exist_ok=True)
    cmd = [env.py, env.install / "net_worker.py", "init", "--config", env.config_file_2,
           "--model-dir", env.model_dir, "--work-dir", env.work_dir_2, "--machine-name", name]
    if env.api_base:
        cmd += ["--api-base", env.api_base]
    _run(env, "enroll", cmd, extra_env={"SSW_WORKER_KEY": key}, cwd=env.install)
    if not env.config_file_2.exists():
        raise SetupError("The second worker key was not accepted by the worker. Please try again.")
    env.enrolled_file_2.write_text(json.dumps({"worker_id": info.get("worker_id"), "label": info.get("label"),
                                               "tier": info.get("tier")}), encoding="utf-8")


def step_check(env):
    _run(env, "check", [env.py, "-c",
                        "import torch; assert torch.cuda.is_available(), 'no CUDA'; "
                        "print('graphics card ready:', torch.cuda.get_device_name(0))"])


FUNCS = {"gpu": step_gpu, "folders": step_folders, "worker_files": step_worker_files, "uv": step_uv,
         "python": step_python, "index_tts": step_index_tts, "packages": step_packages, "torch": step_torch,
         "model": step_model, "extras": step_extras, "ffmpeg": step_ffmpeg, "enroll": step_enroll, "check": step_check}


def _any_exists(*paths):
    return any(Path(p).exists() for p in paths)


MIN_TOOL_BYTES = 100_000        # uv and ffmpeg are programs of many MB; a few bytes is a placeholder, not an install
MIN_WORKER_BYTES = 1_000        # the worker scripts are tens of KB


def _big_enough(paths, minimum):
    for p in paths:
        try:
            if Path(p).stat().st_size >= minimum:
                return True
        except OSError:
            pass
    return False


def worker_files_ok(install):
    """All three scripts present and not placeholders; net_worker.py must be the real worker (it names its version)."""
    i = Path(install)
    if not all(_big_enough([i / n], MIN_WORKER_BYTES) for n in WORKER_FILES):
        return False
    try:
        return "WORKER_VERSION" in (i / "net_worker.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def config_has_key(path):
    """True when the worker's config file holds a worker key. The file merely existing is not enough: one PC had a
    config of `{}` and a finished setup, so the app never joined the queue and never said why."""
    try:
        key = json.loads(Path(path).read_text(encoding="utf-8")).get("worker_key")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(key, str) and bool(key.strip())


def artifact_ok(install, name):
    """Cheap existence check for what a finished step leaves behind. The state file is a note, not proof: a user can
    delete the venv or the model folder, and the step must then run again."""
    i = Path(install)
    if name == "worker_files":
        return worker_files_ok(i)
    if name == "uv":
        return _big_enough([i / "tools" / "uv.exe", i / "tools" / "uv"], MIN_TOOL_BYTES)
    if name in ("python", "torch", "check"):
        return _any_exists(i / "venv" / "Scripts" / "python.exe", i / "venv" / "bin" / "python")
    if name == "packages":
        return _engine_ok(i) and _any_exists(i / "venv" / "Scripts" / "python.exe", i / "venv" / "bin" / "python")
    if name == "index_tts":
        return _engine_ok(i)
    if name == "model":
        return model_complete(i / "IndexTTS-2")
    if name == "extras":
        return extras_cached(i)
    if name == "ffmpeg":
        return _big_enough([i / "ffmpeg" / "ffmpeg.exe", i / "ffmpeg" / "ffmpeg"], MIN_TOOL_BYTES) or (
            sys.platform != "win32" and shutil.which("ffmpeg") is not None)
    if name == "enroll":
        return config_has_key(i / "worker_config.json")
    return True


def verified_done(install, done):
    """`done` minus every step whose files are gone. Losing the venv also drops what was installed into it."""
    keep = {s for s in done if artifact_ok(install, s)}
    if "python" in done and "python" not in keep:
        keep -= {"torch", "packages", "check"}
    return keep


def is_set_up(install):
    try:
        done = set(json.loads((Path(install) / "setup_state.json").read_text(encoding="utf-8")).get("done", []))
    except (OSError, ValueError):
        return False
    done = verified_done(install, done)
    return all(s in done for s in STEPS if s != "gpu")


def step_hf_hub(env):
    """Just huggingface_hub, so the downloads can start while torch and the other packages are still installing.
    Run before the lanes (never beside another pip install), and through _pip so the torch pins still apply."""
    # Held below 1.0, as the engine's own packages require: the newest release would be swapped for an older one by the
    # engine install while a download in the other lane is still using it.
    _run(env, "hf_hub", _pip(env, HF_HUB_SPEC))


def run_setup(env, only=None):
    """Run every step not yet done, saving progress after each. Returns the steps run.

    Everything up to the engine fetch runs in order. Then two lanes run at once (LANE_A and LANE_B); enroll and check
    wait for both. If a lane fails, the other finishes the step it is on and starts no more, then the first error is
    raised, so a re-run resumes from the saved state. SSW_SETUP_SERIAL=1 runs strictly in STEPS order instead."""
    done = verified_done(env.install, load_state(env))
    ran = []
    total = len(STEPS)
    idx = {n: i for i, n in enumerate(STEPS)}
    lock = threading.Lock()

    def run_one(name):
        env.progress(name, idx[name] / total, LABELS[name])
        if name in done and name not in ("gpu", "folders"):
            return
        FUNCS[name](env)
        with lock:
            if name != "gpu":
                done.add(name)
                save_state(env, set(done))
            ran.append(name)

    want = [n for n in STEPS if not only or n in only]
    lane_names = set(LANE_A) | set(LANE_B)
    split = idx[LANE_A[0]]
    before = [n for n in want if idx[n] < split]
    middle = [n for n in want if n in lane_names]
    after = [n for n in want if idx[n] >= split and n not in lane_names]
    for name in before:
        run_one(name)
    if env.base_env.get("SSW_SETUP_SERIAL") == "1":
        for name in middle:
            run_one(name)
    elif middle:
        lane_a = [n for n in LANE_A if n in middle]
        lane_b = [n for n in LANE_B if n in middle]
        if any(n in lane_b and n not in done for n in ("model", "extras")) and env.py.exists():
            env.progress("hf_hub", None, LABELS["hf_hub"])
            step_hf_hub(env)
        stop, errors = threading.Event(), []

        def lane(names):
            for n in names:
                if stop.is_set():
                    return
                try:
                    run_one(n)
                except BaseException as e:          # re-raised below, after the other lane's current step ends
                    errors.append(e)
                    stop.set()
                    return
        threads = [threading.Thread(target=lane, args=(ns,), daemon=True) for ns in (lane_a, lane_b) if ns]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
    for name in after:
        run_one(name)
    if not only and all(s in done for s in STEPS if s != "gpu"):
        shutil.rmtree(env.install / "uv_cache", ignore_errors=True)    # several GB of wheels only a re-install needs
    env.progress("done", 1.0, "Setup finished.")
    return ran


def extras_cached(install):
    """True when every file the engine would fetch on its first job is already in <install>/hf. Pure file check."""
    hub = Path(install) / "hf" / "hub"
    return all(_repo_complete(hub / repo, files) for repo, files in EXTRAS_NEED.items())


def forget_extras(install):
    """Mark the prefetch step as not done (the cache lost it), so the next setup run fetches it again."""
    f = Path(install) / "setup_state.json"
    try:
        done = set(json.loads(f.read_text(encoding="utf-8")).get("done", []))
    except (OSError, ValueError):
        return
    done.discard("extras")
    f.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")


def sync_worker_files(env_or_install, resource_dir=None):
    """Copy the bundled worker scripts over the installed ones when they differ. Setup copies them once; without this
    an install made by an older app keeps running the older worker (no error text in failures, no fixes)."""
    install = Path(getattr(env_or_install, "install", env_or_install))
    res = Path(getattr(env_or_install, "resource_dir", resource_dir))
    changed = []
    for n in WORKER_FILES:
        src, dst = res / n, install / n
        if not src.exists():
            continue
        try:
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                shutil.copy2(src, dst)
                changed.append(n)
        except OSError:
            pass
    return changed
