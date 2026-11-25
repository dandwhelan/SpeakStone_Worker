"""This worker's label and tier, for the menu, the board and the tray.

Setup writes `enrolled.json` (label and tier, never the key), but a PC set up by an older version, or one whose key was
typed in, has no such file. Then the site is asked once (`GET /api/worker/me`, the key as a bearer token, exactly as the
worker does it) and the answer is saved there. A known label is asked about again at most once a day, in case it was
renamed on the site. Every failure is silent and keeps what was known. The key is read, sent and never stored, printed
or logged here.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

from . import __version__
from .net import open_url

REFRESH_SECONDS = 24 * 3600            # a known label is re-read at most this often
RETRY_SECONDS = 30 * 60                # an unknown label is retried at most this often (background loop)
TIMEOUT = 2.0                          # never hold the menu for longer than this


CONFIG_2, ENROLLED_2 = "worker_config_2.json", "enrolled_2.json"     # the second worker on the same card


def read_key(install, config="worker_config.json"):
    try:
        key = json.loads((Path(install) / config).read_text(encoding="utf-8")).get("worker_key")
    except (OSError, ValueError, AttributeError):
        return ""
    return key if isinstance(key, str) else ""


def cached(install, enrolled="enrolled.json"):
    """What is saved: {"label", "tier", "checked_at", ...} (empty when nothing is)."""
    if not install:
        return {}
    try:
        d = json.loads((Path(install) / enrolled).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def label_of(install):
    return str(cached(install).get("label") or "")


def is_fresh(info, now):
    try:
        age = now - float(info.get("checked_at") or 0)
    except (TypeError, ValueError):
        age = REFRESH_SECONDS
    return bool(info.get("label")) and age < REFRESH_SECONDS


def fetch(base, key, opener=open_url, timeout=TIMEOUT):
    """-> {"label", "tier"} from the site, or None on any failure."""
    req = urllib.request.Request(base.rstrip("/") + "/api/worker/me", method="GET",
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                          "User-Agent": f"speakstone-worker-cli/{__version__}",
                                          "X-Worker-Protocol": "1"})
    try:
        with opener(req, timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                return None
            data = json.loads(r.read(100_000).decode("utf-8"))
    except Exception:                                   # unreachable, refused, slow, not JSON: all the same here
        return None
    if isinstance(data, dict) and isinstance(data.get("worker"), dict):
        data = data["worker"]
    if not isinstance(data, dict) or not isinstance(data.get("label"), str) or not data["label"].strip():
        return None
    tier = data.get("tier")
    return {"label": data["label"].strip()[:80], "tier": tier if isinstance(tier, str) else ""}


def save(install, info, now, enrolled="enrolled.json"):
    """Merge label and tier into enrolled.json (keeping worker_id and the rest). Never touches the key."""
    d = dict(cached(install, enrolled))
    d.update({"label": info["label"], "tier": info.get("tier") or d.get("tier"), "checked_at": now})
    d.pop("key", None)
    path = Path(install) / enrolled
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return d


def refresh(install, base, opener=open_url, now=None, timeout=TIMEOUT, force=False):
    """Ask the site when the label is unknown or a day old and a key exists. -> the saved info (possibly empty)."""
    now = time.time() if now is None else now
    info = cached(install)
    if not force and is_fresh(info, now):
        return info
    key = read_key(install)
    if not key:
        return info                                     # no key, no request
    got = fetch(base, key, opener, timeout)
    return save(install, got, now) if got else info


def describe(install):
    """The sentence for Settings line 4."""
    info = cached(install)
    label = str(info.get("label") or "")
    if label:
        tier = str(info.get("tier") or "")
        return f"{label} ({tier})" if tier else label
    return "(could not ask the site yet)" if read_key(install) else "(not set up yet - choose Start now)"


# ---- renaming the worker (the owner types a name in Settings)
import re  # noqa: E402
import urllib.error  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9 _'.\-]{2,32}$")


def check_name(text):
    """-> (name, "") or (None, why). Trimmed; 2-32 characters of letters, digits, space, - _ ' ."""
    name = (text or "").strip()
    if not NAME_RE.match(name):
        return None, "A name is 2 to 32 characters: letters, digits, spaces, - _ ' and . only."
    return name, ""


def _error_text(err):
    try:
        data = json.loads(err.read(10_000).decode("utf-8"))
        msg = data.get("message") if isinstance(data, dict) else None
        return msg.strip()[:200] if isinstance(msg, str) and msg.strip() else ""
    except Exception:
        return ""


def rename(install, base, name, opener=open_url, timeout=10.0, now=None, config="worker_config.json",
           enrolled="enrolled.json"):
    """POST the new label. -> (True, "Renamed to X") or (False, sentence in plain words). Never includes the key.
    `config` / `enrolled` pick the worker: the defaults are the first one, CONFIG_2 / ENROLLED_2 the second."""
    name, why = check_name(name)
    if not name:
        return False, why
    key = read_key(install, config)
    if not key:
        return False, "This PC is not set up yet - choose Start now first."
    req = urllib.request.Request(base.rstrip("/") + "/api/worker/me", method="POST",
                                 data=json.dumps({"label": name}).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                          "Content-Type": "application/json",
                                          "User-Agent": f"speakstone-worker-cli/{__version__}",
                                          "X-Worker-Protocol": "1"})
    try:
        with opener(req, timeout=timeout) as r:
            status = getattr(r, "status", 200)
            data = json.loads(r.read(100_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return False, "The site cannot rename workers yet. Try again after the next site update."
        if e.code == 409:
            return False, "That name is taken. Pick another."
        if e.code in (400, 422):
            return False, _error_text(e) or "The site did not accept that name."
        return False, "The site did not accept that name."
    except Exception:
        return False, "Could not reach the site."
    if status != 200:
        return False, "The site did not accept that name."
    if isinstance(data, dict) and isinstance(data.get("worker"), dict):
        data = data["worker"]
    label = data.get("label") if isinstance(data, dict) else None
    if not isinstance(label, str) or not label.strip():
        return False, "The site's answer was not understood."
    tier = data.get("tier")
    save(install, {"label": label.strip()[:80], "tier": tier if isinstance(tier, str) else ""},
         time.time() if now is None else now, enrolled)
    return True, f"Renamed to {label.strip()[:80]}"


def match_second_name(install, base, renamer=None):
    """The second worker on this card carries the same name as the first (owner, 2026-11-25). When the site gave it
    another label (or the first was renamed since), ask the site to use the first one's. -> True when the two now
    match. Best effort: no key, no label, a refusal or no connection all leave it as it was."""
    first = label_of(install)
    if not first or not read_key(install, CONFIG_2):
        return False
    if str(cached(install, ENROLLED_2).get("label") or "") == first:
        return True
    ok, _ = (renamer or rename)(install, base, first, config=CONFIG_2, enrolled=ENROLLED_2)
    return ok
