"""Send the quest, gossip and book text the SpeakStone add-on captured, straight from this PC's WoW folder.

Owner 2026-11-16: the worker "goes into the addon folder on the PC" and collects the text for lines that have no voice
yet, so a player need not open the in-game export window and paste it on the website.

The add-on keeps what it captures in its saved variables (WTF/Account/<account>/SavedVariables/SpeakStone_Main.lua for
retail, SpeakStone_Forever_Main.lua for WoW Forever), under `<DB>.harvest`. This module reads those files (never
writes them, never runs them: a small parser reads only Lua tables), builds an export in the same shape as the in-game
window's (SpeakStone_MainExport = {locale, build, flavour, ..., quests, gossip, itemText, npcChat, bugs}) and POSTs it
to /api/worker/harvest with the worker key.

What it does not do:
- It cannot mark lines "sent" inside the add-on: WoW rewrites saved variables on logout, so an edit would be lost or
  would clobber the game's own save. Instead it remembers a fingerprint of every entry it sent (wowtext_sent.json in
  the app's folder) and sends an entry again only when the add-on has captured more of it.
- WoW writes saved variables only on logout or /reload, so a line captured in the current session arrives later.

Off unless the player turns it on (Settings [W]), or runs it by hand (menu [4]).
"""
import hashlib
import json
import os
import re
import string
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .net import open_url

# add-on saved-variables file -> (its global, the site's name for the game)
ADDONS = {
    "SpeakStone_Main.lua": ("SpeakStone_MainDB", "retail"),
    "SpeakStone_Forever_Main.lua": ("SpeakStone_ForeverMainDB", "forever"),
}
TOC = {"retail": "SpeakStone_Main", "forever": "SpeakStone_Forever_Main"}
KINDS = ("quests", "gossip", "itemText", "npcChat", "bugs")
BATCH_BYTES = 1536 * 1024          # under the site's 2 MB export limit, with room for the header
MAX_SV_BYTES = 64 * 1024 * 1024    # a saved-variables file bigger than this is not one the add-on wrote
AUTO_EVERY = 3 * 3600              # the automatic send looks at most this often
STATE_NAME = "wowtext_sent.json"


# --------------------------------------------------------------------------
# Finding the files
# --------------------------------------------------------------------------

def _registry_roots():
    if sys.platform != "win32":
        return []
    out = []
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Blizzard Entertainment\World of Warcraft")):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    path, _ = winreg.QueryValueEx(k, "InstallPath")
                    if path:
                        p = Path(path)
                        # InstallPath names the _retail_ folder on a modern install; the root is one up.
                        out.append(p.parent if p.name.startswith("_") and p.name.endswith("_") else p)
            except OSError:
                pass
    except ImportError:
        pass
    return out


def candidate_roots(extra=None):
    """WoW install folders worth looking in: the setting, the registry, then the usual places on every drive."""
    seen, roots = set(), []

    def add(p):
        try:
            p = Path(p)
            key = os.path.normcase(str(p.resolve()))
        except (OSError, ValueError):
            return
        if key not in seen and p.is_dir():
            seen.add(key)
            roots.append(p)

    for p in ([extra] if extra else []) + _registry_roots():
        add(p)
    if sys.platform == "win32":
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
        for d in drives:
            for rel in (r"Program Files (x86)\World of Warcraft", r"Program Files\World of Warcraft",
                        "World of Warcraft", r"Games\World of Warcraft", r"Battle.net\World of Warcraft"):
                add(Path(d) / rel)
    else:
        home = Path.home()
        for rel in ("Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft",
                    ".wine/drive_c/Program Files (x86)/World of Warcraft",
                    "Games/battlenet/drive_c/Program Files (x86)/World of Warcraft"):
            add(home / rel)
    return roots


def find_saved_variables(roots):
    """[(path, global name, flavour, game folder)] for every SpeakStone saved-variables file under the roots.
    A root may be the install folder (with _retail_, _forever_, ...) or one game folder itself."""
    found = []
    for root in roots:
        games = [root] if (root / "WTF").is_dir() else [g for g in sorted(root.iterdir())
                                                         if g.is_dir() and (g / "WTF").is_dir()] if root.is_dir() else []
        for game in games:
            accounts = game / "WTF" / "Account"
            if not accounts.is_dir():
                continue
            for acct in sorted(accounts.iterdir()):
                sv = acct / "SavedVariables"
                for fname, (glob_name, flavour) in ADDONS.items():
                    f = sv / fname
                    if f.is_file():
                        found.append((f, glob_name, flavour, game))
    return found


