"""Notification-area icon for the background mode (Windows first; Linux where a desktop is present).

Optional by design: when pystray, Pillow or a desktop is missing (headless Linux, a rented GPU, a container) `start`
returns None after one log line and the background mode runs exactly as before. Nothing in here may stop the worker.

`build_menu` is pure: it turns the current state into a list of plain items, so tests check labels, checked boxes and
the actions without pystray. `Tray` only maps those items onto pystray.
"""
import os
import subprocess
import sys
import threading
from collections import namedtuple

from . import APP_NAME

# key, label, enabled, checked (None = not a checkbox), default (the left-click / double-click action)
Item = namedtuple("Item", "key label enabled checked default children", defaults=(None,))
SEPARATOR = Item(None, "-", False, None, False)

TOGGLES = (("block_fullscreen", "Not while a full-screen game is running"),
           ("block_on_battery", "Not while on battery"),
           ("auto_update", "Update automatically"))


# key, label, minutes (0 = until the user resumes)
PAUSE_CHOICES = (("pause_15", "15 minutes", 15), ("pause_60", "1 hour", 60), ("pause_120", "2 hours", 120),
                 ("pause_240", "4 hours", 240), ("pause_0", "Until I resume", 0))


def build_menu(version, status, paused, settings, update_version=None, label=""):
    """-> list of Item. `settings` maps the toggle keys above to True/False."""
    items = [Item("title", f"{APP_NAME} {version}" + (f" - {label}" if label else ""), False, None, False),
             Item("status", status, False, None, False),
             SEPARATOR,
             (Item("resume", "Resume", True, None, False) if paused else
              Item("pause", "Pause", True, None, False, tuple(Item(k, label, True, None, False) for k, label, _m in PAUSE_CHOICES)))]
    items += [Item(k, label, True, bool(settings.get(k)), False) for k, label in TOGGLES]
    items.append(SEPARATOR)
    items.append(Item("board", "Open the board", True, None, True))
    if update_version:
        items.append(Item("update_now", f"Update to {update_version} now", True, None, False))
    items += [Item("check", "Check for updates now", True, None, False),
              Item("logs", "Open the log folder", True, None, False),
              SEPARATOR,
              Item("quit", "Quit", True, None, False)]
    return items


def tooltip(version, status):
    return f"{APP_NAME} {version} - {status}"[:127]       # the Windows tooltip limit


def display_available(platform=None, env=None):
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform == "win32":
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def load_icon(path):
    from PIL import Image
    return Image.open(path).convert("RGBA")


class Tray:
    """pystray wrapper. `get_state()` -> {"status", "paused", "settings", "update_version"}; `actions` maps an item key
    to a no-argument function. An action that raises is swallowed: the tray never takes the worker down."""

    def __init__(self, version, get_state, actions, image, pystray, log=lambda s: None, poll_seconds=2.0):
        self.version, self.get_state, self.actions, self.log = version, get_state, actions, log
        self.pystray, self.poll_seconds = pystray, poll_seconds
        self._stop = threading.Event()
        self._last = None
        self.icon = pystray.Icon("speakstone_worker", image, tooltip(version, "Starting"), pystray.Menu(self._menu_items))

    def _state(self):
        s = self.get_state()
        return s["status"], s["paused"], dict(s["settings"]), s.get("update_version"), s.get("label", "")

    def model(self):
        status, paused, settings, upd, label = self._state()
        return build_menu(self.version, status, paused, settings, upd, label)

    def _run(self, key):
        def call(icon=None, item=None):
            try:
                fn = self.actions.get(key)
                if fn:
                    fn()
                self.refresh()
            except Exception as e:
                self.log(f"tray action {key} failed: {type(e).__name__}: {e}")
        return call

    def _items(self, model):
        mi = self.pystray.MenuItem
        out = []
        for it in model:
            if it.key is None:
                out.append(self.pystray.Menu.SEPARATOR)
                continue
            if it.children:                                  # a submenu (Pause > how long)
                out.append(mi(it.label, self.pystray.Menu(*self._items(it.children)), enabled=it.enabled))
                continue
            out.append(mi(it.label, self._run(it.key), enabled=it.enabled, checked=(None if it.checked is None else
                         (lambda _i, v=it.checked: v)), default=it.default))
        return out

    def _menu_items(self):
        return self._items(self.model())

    def refresh(self):
        try:
            state = self._state()
            if state != self._last:
                self._last = state
                self.icon.title = tooltip(self.version, state[0])
                self.icon.update_menu()
        except Exception as e:
            self.log(f"tray refresh failed: {type(e).__name__}: {e}")

    def _poll(self):
        while not self._stop.wait(self.poll_seconds):
            try:
                self.refresh()
            except Exception:                  # refresh logs its own errors; a failing log call must not end polling
                pass

    def start(self):
        # icon.run in a thread of its own. run_detached() does exactly this on Windows and Xorg, but on Linux's GTK and
        # AppIndicator backends it only prepares the icon and expects the program to run a GLib main loop, which this
        # program never does, so the icon never answered there. pystray's FAQ gives a thread as the way to do it.
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()
        threading.Thread(target=self._poll, daemon=True).start()
        self.refresh()

    def stop(self):
        self._stop.set()
        try:
            self.icon.stop()
        except Exception:
            pass


def start(version, get_state, actions, icon_path, log=lambda s: None, platform=None, env=None, importer=None):
    """-> a started Tray, or None (one log line says why). Never raises."""
    env = os.environ if env is None else env
    try:
        if env.get("SSW_NO_TRAY") == "1":
            log("tray: switched off (SSW_NO_TRAY)")
            return None
        if not display_available(platform, env):
            log("tray: no desktop session, running without the tray icon")
            return None
        pystray = (importer or __import__)("pystray")
        tray = Tray(version, get_state, actions, load_icon(icon_path), pystray, log)
        tray.start()
        return tray
    except Exception as e:
        log(f"tray: not available ({type(e).__name__}: {e}), running without it")
        return None


# ---- the actions that touch the machine (wired in cli.background; each takes its seams as arguments)
def open_board(command, popen=subprocess.Popen, platform=None):
    """Start the normal interactive program in a new console window. `command` = [program, args...]."""
    platform = platform or sys.platform
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if platform == "win32":
        popen(command, creationflags=0x00000010, env=env, close_fds=True)       # CREATE_NEW_CONSOLE
        return True
    import shutil
    for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        found = shutil.which(term)
        if found:
            popen([found, "-e", *command], env=env, start_new_session=True)
            return True
    return False


def open_folder(path, platform=None, startfile=None, popen=subprocess.Popen):
    platform = platform or sys.platform
    path = str(path)
    if platform == "win32":
        (startfile or os.startfile)(path)
    else:
        popen(["xdg-open", path])
