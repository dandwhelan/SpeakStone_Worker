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

On by default since 1.2.23 (owner 2026-11-24; settings.MIGRATIONS switches it on once on older PCs). The player can turn
it off in Settings [W], or run it by hand (menu [4]). Settings also shows what was found (survey()): every game folder,
the add-on version in each, and how many captured entries are waiting to be sent.
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
# Game folders Battle.net makes inside one install, by name, for the screen. Anything else is shown by its folder name.
GAME_NAMES = {"_retail_": "Retail", "_forever_": "WoW Forever", "_classic_": "Classic", "_classic_era_": "Classic Era",
              "_anniversary_": "Anniversary", "_ptr_": "Retail PTR", "_xptr_": "Retail beta/PTR", "_beta_": "Beta",
              "_classic_ptr_": "Classic PTR", "_classic_era_ptr_": "Classic Era PTR"}
# Test realms: their text is not the live game's, and the add-on's file there would be sent as "retail". Never sent.
TEST_REALM = re.compile(r"ptr|beta|alpha", re.I)
# What the in-game export carries (Harvester.lua HarvestExportBatches). "speakers" (who says a voiced quest line that has
# no speaker on record, add-on 6.1.x) was missing until 1.2.23, so those reports never left a worker PC.
KINDS = ("quests", "gossip", "itemText", "npcChat", "bugs", "speakers")
QUEST_PASSAGES = ("description", "progress", "completion")
RECEIPT_NAME = "WorkerReceipt.lua"      # ships empty in add-on 6.1.3+; see write_receipt()
RECEIPT_MAX = 200_000                   # hashes in one receipt; far above any real store
BATCH_BYTES = 1536 * 1024          # under the site's 2 MB export limit, with room for the header
MAX_SV_BYTES = 64 * 1024 * 1024    # a saved-variables file bigger than this is not one the add-on wrote
AUTO_EVERY = 3 * 3600              # the automatic send looks at most this often
STATE_NAME = "wowtext_sent.json"


# --------------------------------------------------------------------------
# Finding the files
# --------------------------------------------------------------------------

def _install_root(p):
    """InstallPath names the _retail_ folder on a modern install; the root is one up."""
    p = Path(p)
    return p.parent if p.name.startswith("_") and p.name.endswith("_") else p