# --------------------------------------------------------------------------
# Reading Lua tables (the subset WoW writes) without running anything
# --------------------------------------------------------------------------

class LuaError(ValueError):
    pass


_NUM = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESC = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
        "\\": "\\", '"': '"', "'": "'", "\n": "\n"}


def parse_saved_variables(text):
    """{global name: value} for a saved-variables file. Tables become dicts (array parts keyed 1, 2, ...)."""
    pos, n = 0, len(text)
    out = {}

    def err(msg):
        raise LuaError(f"line {text.count(chr(10), 0, pos) + 1}: {msg}")

    def skip():
        nonlocal pos
        while pos < n:
            c = text[pos]
            if c in " \t\r\n,;":
                pos += 1
            elif text.startswith("--", pos):
                if text.startswith("--[[", pos):
                    e = text.find("]]", pos)
                    pos = n if e < 0 else e + 2
                else:
                    e = text.find("\n", pos)
                    pos = n if e < 0 else e + 1
            else:
                break

    def string_lit():
        nonlocal pos
        q = text[pos]
        pos += 1
        buf = []
        while pos < n:
            c = text[pos]
            if c == q:
                pos += 1
                return "".join(buf)
            if c == "\\":
                pos += 1
                e = text[pos] if pos < n else ""
                if e.isdigit():
                    m = re.match(r"\d{1,3}", text[pos:])
                    buf.append(chr(int(m.group(0))))
                    pos += len(m.group(0))
                    continue
                buf.append(_ESC.get(e, e))
                pos += 1
                continue
            buf.append(c)
            pos += 1
        err("unterminated string")

    def value(depth=0):
        nonlocal pos
        if depth > 60:
            err("tables nested too deep")
        skip()
        if pos >= n:
            err("value expected")
        c = text[pos]
        if c == "{":
            return table(depth + 1)
        if c in "\"'":
            return string_lit()
        for word, v in (("true", True), ("false", False), ("nil", None)):
            if text.startswith(word, pos) and not (text[pos + len(word):pos + len(word) + 1].isalnum()):
                pos += len(word)
                return v
        m = _NUM.match(text, pos)
        if m:
            pos = m.end()
            s = m.group(0)
            if s.lower().startswith(("0x", "-0x")):
                return int(s, 16)
            f = float(s)
            return int(f) if f.is_integer() and not re.search(r"[.eE]", s) else f
        err(f"unexpected {c!r}")

    def table(depth):
        nonlocal pos
        pos += 1                                    # {
        t, idx = {}, 1
        while True:
            skip()
            if pos >= n:
                err("unterminated table")
            if text[pos] == "}":
                pos += 1
                return t
            if text[pos] == "[":
                pos += 1
                k = value(depth)
                skip()
                if text[pos:pos + 1] != "]":
                    err("] expected")
                pos += 1
                skip()
                if text[pos:pos + 1] != "=":
                    err("= expected")
                pos += 1
                t[k] = value(depth)
            else:
                m = _NAME.match(text, pos)
                save = pos
                if m:
                    pos = m.end()
                    skip()
                    if text[pos:pos + 1] == "=" and text[pos:pos + 2] != "==":
                        pos += 1
                        t[m.group(0)] = value(depth)
                        continue
                    pos = save
                t[idx] = value(depth)
                idx += 1

    while True:
        skip()
        if pos >= n:
            return out
        m = _NAME.match(text, pos)
        if not m:
            err("name expected")
        pos = m.end()
        skip()
        if text[pos:pos + 1] != "=":
            err("= expected")
        pos += 1
        out[m.group(0)] = value()


# --------------------------------------------------------------------------
# Writing the export (the in-game window's shape, Harvester.lua HarvestExportBatches)
# --------------------------------------------------------------------------

_LUA_ESC = {"\\": "\\\\", '"': '\\"', "\r": "\\r", "\n": "\\n"}


def _esc(s):
    return re.sub(r'[\\"\r\n]', lambda m: _LUA_ESC[m.group(0)], s)


def _key(k):
    if isinstance(k, bool):
        return None
    if isinstance(k, (int, float)):
        return f"[{int(k) if float(k).is_integer() else k}]"
    if isinstance(k, str):
        return f'["{_esc(k)}"]'
    return None


