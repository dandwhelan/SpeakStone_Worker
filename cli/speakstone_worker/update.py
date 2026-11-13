"""Self-update from the project's public GitHub releases.

Safety properties (each is tested):
- nothing is installed unless the release carries a fingerprint file (`<asset>.sha256`) and the download matches it;
- the new file must also start and print the expected version before it replaces anything;
- only https links on GitHub's own hosts are followed, redirects included;
- any failure removes the half-done file, keeps the current version and is tried again at the next check;
- only a frozen program running from its own install folder swaps itself; from source or from some other folder it
  only reports that a newer version exists.

Every network and file-system call sits behind a function argument so tests replace it with a fake.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from pathlib import Path

from . import APP_NAME, __version__

RELEASE_URL = "https://api.github.com/repos/dandwhelan/SpeakStone_Worker/releases/latest"
ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
ASSETS = {"win32": "SpeakStoneWorker-windows.exe", "linux": "SpeakStoneWorker-linux"}
MIN_BYTES = 5_000_000                       # the real program is far larger; anything smaller is not it
CHECK_TIMEOUT = 8
DOWNLOAD_TIMEOUT = 120
CHECK_EVERY = 6 * 3600
RETRY_AFTER_FAILURE = (120, 300, 900, 3600)   # seconds before the next look after 1, 2, 3, 4+ failed checks in a row
SWITCHED_OFF = "updates are switched off (SSW_NO_UPDATE)"
RESET_ENV = "PYINSTALLER_RESET_ENVIRONMENT"  # a one-file program starting itself must unpack afresh, not reuse ours

Release = namedtuple("Release", "version asset_url sha_url")


class UpdateError(Exception):
    """A reason, in plain words, why no update was done."""


def disabled(env=None):
    return (os.environ if env is None else env).get("SSW_NO_UPDATE") == "1"


def parse_version(text):
    """'v1.10.0' -> (1, 10, 0). None when it does not look like a version."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def platform_key(platform=None):
    p = platform or sys.platform
    return "win32" if p == "win32" else "linux"


def url_allowed(url):
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and (u.hostname or "").lower() in ALLOWED_HOSTS