def _registry_roots():
    if sys.platform != "win32":
        return []
    out = []
    try:
        import winreg
    except ImportError:
        return out
    for sub in (r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
                r"SOFTWARE\Blizzard Entertainment\World of Warcraft"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                path, _ = winreg.QueryValueEx(k, "InstallPath")
                if path:
                    out.append(_install_root(path))
        except OSError:
            pass
    # Battle.net's uninstall entries name every WoW product it installed, Classic and Forever included, wherever the
    # player put them ("World of Warcraft", "World of Warcraft Classic Era", ...).
    for sub in (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                for i in range(4096):
                    try:
                        name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(k, name) as app:
                            shown = winreg.QueryValueEx(app, "DisplayName")[0]
                            if isinstance(shown, str) and shown.startswith("World of Warcraft"):
                                where = winreg.QueryValueEx(app, "InstallLocation")[0]
                                if where:
                                    out.append(_install_root(where))
                    except OSError:
                        pass
        except OSError:
            pass
    return out


_PRODUCT_DB_PATH = re.compile(rb"[A-Za-z]:[/\\][^\x00-\x1f\"*?<>|]{2,240}")


def _battlenet_roots(env=None):
    """Install folders listed in Battle.net's own product list (ProgramData/Battle.net/Agent/product.db). It is a
    protobuf file; only the paths are wanted, so they are picked out of the bytes, and only folders that look like a
    WoW install (one with a game folder holding WTF, or WTF itself) are kept."""
    if sys.platform != "win32":
        return []
    env = os.environ if env is None else env
    db = Path(env.get("ProgramData") or r"C:\ProgramData") / "Battle.net" / "Agent" / "product.db"
    try:
        if db.stat().st_size > 4 * 1024 * 1024:
            return []
        raw = db.read_bytes()
    except OSError:
        return []
    out = []
    for m in _PRODUCT_DB_PATH.finditer(raw):
        try:
            p = _install_root(m.group(0).decode("utf-8"))
        except UnicodeDecodeError:
            continue
        if "warcraft" in str(p).lower():
            out.append(p)
    return out


def _local_fixed_drives():
    """Drive roots worth guessing at: local hard disks only. A mapped network drive or a card reader can take seconds
    to answer each question (and the guess list asks five per drive), every time the send runs."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        get_type = ctypes.windll.kernel32.GetDriveTypeW
    except (ImportError, AttributeError, OSError):
        return [f"{d}:\\" for d in "CDEFG" if os.path.isdir(f"{d}:\\")]
    out = []
    for i, d in enumerate(string.ascii_uppercase):
        if mask & (1 << i) and get_type(f"{d}:\\") == 3:        # 3 = DRIVE_FIXED
            out.append(f"{d}:\\")
    return out


def candidate_roots(extra=None):
    """WoW install folders worth looking in: the setting, the registry and Battle.net's product list, then the usual
    places on every local disk."""
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

    for p in ([extra] if extra else []) + _registry_roots() + _battlenet_roots():
        add(p)
    if sys.platform == "win32":
        for d in _local_fixed_drives():
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


def find_games(roots):
    """Every game folder (one with WTF or Interface: _retail_, _forever_, _classic_era_, ...) under the roots, once each.
    A root may be the install folder or one game folder itself."""
    seen, games = set(), []

    def is_game(p):
        return (p / "WTF").is_dir() or (p / "Interface").is_dir()

    for root in roots:
        try:
            found = [root] if is_game(root) else [g for g in sorted(root.iterdir()) if g.is_dir() and is_game(g)]
        except OSError:
            continue
        for g in found:
            try:
                key = os.path.normcase(str(g.resolve()))
            except (OSError, ValueError):
                continue
            if key not in seen:
                seen.add(key)
                games.append(g)
    return games


def is_test_realm(game):
    return bool(TEST_REALM.search(game.name))


def _saved_variables_in(game):
    out = []
    accounts = game / "WTF" / "Account"
    try:
        accts = sorted(a for a in accounts.iterdir() if a.is_dir()) if accounts.is_dir() else []
    except OSError:
        return out
    for acct in accts:
        sv = acct / "SavedVariables"
        for fname, (glob_name, flavour) in ADDONS.items():
            f = sv / fname
            # is_file() follows a link; a saved-variables file is never one, so a link is not read (it could point
            # anywhere on the PC, and its contents would go to the site).
            if f.is_file() and not f.is_symlink():
                out.append((f, glob_name, flavour, game))
    return out


def find_saved_variables(roots):
    """[(path, global name, flavour, game folder)] for every SpeakStone saved-variables file under the roots.
    Test realms (PTR, beta) are left out: their text is not the live game's."""
    found = []
    for game in find_games(roots):
        if not is_test_realm(game):
            found.extend(_saved_variables_in(game))
    return found


# --------------------------------------------------------------------------
# Reading Lua tables (the subset WoW writes) without running anything
# --------------------------------------------------------------------------

class LuaError(ValueError):
    pass


_NUM = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEC_ESC = re.compile(r"\d{1,3}")
_PLAIN = {q: re.compile(r"[^\\" + q + r"]+") for q in "\"'"}     # a run of characters with no escape and no closing quote
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
        plain = _PLAIN[q]
        pos += 1
        buf = []
        # Whole runs of plain text at a time: a saved-variables file is mostly long strings, and one character per
        # step made a few-MB file take seconds. A \ddd escape is matched in place (slicing text[pos:] for it copied the
        # rest of the file each time).
        while pos < n:
            m = plain.match(text, pos)
            if m:
                buf.append(m.group(0))
                pos = m.end()
                continue
            c = text[pos]
            if c == q:
                pos += 1
                return "".join(buf)
            pos += 1                                  # a backslash
            e = text[pos] if pos < n else ""
            m = _DEC_ESC.match(text, pos)
            if m:
                buf.append(chr(min(int(m.group(0)), 0x10FFFF)))
                pos = m.end()
                continue
            buf.append(_ESC.get(e, e))
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


# --------------------------------------------------------------------------
# Clearing what was sent: the receipt the add-on reads at login
# --------------------------------------------------------------------------

def _lua_str(v):
    """tostring() of a key or value, as Lua 5.1 writes it inside table.concat."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, float):
        return "%.14g" % v
    return str(v)


def hash_text(text):
    """The add-on's HashText (SpeakStone_Main.lua): h = (h * 31 + byte) mod 2^32 over the UTF-8 bytes."""
    h = 0
    for b in text.encode("utf-8", errors="surrogatepass"):
        h = (h * 31 + b) % 4294967296
    return h


def sent_key(*parts):
    """The add-on's SentKey(...): HashText of the parts joined by \\31."""
    return hash_text("\x1f".join(_lua_str(p) for p in parts))


def _texts(lst):
    """An array the parser read as {1: a, 2: b, ...}, in order."""
    if not isinstance(lst, dict):
        return []
    return [lst[i] for i in sorted(k for k in lst if isinstance(k, int) and not isinstance(k, bool))]


def entry_sent_keys(kind, key, entry):
    """Every SentKey the add-on would make for one stored entry (Harvester.lua HarvestSnapshot)."""
    if not isinstance(entry, dict):
        return []
    out = []
    if kind == "quests":
        for p in QUEST_PASSAGES:
            c = entry.get(p)
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                out.append(sent_key("q", key, p, c["text"]))
    elif kind in ("gossip", "npcChat"):
        tag = "g" if kind == "gossip" else "c"
        out += [sent_key(tag, key, t) for t in _texts(entry.get("texts")) if isinstance(t, str)]
    elif kind == "itemText":
        pages = entry.get("pages")
        if isinstance(pages, dict):
            out += [sent_key("i", key, page, t) for page, t in pages.items() if isinstance(t, str)]
    elif kind == "bugs":
        t = entry.get("text")
        out.append(sent_key("b", key, t if isinstance(t, str) else ""))
    elif kind == "speakers":
        if entry.get("npcID") is not None:
            out.append(sent_key("s", key, entry["npcID"]))
    return out


def confirmed_keys(harvest, flavour, sent):
    """SentKeys of every stored entry the site has confirmed (its fingerprint is in `sent`)."""
    keys = set()
    for kind in KINDS:
        tbl = harvest.get(kind) if isinstance(harvest, dict) else None
        if not isinstance(tbl, dict):
            continue
        for k, entry in tbl.items():
            piece = entry_text(k, entry)
            if piece is not None and fingerprint(flavour, kind, piece) in sent:
                keys.update(entry_sent_keys(kind, k, entry))
    return keys


def receipt_path(game, flavour):
    return game / "Interface" / "AddOns" / TOC[flavour] / RECEIPT_NAME


def write_receipt(game, flavour, keys, now=None):
    """Tell the add-on which captured lines the site has, so it clears them at the next login (Harvester.lua
    ApplyWorkerReceipt). -> True when written.

    Only into an add-on that ships the empty WorkerReceipt.lua (6.1.3+): an older add-on would never read it, and a
    file of ours appearing in a folder the game loads would be a surprise. WoW runs this file, so it holds integers we
    computed and nothing else, and is swapped in whole (a temp file beside it, then a rename) so the game can never
    read half of one. Writing while WoW is running is fine: the game reads it only at login. The receipt lists every
    confirmed line still in the store, so it is the same whatever order runs happen in, and an add-on update (which
    puts the empty file back) only delays the clearing to the next run."""
    p = receipt_path(game, flavour)
    tmp = None
    try:
        if not p.is_file() or p.is_symlink() or p.stat().st_size > 8 * 1024 * 1024:
            return False
        ks = sorted(int(k) for k in keys if isinstance(k, int) and 0 <= k < 4294967296)[:RECEIPT_MAX]
        lines = ["-- Written by SpeakStone Worker: captured lines the website has confirmed. The add-on clears them\n",
                 "-- at the next login. Rewritten on every run; safe to delete.\n",
                 f"SpeakStone_WorkerReceipt = {{ written = {int(now or time.time())}, sent = {{\n"]
        for i in range(0, len(ks), 12):
            lines.append(",".join(str(k) for k in ks[i:i + 12]) + ",\n")
        lines.append("} }\n")
        text = "".join(lines)
        try:
            if p.read_text(encoding="utf-8", errors="replace").split("\n", 3)[-1] == text.split("\n", 3)[-1]:
                return True                          # unchanged apart from the time: leave the file alone
        except OSError:
            pass
        tmp = p.with_name(f".{RECEIPT_NAME}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        for attempt in range(10):
            try:
                os.replace(tmp, p)
                return True
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    return False


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
    # Only a clear yes counts as sent: what is sent is later cleared from the add-on (write_receipt), so a reply that
    # is not the site's JSON object (a proxy page, a captive portal) must not be taken for one.
    if not isinstance(data, dict) or not (isinstance(data.get("counts"), dict) or data.get("submission_id")):
        return False, "The site did not confirm it."
    counts = data.get("counts")
    return True, counts if isinstance(counts, dict) else {}


def collect_and_send(install, base, key, wow_folder="", poster=post_export, roots=None, say=print):
    """Find every SpeakStone saved-variables file, send what is new. -> summary dict."""
    summary = {"files": 0, "entries": 0, "skipped": 0, "uploads": 0, "new": 0, "receipts": 0, "errors": []}
    stores = []                                       # (game, flavour, harvest) read this run, for the receipts
    files = find_saved_variables(roots if roots is not None else candidate_roots(wow_folder or None))
    summary["files"] = len(files)
    sent = load_sent(install)
    if not files:
        say("No SpeakStone saved text found. Is WoW installed somewhere unusual? Set its folder in Settings [F].")
        # Remember the look, or the automatic send (which waits AUTO_EVERY after the last run) would search every
        # disk again 5 minutes later, for ever, on a PC with no WoW at all (every rented GPU, most Linux boxes).
        try:
            save_sent(install, sent)
        except OSError:
            pass
        return summary
    for path, glob_name, flavour, game in files:
        try:
            harvest = read_harvest(path, glob_name)
        except (OSError, LuaError) as e:
            summary["errors"].append(f"{path.name}: could not read it ({e})")
            continue
        if not isinstance(harvest, dict):
            continue
        batches, prints, skipped = build_batches(harvest, game_meta(game, flavour), sent, flavour)
        summary["skipped"] += skipped
        where = f"{game.name} / {path.parent.parent.name}"
        stores.append((game, flavour, harvest))
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

    # Clear what the site confirmed: one receipt per add-on folder, covering every account's store in that game.
    by_addon = {}
    for game, flavour, harvest in stores:
        by_addon.setdefault((os.path.normcase(str(game)), flavour), (game, flavour, set()))[2].update(
            confirmed_keys(harvest, flavour, sent))
    for game, flavour, keys in by_addon.values():
        if write_receipt(game, flavour, keys):
            summary["receipts"] += 1
            if keys:
                say(f"{game.name}: {len(keys)} sent lines will be cleared from the add-on at your next login.")
        elif keys and receipt_path(game, flavour).parent.is_dir() and not receipt_path(game, flavour).is_file():
            say(f"{game.name}: update the SpeakStone add-on so it can clear lines that were already sent.")
    return summary


_HARVEST_CACHE = {}          # path -> ((size, mtime), harvest): the Settings screen redraws often; WoW rewrites rarely
_CACHE_LOCK = threading.Lock()


def read_harvest(path, glob_name):
    """The add-on's `<DB>.harvest` table from one saved-variables file, or None. Raises OSError / LuaError.
    Parsed once per file version: the same file is not read again until WoW rewrites it."""
    st = path.stat()
    if st.st_size > MAX_SV_BYTES:
        raise LuaError("too big to be the add-on's file")
    stamp = (st.st_size, st.st_mtime_ns)
    key = os.path.normcase(str(path))
    with _CACHE_LOCK:
        hit = _HARVEST_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    with open(path, "rb") as f:
        raw = f.read(MAX_SV_BYTES + 1)                 # the size may have grown since stat(): never read past the cap
    if len(raw) > MAX_SV_BYTES:
        raise LuaError("too big to be the add-on's file")
    # read_text() used to turn \r\n into \n; keep that, or an escaped line break would come out as \r\n in the text
    db = parse_saved_variables(raw.decode("utf-8", errors="replace").replace("\r\n", "\n")).get(glob_name)
    harvest = db.get("harvest") if isinstance(db, dict) else None
    with _CACHE_LOCK:
        if len(_HARVEST_CACHE) > 32:
            _HARVEST_CACHE.clear()
        _HARVEST_CACHE[key] = (stamp, harvest)
    return harvest


def _count_entries(harvest, flavour, sent):
    """-> (captured entries, entries not sent yet), by the same fingerprints the send uses."""
    total = waiting = 0
    for kind in KINDS:
        tbl = harvest.get(kind) if isinstance(harvest, dict) else None
        if not isinstance(tbl, dict):
            continue
        for k, entry in tbl.items():
            piece = entry_text(k, entry)
            if piece is None:
                continue
            total += 1
            if fingerprint(flavour, kind, piece) not in sent:
                waiting += 1
    return total, waiting


def _pack_count(game):
    try:
        return sum(1 for p in (game / "Interface" / "AddOns").iterdir()
                   if p.is_dir() and p.name.startswith(("SpeakStone_Pack_", "SpeakStone_Forever_")) and
                   not p.name.startswith("SpeakStone_Forever_Main") and "." not in p.name)   # .old / .stale copies
    except OSError:
        return 0


def survey(install, wow_folder="", roots=None):
    """What the send would find, for the Settings screen. Reads nothing it would not send, sends nothing.
    -> [{"folder", "game", "name", "test_realm", "addons": {flavour: version or None}, "packs",
         "files": [{"account", "flavour", "entries", "waiting", "error"}]}], one per game folder."""
    sent = load_sent(install)
    out = []
    for game in find_games(roots if roots is not None else candidate_roots(wow_folder or None)):
        addons = {}
        for flavour, folder in TOC.items():
            toc = game / "Interface" / "AddOns" / folder / f"{folder}.toc"
            if toc.is_file():
                addons[flavour] = game_meta(game, flavour).get("addonVersion") or "?"
        row = {"folder": str(game.parent), "game": game.name, "name": GAME_NAMES.get(game.name, game.name),
               "test_realm": is_test_realm(game), "addons": addons, "packs": _pack_count(game), "files": []}
        for path, glob_name, flavour, _ in _saved_variables_in(game):
            f = {"account": path.parent.parent.name, "flavour": flavour, "entries": 0, "waiting": 0, "error": ""}
            try:
                f["entries"], f["waiting"] = _count_entries(read_harvest(path, glob_name), flavour, sent)
            except (OSError, LuaError) as e:
                f["error"] = str(e)[:120]
            row["files"].append(f)
        out.append(row)
    return out


def survey_lines(rows, last=None):
    """The survey as plain sentences, one game folder per line plus one per saved file under it."""
    if not rows:
        return ["No WoW folder found. Set it with [F]."]
    lines = []
    for r in rows:
        if r["addons"]:
            vers = ", ".join(f"{'SpeakStone' if fl == 'retail' else 'SpeakStone Forever'} {v}"
                             for fl, v in r["addons"].items())
            packs = f", {r['packs']} voice pack{'s' if r['packs'] != 1 else ''}" if r["packs"] else ", no voice packs"
            head = f"{r['name']} ({r['game']}): {vers}{packs}"
        else:
            head = f"{r['name']} ({r['game']}): no SpeakStone add-on"
        lines.append(f"{head}   [{r['folder']}]")
        if r["test_realm"] and r["files"]:
            lines.append("    test realm: its captured text is not sent")
            continue
        if r["addons"] and not r["files"]:
            lines.append("    nothing captured yet (WoW saves it when you log out or type /reload)")
        for f in r["files"]:
            if f["error"]:
                lines.append(f"    {f['account']}: could not read it ({f['error']})")
            else:
                lines.append(f"    {f['account']}: {f['entries']} captured, {f['waiting']} waiting to be sent")
    if last:
        lines.append(f"Last looked: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}")
    return lines


def auto_loop(install, settings, base_fn, key_fn, stop=None, log=lambda s: None, every=AUTO_EVERY, sleep=time.sleep,
              before=None):
    """The service's background send: when Settings [W] is on, at most every `every` seconds.
    `before` (the add-on update, Settings [A]) runs first on every turn; when it replaced a base add-on its
    WorkerReceipt.lua is the empty shipped copy, so the send runs straight after to write the receipt again."""
    while not (stop and stop.is_set()):
        force = False
        if before is not None:
            try:
                force = bool(before())
            except Exception as e:                   # never let the update take the send (or the worker) down
                log(f"add-ons: {type(e).__name__}: {e}")
        try:
            settings.reload()
            if settings["send_wow_text"] and (force or time.time() - last_run(install) >= every):
                key = key_fn()
                if key:
                    s = collect_and_send(install, base_fn(), key, settings["wow_folder"], say=log)
                    log(f"wow text: {s['files']} files, {s['entries']} entries sent in {s['uploads']} uploads"
                        + (f", {len(s['errors'])} problems" if s["errors"] else ""))
        except Exception as e:                       # never let this take the worker down
            log(f"wow text: {type(e).__name__}: {e}")
        sleep(300)


def start_auto(install, settings, base_fn, key_fn, log=lambda s: None, before=None):
    t = threading.Thread(target=auto_loop, args=(install, settings, base_fn, key_fn),
                         kwargs={"log": log, "before": before}, daemon=True)
    t.start()
    return t
