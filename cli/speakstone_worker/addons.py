"""Keep the player's installed SpeakStone add-ons up to date (owner 2026-11-24: players who run the Worker should always
have the newest add-on).

What it does, for every WoW game folder wowtext.find_games() finds (_retail_, _forever_, ...):
- looks at the SpeakStone add-ons already in Interface/AddOns (the base add-on and every voice pack). It never installs
  an add-on the player does not have, and leaves alone a folder that is a link/junction, holds `.git`, holds a
  `.no-auto-update` file, or whose version it cannot read (a development copy);
- asks GitHub for each one's latest release (api.github.com/repos/dandwhelan/<folder>/releases/latest; every add-on
  folder is named after its repo, and each `vX.Y.Z` tag's release carries `<folder>-vX.Y.Z[-forever].zip`, made by the
  BigWigs packager in the repo's release.yml). No key is sent to GitHub, and only GitHub's own hosts are followed;
- updates only while WoW for that game folder is not running, and only when the release is newer than what is
  installed (a hand-uploaded pack whose GitHub release is older than CurseForge's is never downgraded);
- downloads into a staging folder, checks the size against GitHub's, the zip's checksums, that every entry sits under
  `<folder>/` (no `..`, no absolute path, no link) and that `<folder>/<folder>.toc` says the release's version, then
  swaps the folder in whole: rename the old folder out, rename the new one in, delete the old one.

Never a backup folder inside Interface/AddOns: a `.backup_*` there made WoW hang for minutes at logout (questwow
CLAUDE.md). The staging folder is the Worker's own data folder when it is on the same disk as WoW (so the swap is two
renames); otherwise it is `<game folder>/.SpeakStoneWorker_update`, outside Interface. A swap cut short (power cut) is
put back at the next run from what is left in the staging folder.

The base add-on is updated before the packs of that game folder, and the packs wait when it could not be: packs from
6.1.1 on store long lines in parts that an older base add-on cannot play.
"""
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import __version__

OWNER = "dandwhelan"
API = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com")
BASE_ADDONS = ("SpeakStone_Main", "SpeakStone_Forever_Main")
NAME_RE = re.compile(r"^SpeakStone_(?:Main|Forever_Main|Pack_[A-Za-z0-9_]{1,60}|Forever_[A-Za-z0-9_]{1,60})$")
DEV_MARKER = ".no-auto-update"
MAX_ZIP_BYTES = 1_000_000_000          # the biggest pack zip is ~520 MB (CurseForge's cap is ~513 MB)
MAX_UNPACKED_BYTES = 3_000_000_000      # audio is stored, not compressed: anything far bigger is not a pack
MAX_ENTRIES = 200_000
CHECK_EVERY = 6 * 3600                  # GitHub allows 60 unauthenticated calls an hour; one look is one per add-on
RETRY_AFTER_FAILURE = 3600
API_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 120                  # per read, not for the whole file
STATE_NAME = "addon_update.json"
STAGE_NAME = "addon_update"             # in the Worker's data folder
GAME_STAGE_NAME = ".SpeakStoneWorker_update"   # in the game folder, only when the data folder is on another disk
JOB_NAME = "job.json"


class UpdateError(Exception):
    """Why one add-on was not updated, in plain words."""


# --------------------------------------------------------------------------
# Versions and the installed add-ons
# --------------------------------------------------------------------------