def serialize(out, tbl, indent):
    for k, v in tbl.items():
        ks = _key(k)
        if ks is None:
            continue
        if isinstance(v, dict):
            out.append(f"{indent}{ks} = {{\n")
            serialize(out, v, indent + "  ")
            out.append(f"{indent}}},\n")
        elif isinstance(v, str):
            out.append(f'{indent}{ks} = "{_esc(v)}",\n')
        elif isinstance(v, bool):
            out.append(f"{indent}{ks} = {'true' if v else 'false'},\n")
        elif isinstance(v, (int, float)):
            out.append(f"{indent}{ks} = {v},\n")


def entry_text(key, entry):
    ks = _key(key)
    if ks is None:
        return None
    out = [f"    {ks} = {{\n"]
    serialize(out, entry if isinstance(entry, dict) else {}, "      ")
    out.append("    },\n")
    return "".join(out)


def fingerprint(flavour, kind, piece):
    return hashlib.sha256(f"{flavour}\x1f{kind}\x1f{piece}".encode("utf-8")).hexdigest()[:32]


def game_meta(game, flavour):
    """locale, build, interface and add-on version, read from the game folder (the in-game export asks the client)."""
    meta = {"locale": "enUS", "flavour": flavour}
    try:
        cfg = (game / "WTF" / "Config.wtf").read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^SET textLocale "([A-Za-z]{4})"', cfg, re.M)
        if m:
            meta["locale"] = m.group(1)
    except OSError:
        pass
    toc = game / "Interface" / "AddOns" / TOC[flavour] / f"{TOC[flavour]}.toc"
    try:
        t = toc.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^## Version:\s*(\S+)", t, re.M)
        if m:
            meta["addonVersion"] = m.group(1)
        m = re.search(r"^## Interface:\s*(\d+)", t, re.M)
        if m:
            meta["interface"] = int(m.group(1))
    except OSError:
        pass
    # .build.info sits in the install folder: one row per product, the Version column is the build
    try:
        info = (game.parent / ".build.info").read_text(encoding="utf-8", errors="replace").splitlines()
        cols = [c.split("!")[0] for c in info[0].split("|")]
        want = {"_retail_": "wow", "_forever_": "wowf"}.get(game.name)
        for row in info[1:]:
            vals = row.split("|")
            rec = dict(zip(cols, vals))
            if want and rec.get("Product") == want and rec.get("Version"):
                meta["build"] = rec["Version"].rsplit(".", 1)[-1]
                break
    except (OSError, IndexError):
        pass
    return meta


def build_batches(harvest, meta, sent, flavour, max_bytes=BATCH_BYTES):
    """-> (list of export strings, list of fingerprint lists, one per string, entries skipped as already sent)."""
    header_tbl = dict(meta)
    for k in ("chars", "npcs"):
        if isinstance(harvest.get(k), dict):
            header_tbl[k] = harvest[k]
    header = []
    serialize(header, header_tbl, "  ")
    header = "".join(header)

    plan, skipped = [], 0
    for kind in KINDS:
        tbl = harvest.get(kind)
        if not isinstance(tbl, dict):
            continue
        for key, entry in tbl.items():
            piece = entry_text(key, entry)
            if piece is None:
                continue
            fp = fingerprint(flavour, kind, piece)
            if fp in sent:
                skipped += 1
                continue
            plan.append((kind, piece, fp))

    batches, prints = [], []
    if not plan:
        return batches, prints, skipped
    out, size, open_kind, fps = None, 0, None, None

    def begin():
        nonlocal out, size, open_kind, fps
        out, size, open_kind, fps = ["SpeakStone_MainExport = {\n", header], 0, None, []
        size = sum(len(s) for s in out)

    def finish():
        if open_kind:
            out.append("  },\n")
        out.append("}")
        batches.append("".join(out))
        prints.append(fps)

    begin()
    for kind, piece, fp in plan:
        opening = f'  ["{kind}"] = {{\n' if open_kind != kind else ""
        need = len(piece.encode("utf-8")) + len(opening) + 6
        if fps and size + need > max_bytes:
            finish()
            begin()
            opening = f'  ["{kind}"] = {{\n'
        if open_kind != kind:
            if open_kind:
                out.append("  },\n")
                size += 5
            out.append(opening)
            size += len(opening)
            open_kind = kind
        out.append(piece)
        size += len(piece.encode("utf-8"))
        fps.append(fp)
    finish()
    return batches, prints, skipped


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def _state_path(install):
    return Path(install) / STATE_NAME


