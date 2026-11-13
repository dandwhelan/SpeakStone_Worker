"""CLI settings (the worker key is NOT here: it lives in the worker's own config, written by `net_worker.py init`)."""
import json
import os
import re
import threading
import time
from pathlib import Path

from .paths import data_dir

DEFAULTS = {
    "idle_minutes": 10,
    "pause_from": "",           # "HH:MM"; with pause_to, the worker never runs in that window
    "pause_to": "",
    "block_fullscreen": True,
    "block_on_battery": True,
    "workers": 1,               # workers on this card: 1, or 2 on a 12 GB+ card with room
    "paused_until": 0,          # epoch seconds: a timed pause from the tray, kept so it survives a restart; 0 = none
    "start_on_open": True,      # opening the app starts making lines at once (the menu is one key away)
    "auto_update": True,        # fetch and install a newer release by itself; off = only say one is available
}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class Settings:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / "cli.json"
        self.data = dict(DEFAULTS)
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                self.data.update({k: d[k] for k in DEFAULTS if k in d})
        except (OSError, ValueError):
            pass

    def reload(self):
        """Read the file again, so a change made in another window (the menu, the tray) is seen by a running process."""
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(d, dict):
            self.data.update({k: d[k] for k in DEFAULTS if k in d})

    def save(self):
        """The background loop, its tray thread and a menu window can all save at once. Each writer gets its own temp
        name (one shared `cli.tmp` let one writer move the other's file away), and on Windows the swap is tried again
        for a moment while another process has the file open to read it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 9:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.05)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()


def parse_hhmm(s):
    m = _HHMM.match((s or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def in_pause_window(now_minutes, pause_from, pause_to):
    """True when `now_minutes` (minutes since midnight) is inside the daily pause window. Handles overnight."""
    a, b = parse_hhmm(pause_from), parse_hhmm(pause_to)
    if a is None or b is None or a == b:
        return False
    return (a <= now_minutes < b) if a < b else (now_minutes >= a or now_minutes < b)