def parse_version(text):
    m = re.match(r"^v?(\d+(?:\.\d+){0,3})$", (text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _norm(v):
    return v + (0,) * (4 - len(v))


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and _norm(a) > _norm(b)


def same_version(a, b):
    a, b = parse_version(a), parse_version(b)
    return a is not None and b is not None and _norm(a) == _norm(b)


def toc_version(text):
    m = re.search(r"^##\s*Version:\s*(\S+)", text or "", re.M)
    return m.group(1) if m else None


def _is_link(p):
    """A symlink, or on Windows a junction or any other reparse point (how a dev links a git clone into AddOns)."""
    try:
        st = os.lstat(p)
    except OSError:
        return True                                   # cannot even look at it: leave it alone
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT


def dev_reason(folder):
    """Why this add-on folder must not be touched, or '' when it may be updated."""
    if _is_link(folder):
        return "a linked folder"
    if (folder / ".git").exists():
        return "a git folder"
    if (folder / DEV_MARKER).exists():
        return f"marked {DEV_MARKER}"
    return ""


def installed_addons(game):
    """[(name, folder, version or None, dev reason)] for every SpeakStone add-on in this game folder, base add-ons first."""
    addons = game / "Interface" / "AddOns"
    out = []
    try:
        names = sorted(p.name for p in addons.iterdir() if NAME_RE.match(p.name))
    except OSError:
        return out
    for name in names:
        folder = addons / name
        reason = dev_reason(folder)
        if not reason and not folder.is_dir():
            continue
        version = None
        if not reason:
            try:
                version = toc_version((folder / f"{name}.toc").read_text(encoding="utf-8", errors="replace"))
            except OSError:
                version = None
        out.append((name, folder, version, reason))
    out.sort(key=lambda r: (r[0] not in BASE_ADDONS, r[0]))
    return out


# --------------------------------------------------------------------------
# Is WoW running from this game folder?
# --------------------------------------------------------------------------

_WOW_EXE = re.compile(r"^wow[a-z0-9_-]*\.exe$", re.I)     # Wow.exe, WowClassic.exe, WowT.exe, WowB.exe, Wow-64.exe


def _image_path(pid):
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(0x1000, False, int(pid))              # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


def wow_processes():
    """[(exe name, full path or None)] for every running WoW game program. A path that cannot be read is None."""
    out = []
    if sys.platform == "win32":
        from .update import _process_table
        for pid, (_parent, name) in _process_table().items():
            if _WOW_EXE.match(name or "") and name != "wowvoiceproxy.exe":     # voice chat helper, not the game
                try:
                    out.append((name, _image_path(pid)))
                except Exception:
                    out.append((name, None))
        return out
    # Linux/Wine: the game shows up as a process whose command line names the exe
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read(4096).split(b"\0")
        except OSError:
            continue
        for a in args[:3]:
            s = a.decode("utf-8", errors="replace").replace("\\", "/")
            base = s.rsplit("/", 1)[-1]
            if _WOW_EXE.match(base):
                out.append((base, None))                  # a Wine path is not the Linux path: treat as "maybe here"
                break
    return out


def wow_running_in(game, procs=None):
    """True when WoW may be running from this game folder. A WoW whose path cannot be read counts as running here."""
    procs = wow_processes() if procs is None else procs
    want = os.path.normcase(os.path.abspath(str(game)))
    for _name, path in procs:
        if not path:
            return True
        if os.path.normcase(os.path.abspath(os.path.dirname(path))) == want:
            return True
    return False


# --------------------------------------------------------------------------
# GitHub (no key, https, GitHub's own hosts only, redirects included)
# --------------------------------------------------------------------------

def url_allowed(url):
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and (u.hostname or "").lower() in ALLOWED_HOSTS and not u.username and not u.password


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl):
            raise urllib.error.URLError(f"redirect to a host that is not allowed (HTTP {code})")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            for h in ("Authorization", "Cookie"):           # never sent anyway; make sure nothing carries one on
                new.remove_header(h)
        return new


_OPENER = urllib.request.build_opener(_CheckedRedirects)


def _request(url, accept):
    # Deliberately no Authorization header: GitHub's public releases need no key, and the worker key is the site's.
    return urllib.request.Request(url, headers={"User-Agent": f"SpeakStoneWorker/{__version__}", "Accept": accept})


def http_json(url, timeout=API_TIMEOUT):
    if not url_allowed(url):
        raise UpdateError("not a GitHub https link")
    try:
        with _OPENER.open(_request(url, "application/vnd.github+json"), timeout=timeout) as r:
            return json.loads(r.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (403, 429):
            raise UpdateError("GitHub is limiting requests; trying again later") from None
        raise UpdateError(f"GitHub answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"could not reach GitHub ({type(e).__name__})") from None


def http_download(url, dest, expected, timeout=DOWNLOAD_TIMEOUT):
    """Save `url` to `dest`, refusing anything past `expected` bytes."""
    if not url_allowed(url):
        raise UpdateError("download link is not on GitHub")
    got = 0
    try:
        with _OPENER.open(_request(url, "application/octet-stream"), timeout=timeout) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > expected:
                    raise UpdateError("the download is bigger than GitHub said")
                f.write(chunk)
    except urllib.error.HTTPError as e:
        raise UpdateError(f"download failed (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"download failed ({type(e).__name__})") from None
    return got


def pick_release(name, data):
    """A GitHub 'latest release' answer -> {"version", "url", "size"}. Raises UpdateError."""
    if not isinstance(data, dict):
        raise UpdateError("the release answer was not understood")
    version = (data.get("tag_name") or "").strip().lstrip("v")
    if parse_version(version) is None:
        raise UpdateError("the release has no version number")
    zips = []
    for a in data.get("assets") or []:
        if not isinstance(a, dict):
            continue
        n = a.get("name") or ""
        if n.lower().endswith(".zip") and n.startswith(f"{name}-"):
            zips.append(a)
    if len(zips) != 1:
        raise UpdateError(f"release {version} has {'no' if not zips else 'more than one'} add-on zip")
    a = zips[0]
    url, size = a.get("browser_download_url") or "", a.get("size")
    if not url_allowed(url):
        raise UpdateError("the release link points somewhere other than GitHub")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ZIP_BYTES:
        raise UpdateError("the release zip's size is missing or too big")
    return {"version": version, "url": url, "size": size}


def latest_release(name, get=http_json):
    """-> release dict, or None when the add-on has no release on GitHub."""
    data = get(API.format(owner=OWNER, repo=name))
    return None if data is None else pick_release(name, data)


# --------------------------------------------------------------------------
# Checking and unpacking the zip
# --------------------------------------------------------------------------

def _bad_entry(name, folder):
    if not name or "\\" in name or "\x00" in name or name.startswith("/") or ":" in name:
        return True
    parts = name.split("/")
    if any(p in ("..", ".") for p in parts):
        return True
    return parts[0] != folder or (len(parts) == 1)


def check_zip(path, name, version):
    """Refuse a zip that is not exactly this add-on at this version. -> list of ZipInfo to unpack."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise UpdateError(f"the download is not a good zip ({type(e).__name__})") from None
    with z:
        infos = z.infolist()
        if not infos or len(infos) > MAX_ENTRIES:
            raise UpdateError("the zip is empty or holds too many files")
        total = 0
        for i in infos:
            if _bad_entry(i.filename, name):
                raise UpdateError(f"the zip holds a file outside {name}/ ({i.filename[:80]!r})")
            if stat.S_ISLNK(i.external_attr >> 16):
                raise UpdateError("the zip holds a link")
            total += i.file_size
        if total > MAX_UNPACKED_BYTES:
            raise UpdateError("the zip unpacks to far more than an add-on")
        toc = f"{name}/{name}.toc"
        try:
            got = toc_version(z.read(toc).decode("utf-8", errors="replace"))
        except KeyError:
            raise UpdateError(f"the zip has no {toc}") from None
        if not same_version(got, version):
            raise UpdateError(f"the zip's .toc says {got}, the release says {version}")
        try:
            bad = z.testzip()
        except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as e:
            raise UpdateError(f"the zip is damaged ({type(e).__name__})") from None
        if bad:
            raise UpdateError(f"the zip is damaged ({bad[:80]})")
        return infos


def unpack(path, infos, dest):
    """Write each entry under `dest` (which then holds `<name>/`), never anywhere else."""
    root = os.path.realpath(dest)
    with zipfile.ZipFile(path) as z:
        for i in infos:
            target = os.path.realpath(os.path.join(root, *i.filename.split("/")))
            if os.path.commonpath([root, target]) != root or target == root:
                raise UpdateError(f"the zip holds a file outside the add-on ({i.filename[:80]!r})")
            if i.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            left = i.file_size
            with z.open(i) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    left -= len(chunk)
                    if left < 0:
                        raise UpdateError("a file in the zip is bigger than it says")
                    out.write(chunk)


# --------------------------------------------------------------------------
# Staging and the swap
# --------------------------------------------------------------------------

def _dev(p):
    try:
        return os.stat(p).st_dev
    except OSError:
        return None


def stage_root(install, game):
    """A folder on the same disk as the game's AddOns folder, outside Interface, for the download and the swap."""
    own = Path(install) / STAGE_NAME
    try:
        own.mkdir(parents=True, exist_ok=True)
    except OSError:
        own = None
    addons = game / "Interface" / "AddOns"
    if own is not None and _dev(own) is not None and _dev(own) == _dev(addons):
        return own
    return game / GAME_STAGE_NAME


def _rmtree(p):
    shutil.rmtree(p, ignore_errors=True)


def recover(install, games, log=lambda s: None):
    """Finish what a cut-short run left: put back an add-on folder that was moved out and never replaced, then clear
    the staging folders."""
    roots = [Path(install) / STAGE_NAME] + [g / GAME_STAGE_NAME for g in games]
    for root in roots:
        try:
            jobs = [j for j in root.iterdir() if j.is_dir()] if root.is_dir() else []
        except OSError:
            continue
        for job in jobs:
            try:
                info = json.loads((job / JOB_NAME).read_text(encoding="utf-8"))
                target, name = Path(info["addons"]) / info["name"], info["name"]
                old = job / "old" / name
                if NAME_RE.match(name) and old.is_dir() and not os.path.lexists(target):
                    os.rename(old, target)
                    log(f"add-ons: put {name} back after an update was cut short")
            except (OSError, ValueError, KeyError, TypeError):
                pass
            _rmtree(job)
        if root.name == GAME_STAGE_NAME:
            try:
                root.rmdir()
            except OSError:
                pass


def swap_in(new, target, old, still_safe, rename=os.rename):
    """Replace `target` with `new`; the old folder goes to `old` and is put back if anything fails."""
    if not still_safe():
        raise UpdateError("WoW was started; will update when it is closed")
    try:
        rename(target, old)
    except OSError as e:
        raise UpdateError(f"the add-on folder is in use ({type(e).__name__})") from None
    try:
        rename(new, target)
    except OSError as e:
        try:
            rename(old, target)
        except OSError:
            pass                                       # recover() puts it back on the next run
        raise UpdateError(f"could not move the new add-on in ({type(e).__name__})") from None
    _rmtree(old)


def update_one(install, game, name, folder, release, running=wow_running_in, download=http_download,
               log=lambda s: None):
    """Download, check and swap in one add-on. Raises UpdateError. -> the new version."""
    root = stage_root(install, game)
    job = root / f"{name}-{os.getpid()}-{int(time.time())}"
    job.mkdir(parents=True, exist_ok=False)
    try:
        (job / JOB_NAME).write_text(json.dumps({"addons": str(folder.parent), "name": name}), encoding="utf-8")
        need = release["size"] * 2 + 50_000_000
        try:
            if shutil.disk_usage(job).free < need:
                raise UpdateError("not enough free disk space")
        except OSError:
            pass
        zpath = job / f"{name}.zip"
        got = download(release["url"], zpath, release["size"])
        if got != release["size"] or zpath.stat().st_size != release["size"]:
            raise UpdateError("the download is not the size GitHub said")
        infos = check_zip(zpath, name, release["version"])
        unpack(zpath, infos, job / "new")
        zpath.unlink()
        (job / "old").mkdir()
        if dev_reason(folder):
            raise UpdateError("the folder became a linked or development copy")
        swap_in(job / "new" / name, folder, job / "old" / name, lambda: not running(game))
        return release["version"]
    finally:
        _rmtree(job)
        if root.name == GAME_STAGE_NAME:
            try:
                root.rmdir()
            except OSError:
                pass


# --------------------------------------------------------------------------
# One run over every game folder
# --------------------------------------------------------------------------

def run(install, games, latest=latest_release, update=update_one, running=wow_running_in, log=lambda s: None):
    """Update what can be updated. -> {"games": {game folder name: {...}}, "updated_base": bool, "errors": int}."""
    recover(install, games, log)
    cache, out, updated_base, errors = {}, {}, False, 0
    stop_asking = None
    for game in games:
        g = {"folder": str(game), "updated": [], "waiting": [], "skipped": [], "problems": [], "checked": 0}
        out[game.name] = g
        base_blocked = False
        for name, folder, version, reason in installed_addons(game):
            if reason:
                g["skipped"].append(f"{name} ({reason})")
                continue
            if parse_version(version) is None:
                g["skipped"].append(f"{name} (version not readable)")
                continue
            if stop_asking:
                g["problems"].append(f"{name}: {stop_asking}")
                continue
            if name not in cache:
                try:
                    cache[name] = latest(name)
                except UpdateError as e:
                    cache[name] = e
                    if "limiting" in str(e) or "could not reach" in str(e):
                        stop_asking = str(e)
            rel = cache[name]
            if isinstance(rel, UpdateError):
                g["problems"].append(f"{name}: {rel}")
                errors += 1
                if name in BASE_ADDONS:
                    base_blocked = True
                continue
            g["checked"] += 1
            if rel is None or not is_newer(rel["version"], version):
                continue
            if name not in BASE_ADDONS and base_blocked:
                g["waiting"].append(f"{name} {rel['version']} (after the base add-on)")
                continue
            if running(game):
                g["waiting"].append(f"{name} {rel['version']}")
                if name in BASE_ADDONS:
                    base_blocked = True
                continue
            log(f"add-ons: updating {game.name}/{name} {version} -> {rel['version']}")
            try:
                new = update(install, game, name, folder, rel, running=running, log=log)
            except UpdateError as e:
                g["problems"].append(f"{name}: {e}")
                errors += 1
                if name in BASE_ADDONS:
                    base_blocked = True
                log(f"add-ons: {game.name}/{name} not updated: {e}")
                continue
            except Exception as e:                                # never let one add-on take the run down
                g["problems"].append(f"{name}: {type(e).__name__}")
                errors += 1
                if name in BASE_ADDONS:
                    base_blocked = True
                log(f"add-ons: {game.name}/{name} not updated: {type(e).__name__}: {e}")
                continue
            g["updated"].append(f"{name} to {new}")
            if name in BASE_ADDONS:
                updated_base = True
            log(f"add-ons: updated {game.name}/{name} to {new}")
        if g["waiting"]:
            log(f"add-ons: {game.name}: waiting for WoW to close: {', '.join(g['waiting'])}")
    return {"games": out, "updated_base": updated_base, "errors": errors}


# --------------------------------------------------------------------------
# State, the Settings lines and the background loop
# --------------------------------------------------------------------------

def _state_path(install):
    return Path(install) / STATE_NAME


def load_state(install):
    try:
        d = json.loads(_state_path(install).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(install, state):
    p = _state_path(install)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def due(state, now=None, every=CHECK_EVERY):
    now = time.time() if now is None else now
    last = state.get("last_check", 0) or 0
    wait = RETRY_AFTER_FAILURE if (state.get("errors") or state.get("waiting")) else every
    return now - last >= min(wait, every)


def status_lines(install, enabled=True):
    """The add-on update status for the Settings screen, one line per game folder."""
    if not enabled:
        return ["Add-on updates: off"]
    st = load_state(install)
    games = st.get("games") or {}
    if not st.get("last_check"):
        return ["Add-on updates: not checked yet (the background service looks every few hours)"]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["last_check"]))
    lines = []
    for gname, g in sorted(games.items()):
        parts = []
        if g.get("updated"):
            parts.append("updated " + ", ".join(g["updated"]))
        if g.get("waiting"):
            parts.append("waiting for WoW to close: " + ", ".join(g["waiting"]))
        if g.get("problems"):
            parts.append("not updated: " + "; ".join(g["problems"][:3]) + (" ..." if len(g["problems"]) > 3 else ""))
        if not parts:
            parts.append("up to date" if g.get("checked") or not g.get("skipped") else "nothing to update")
        if g.get("skipped"):
            parts.append("left alone: " + ", ".join(g["skipped"]))
        lines.append(f"Add-ons ({gname}): " + "; ".join(parts))
    if not lines:
        lines.append("Add-on updates: no SpeakStone add-on found")
    lines.append(f"Add-ons last checked: {when}")
    return lines


_RUN_LOCK = threading.Lock()
LOCK_NAME = "addon_update.lock"


def _lock_file(install):
    """Hold a lock across processes (the service and a menu window may both run an update): an exclusive lock on the
    first byte of addon_update.lock. -> open file to keep, or None when another process holds it."""
    p = Path(install) / LOCK_NAME
    try:
        fh = open(p, "a+b")
    except OSError:
        return None
    try:
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _unlock_file(fh):
    try:
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    fh.close()


def check_and_update(install, wow_folder="", roots=None, log=lambda s: None, **kw):
    """One full run, saved to the state file. -> the run's result, or None when another run is in progress."""
    from . import wowtext
    if not _RUN_LOCK.acquire(blocking=False):
        return None
    held = _lock_file(install)
    if held is None:
        _RUN_LOCK.release()
        return None
    try:
        games = wowtext.find_games(roots if roots is not None else wowtext.candidate_roots(wow_folder or None))
        res = run(install, games, log=log, **kw)
        state = {"last_check": time.time(), "errors": res["errors"],
                 "waiting": sum(len(g["waiting"]) for g in res["games"].values()), "games": res["games"]}
        try:
            save_state(install, state)
        except OSError:
            pass
        return res
    finally:
        _unlock_file(held)
        _RUN_LOCK.release()


def maybe_update(install, settings, log=lambda s: None, now=None, runner=check_and_update):
    """For the background loop: run when the setting is on and a look is due. -> True when a base add-on was replaced
    (its WorkerReceipt.lua is then the empty shipped one, so the WoW-text send should run straight after)."""
    if not settings["update_addons"] or not due(load_state(install), now):
        return False
    res = runner(install, settings["wow_folder"], log=log)
    if res is None:
        return False
    n = sum(len(g["updated"]) for g in res["games"].values())
    log(f"add-ons: checked, {n} updated" + (f", {res['errors']} problems" if res["errors"] else ""))
    return bool(res["updated_base"])