def load_sent(install):
    try:
        d = json.loads(_state_path(install).read_text(encoding="utf-8"))
        return set(d.get("sent", [])) if isinstance(d, dict) else set()
    except (OSError, ValueError):
        return set()


def save_sent(install, sent, last=None):
    p = _state_path(install)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"sent": sorted(sent), "last_run": last or time.time()}), encoding="utf-8")
    os.replace(tmp, p)


def last_run(install):
    try:
        return float(json.loads(_state_path(install).read_text(encoding="utf-8")).get("last_run", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0.0


def post_export(base, key, source, opener=open_url, timeout=60):
    """-> (True, counts dict) or (False, sentence). Never includes the key."""
    req = urllib.request.Request(base.rstrip("/") + "/api/worker/harvest", method="POST",
                                 data=json.dumps({"protocol": 1, "source": source}).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                          "Content-Type": "application/json",
                                          "User-Agent": f"speakstone-worker-cli/{__version__}"})
    try:
        with opener(req, timeout=timeout) as r:
            data = json.loads(r.read(200_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return False, "The site cannot take text from the worker yet. Try again after the next site update."
        try:
            msg = json.loads(e.read(10_000).decode("utf-8")).get("error")
        except Exception:
            msg = None
        return False, (msg.strip()[:200] if isinstance(msg, str) and msg.strip() else f"The site refused it (HTTP {e.code}).")
    except Exception:
        return False, "Could not reach the site."
    counts = data.get("counts") if isinstance(data, dict) else None
    return True, counts if isinstance(counts, dict) else {}


def collect_and_send(install, base, key, wow_folder="", poster=post_export, roots=None, say=print):
    """Find every SpeakStone saved-variables file, send what is new. -> summary dict."""
    summary = {"files": 0, "entries": 0, "skipped": 0, "uploads": 0, "new": 0, "errors": []}
    files = find_saved_variables(roots if roots is not None else candidate_roots(wow_folder or None))
    summary["files"] = len(files)
    if not files:
        say("No SpeakStone saved text found. Is WoW installed somewhere unusual? Set its folder in Settings [W].")
        return summary
    sent = load_sent(install)
    for path, glob_name, flavour, game in files:
        try:
            if path.stat().st_size > MAX_SV_BYTES:
                summary["errors"].append(f"{path.name}: too big to be the add-on's file")
                continue
            data = parse_saved_variables(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, LuaError) as e:
            summary["errors"].append(f"{path.name}: could not read it ({e})")
            continue
        db = data.get(glob_name)
        harvest = db.get("harvest") if isinstance(db, dict) else None
        if not isinstance(harvest, dict):
            continue
        batches, prints, skipped = build_batches(harvest, game_meta(game, flavour), sent, flavour)
        summary["skipped"] += skipped
        where = f"{game.name} / {path.parent.parent.name}"
        if not batches:
            say(f"{where}: nothing new.")
            continue
        for source, fps in zip(batches, prints):
            ok, res = poster(base, key, source)
            if not ok:
                summary["errors"].append(f"{where}: {res}")
                say(f"{where}: {res}")
                break
            sent.update(fps)
            save_sent(install, sent)               # after every upload, so a later failure does not resend this one
            summary["uploads"] += 1
            summary["entries"] += len(fps)
            summary["new"] += int(res.get("new", 0) or 0)
            say(f"{where}: sent {len(fps)} captured entries "
                f"({res.get('new', 0)} new lines, {res.get('known', 0)} already known).")
    save_sent(install, sent)
    return summary


def auto_loop(install, settings, base_fn, key_fn, stop=None, log=lambda s: None, every=AUTO_EVERY, sleep=time.sleep):
    """The service's background send: when Settings [W] is on, at most every `every` seconds."""
    while not (stop and stop.is_set()):
        try:
            settings.reload()
            if settings["send_wow_text"] and time.time() - last_run(install) >= every:
                key = key_fn()
                if key:
                    s = collect_and_send(install, base_fn(), key, settings["wow_folder"], say=log)
                    log(f"wow text: {s['files']} files, {s['entries']} entries sent in {s['uploads']} uploads"
                        + (f", {len(s['errors'])} problems" if s["errors"] else ""))
        except Exception as e:                       # never let this take the worker down
            log(f"wow text: {type(e).__name__}: {e}")
        sleep(300)


def start_auto(install, settings, base_fn, key_fn, log=lambda s: None):
    t = threading.Thread(target=auto_loop, args=(install, settings, base_fn, key_fn), kwargs={"log": log}, daemon=True)
    t.start()
    return t