def pick_release(data, platform=None):
    """Read a GitHub 'latest release' answer. -> Release for this platform. Raises UpdateError with the reason."""
    if not isinstance(data, dict):
        raise UpdateError("the release answer was not understood")
    version = (data.get("tag_name") or "").lstrip("v")
    if parse_version(version) is None:
        raise UpdateError("the release has no version number")
    name = ASSETS[platform_key(platform)]
    by_name = {a.get("name"): a.get("browser_download_url") for a in data.get("assets") or [] if isinstance(a, dict)}
    if name not in by_name:
        raise UpdateError(f"release {version} has no file for this system")
    if name + ".sha256" not in by_name:
        raise UpdateError(f"release {version} has no fingerprint file, so it is not installed")
    asset_url, sha_url = by_name[name], by_name[name + ".sha256"]
    for u in (asset_url, sha_url):
        if not url_allowed(u):
            raise UpdateError("the release links point somewhere other than GitHub")
    return Release(version, asset_url, sha_url)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl):
            raise urllib.error.URLError("redirect to a host that is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(_CheckedRedirects)


def _request(url):
    return urllib.request.Request(url, headers={"User-Agent": f"SpeakStoneWorker/{__version__}",
                                                "Accept": "application/vnd.github+json, application/octet-stream"})


def http_get(url, timeout=CHECK_TIMEOUT, max_bytes=2_000_000):
    """Small text answers (the release list, the fingerprint). https only."""
    if not url.lower().startswith("https://"):
        raise UpdateError("not an https link")
    with _opener().open(_request(url), timeout=timeout) as r:
        return r.read(max_bytes)


def http_download(url, dest, timeout=DOWNLOAD_TIMEOUT):
    if not url_allowed(url):
        raise UpdateError("download link is not on GitHub")
    with _opener().open(_request(url), timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def check(current=__version__, env=None, platform=None, get=http_get):
    """-> Release when a newer one exists, None when up to date. Raises UpdateError when it cannot tell or must not."""
    import json
    env = os.environ if env is None else env
    if disabled(env):
        raise UpdateError(SWITCHED_OFF)
    try:
        data = json.loads(get(env.get("SSW_UPDATE_URL") or RELEASE_URL))
    except UpdateError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as e:
        raise UpdateError(f"could not reach the release page ({describe_error(e)})")
    rel = pick_release(data, platform)
    return rel if is_newer(rel.version, current) else None


def describe_error(e):
    """A short reason for the log: the kind of error, the HTTP status if any, and the cause (a link is never included).
    'URLError' alone told us nothing when checks failed for 20 minutes on one PC."""
    kind = type(e).__name__
    code = getattr(e, "code", None)
    reason = getattr(e, "reason", None)
    text = kind + (f" {code}" if isinstance(code, int) else "")
    if reason is not None and not isinstance(reason, str):
        reason = f"{type(reason).__name__}: {reason}"
    if reason:
        text += f": {str(reason)[:120]}"
    elif not isinstance(code, int) and str(e):
        text += f": {str(e)[:120]}"
    return text


def parse_fingerprint(text):
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    return m.group(1).lower() if m else None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def staging_name(exe):
    """Beside the installed program, with `.new` before the extension (so Windows still treats it as a program)."""
    exe = Path(exe)
    return exe.with_name(f"{exe.stem}.new{exe.suffix}")


def old_name(exe):
    exe = Path(exe)
    return exe.with_name(exe.name + ".old")


def free_old_name(exe, remove=None):
    """A name the running program can be moved aside to. `<name>.old` when it is free or can be deleted; when an earlier
    old copy is still running (a window left open from before the last update) Windows will not delete it, so the next
    free `<name>.old2`, `.old3`, ... is used. Without this, one forgotten open window blocked every later update and
    every service install on that PC (seen 2026-11-12)."""
    exe = Path(exe)
    remove = remove or _remove
    for n in range(1, 50):
        cand = exe.with_name(exe.name + (".old" if n == 1 else f".old{n}"))
        remove(cand)
        if not cand.exists():
            return cand
    raise OSError("too many old copies of the program are still running; close its other windows")


def self_run(cmd, timeout=60):
    env = dict(os.environ)
    env[RESET_ENV] = "1"
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def run_version(path, run=self_run):
    try:
        r = run([str(path), "--version"])
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def fetch_verified(release, exe, get=http_get, download=http_download, run=self_run, min_bytes=MIN_BYTES):
    """Download the release next to `exe` and prove it: size, fingerprint, and that it starts. -> path of the `.new`
    file. On any failure the file is deleted and UpdateError says why."""
    new = staging_name(exe)
    try:
        want = parse_fingerprint(get(release.sha_url).decode("utf-8", "replace"))
        if not want:
            raise UpdateError("the fingerprint file could not be read")
        download(release.asset_url, str(new))
        if not new.exists() or new.stat().st_size < min_bytes:
            raise UpdateError("the downloaded file is too small")
        if sha256_of(new) != want:
            raise UpdateError("the downloaded file does not match its fingerprint")
        if run_version(new, run) != f"{APP_NAME} {release.version}":
            raise UpdateError("the downloaded file did not start as the expected version")
        return new
    except UpdateError:
        _remove(new)
        raise
    except (OSError, urllib.error.URLError, subprocess.SubprocessError) as e:
        _remove(new)
        raise UpdateError(f"the download failed ({describe_error(e)})")


def _remove(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def cleanup_old(exe):
    """At start: delete the `.old` and `.new` files an earlier swap or failed download left behind."""
    if exe:
        exe = Path(exe)
        for leftover in exe.parent.glob(exe.name + ".old*"):      # one still in use stays; it goes at a later start
            _remove(leftover)
        _remove(staging_name(exe))


def file_stamp(path):
    """(size, modified time) of a file, or None when it is not there."""
    try:
        st = os.stat(str(path))
        return st.st_size, st.st_mtime_ns
    except (OSError, TypeError):
        return None


def same_file(a, b):
    try:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
    except OSError:
        return False


def folder_writable(folder, mkstemp=None):
    """Can a file be made beside the program? (Needed for the new file and for renaming the running one.)"""
    import tempfile
    try:
        fd, name = (mkstemp or tempfile.mkstemp)(prefix=".ssw_write_test_", dir=str(folder))
        os.close(fd)
        os.remove(name)
        return True
    except OSError:
        return False


def can_self_update(frozen, exe, app_exe=None, writable=None):
    """(True, "") or (False, why). The frozen program swaps itself wherever it was started from, as long as it can write
    beside itself. Until 1.2.2 this was only allowed from the program's own install folder: everyone who ran the file
    they had downloaded was told "update available" for ever and never updated. `app_exe` is kept for callers."""
    if not frozen:
        return False, "running from source"
    if not exe:
        return False, "the program's own file is not known"
    if not (writable or folder_writable)(Path(exe).parent):
        return False, "cannot write in the program's folder (move the program to a folder of your own)"
    return True, ""


def relaunch_command(exe, args, platform=None, environ=None):
    """The command that starts the new program with the same arguments. Background mode keeps its windowless launch."""
    from . import service
    if platform_key(platform) == "win32" and "--background" in args:
        program, rest = service.windowless(exe, list(args), environ)
        return [program, *rest]
    return [str(exe), *args]


def _spawn_detached(cmd, popen=subprocess.Popen):
    env = dict(os.environ)
    env[RESET_ENV] = "1"
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    kw = dict(env=env, **quiet)
    if sys.platform == "win32":
        base = 0x00000008 | 0x00000200                      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        if "--headless" not in cmd:
            base = 0x00000010 | 0x00000200                  # a menu run gets its own console window
            # ... and must write to THAT window. Until 1.2.4 its output was sent nowhere, so after an update the
            # restarted program was an empty black window (it was running, but drew to nothing).
            kw = dict(env=env)
        try:                                                # leave the Task Scheduler's job, if it allows that
            return popen(cmd, creationflags=base | 0x01000000, **kw)
        except OSError:
            return popen(cmd, creationflags=base, **kw)
    return popen(cmd, start_new_session=True, **kw)


def swap_windows(exe, new, args, rename=os.rename, replace=os.replace, spawn=_spawn_detached, remove=_remove,
                 exit_fn=sys.exit):
    """A running exe can be renamed but not overwritten: move it to `.old`, put the new one in its place, start it,
    leave. If the second step fails the old name is put back and nothing has changed."""
    exe, new = Path(exe), Path(new)
    old = free_old_name(exe, remove)
    rename(exe, old)
    try:
        replace(new, exe)
    except OSError:
        rename(old, exe)
        raise
    try:
        spawn(relaunch_command(exe, args, "win32"))
    except OSError:
        raise UpdateError("the new version is in place but could not be started; it starts next time")
    exit_fn(0)


def swap_linux(exe, new, args, replace=os.replace, execv=os.execv):
    os.environ[RESET_ENV] = "1"
    replace(new, exe)
    execv(str(exe), [str(exe), *args])


def apply(release, exe, args, platform=None, fetch=fetch_verified, **ops):
    # `fetch` may hand back a file that was already proven (the flow downloads before it stops the worker)
    """Download, prove and swap. Does not return when the swap worked (the process is replaced or exits).
    Raises UpdateError otherwise, with the `.new` file already removed."""
    new = fetch(release, exe)
    try:
        if platform_key(platform) == "win32":
            swap_windows(exe, new, args, **ops)
        else:
            swap_linux(exe, new, args, **ops)
    except OSError as e:
        _remove(new)
        raise UpdateError(f"could not put the new version in place ({type(e).__name__})")


def install_copy(src, dst, copy=shutil.copy2, replace=os.replace, rename=os.rename, remove=_remove):
    """Copy the running program into its own folder. Used when installing the service. A copy that is already in use
    (the old one is running) is moved aside first, as the updater does. Raises OSError when it cannot be done."""
    src, dst = Path(src), Path(dst)
    if same_file(src, dst):
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".copy")
    try:
        copy(str(src), str(tmp))
        try:
            replace(tmp, dst)
        except OSError:
            old = free_old_name(dst, remove)
            rename(dst, old)
            try:
                replace(tmp, dst)
            except OSError:
                rename(old, dst)
                raise
    except OSError:
        remove(tmp)
        raise
    return dst


class UpdateFlow:
    """Check and apply, for the menu and for the background loop. All outside calls are arguments, so tests fake them.
    `log` gets one short line per outcome; a failed check never raises."""

    def __init__(self, log=lambda s: None, check_fn=check, apply_fn=apply, frozen=None, exe=None, app_exe=None,
                 args=None, clock=None, env=None, fetch_fn=fetch_verified):
        import time
        self.log, self.check_fn, self.apply_fn, self.fetch_fn = log, check_fn, apply_fn, fetch_fn
        self.before_swap = None         # called just before a swap that follows a drain (writes the resume marker)
        self.swap_failed = None
        self.staged = None              # a downloaded and proven new file, waiting for the swap
        self.frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        self.exe = exe if exe is not None else (sys.executable if self.frozen else None)
        self.app_exe = app_exe
        self.args = list(sys.argv[1:] if args is None else args)
        self.clock = clock or time.time
        self.env = env
        self.release = None             # a newer release that is known but not installed yet
        self.next_check = 0.0
        self.failures = 0               # failed checks in a row: sets how soon the next look is
        self._last_failure = None       # the message of the last failure, so a broken network logs one line, not thousands
        self.forced = False             # the user asked ("Update now" / "Check now"): install even with auto off
        self.stamp = file_stamp(self.exe) if self.frozen else None      # the program file as it was when we started
        self._seen = (None, None)       # (stamp, version) of the last changed file looked at

    @property
    def version(self):
        return self.release.version if self.release else None

    def due(self):
        return self.clock() >= self.next_check

    def check(self):
        """Look now. -> the newer Release or None. Silent on failure apart from one log line per distinct message.
        After a failure the next look comes sooner (2, 5, 15, then every 60 minutes) so a PC that woke up before its
        network did does not wait six hours; after any answer it is the normal six hours again. Never raises."""
        now = self.clock()
        self.next_check = now + CHECK_EVERY
        try:
            rel = self.check_fn(env=self.env)
        except UpdateError as e:
            msg = str(e)
            if msg != SWITCHED_OFF:                     # switched off is not a failure: nothing to retry sooner
                self.next_check = now + RETRY_AFTER_FAILURE[min(self.failures, len(RETRY_AFTER_FAILURE) - 1)]
                self.failures += 1
            if msg != self._last_failure:
                self.log(f"update check: {msg}")
            self._last_failure = msg
            return None
        except Exception as e:                          # nothing unexpected may end checking: log it and look again
            msg = f"unexpected {type(e).__name__}: {e}"[:200]
            self.next_check = now + RETRY_AFTER_FAILURE[min(self.failures, len(RETRY_AFTER_FAILURE) - 1)]
            self.failures += 1
            if msg != self._last_failure:
                self.log(f"update check: {msg}")
            self._last_failure = msg
            return None
        self.failures, self._last_failure = 0, None
        if rel and (self.release is None or rel.version != self.release.version):
            self.log(f"update available: {rel.version}")
        self.release = rel
        return rel

    def can_apply(self):
        return can_self_update(self.frozen, self.exe, self.app_exe)

    def replaced(self, stamp=file_stamp, version_of=run_version):
        """Has ANOTHER copy of the program (the service, another window) already put a newer version in our place?
        -> its version, or None. A window left open while the service updated itself kept running the old version for
        ever, and its own later update attempt failed (seen 2026-11-13). The slow look inside the file (it has to be
        started once) happens only when the file changed, and once per change."""
        if not self.frozen or not self.exe or self.stamp is None:
            return None
        now = stamp(self.exe)
        if now is None or now == self.stamp:
            return None
        if self._seen[0] != now:
            text = version_of(self.exe)
            found = text[len(APP_NAME):].strip() if text.startswith(APP_NAME + " ") else ""
            self._seen = (now, found if found and is_newer(found, __version__) else None)
        return self._seen[1]

    def restart_as_installed(self, spawn=_spawn_detached, exit_fn=sys.exit):
        """Start the program file that is already in place and leave. Nothing is downloaded or moved."""
        self.log(f"restarting as the installed version {self._seen[1] or ''}".rstrip())
        try:
            spawn(relaunch_command(self.exe, self.args))
        except OSError:
            self.log("the installed newer version could not be started; close this window and open the program again")
            self.stamp = self._seen[0]              # do not try again every few seconds
            return False
        exit_fn(0)
        return True

    def prepare(self):
        """Download and prove the new file now, before anything is stopped. -> None, or a sentence on failure (the
        release is forgotten and looked for again at the next check)."""
        if self.staged is not None and Path(self.staged).exists():
            return None
        try:
            self.staged = self.fetch_fn(self.release, self.exe)
        except UpdateError as e:
            self.log(f"update not installed: {e}")
            self.release, self.staged = None, None
            return f"The update was not installed: {e}."
        return None

    def give_up(self):
        """Drop this round (the worker did not finish in time): delete the proven file, look again at the next check."""
        if self.staged is not None:
            _remove(self.staged)
        self.staged, self.release, self.forced = None, None, False
        self.next_check = self.clock() + CHECK_EVERY

    def apply(self):
        """Install the known release. Returns a sentence when nothing was installed; does not return on success."""
        if self.release is None:
            return "No update is waiting."
        ok, why = self.can_apply()
        if not ok:
            return f"Version {self.release.version} is available ({why}). Download it from the releases page."
        msg = self.prepare()
        if msg:
            return msg
        try:
            self.apply_fn(self.release, self.exe, self.args, fetch=lambda r, e: self.staged)
        except UpdateError as e:
            self.log(f"update not installed: {e}")
            self.release, self.staged = None, None            # tried again at the next check
            return f"The update was not installed: {e}."
        return "Updating..."
