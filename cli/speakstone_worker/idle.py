"""When to run in the background. `decide` is pure; the readers never raise.

idle_seconds() returns None where the OS gives no idle time (Linux without xprintidle, e.g. a headless box): the
background loop then treats the PC as "always on" and runs the worker at low priority (nice).
"""
import ctypes
import glob
import subprocess
import sys

LEAVE_SECONDS = 15      # input younger than this counts as "the user is back"


def decide(mode, running, idle_seconds, idle_minutes, fullscreen, on_battery, paused,
           block_fullscreen=True, block_on_battery=True):
    """Return 'start', 'stop' or 'keep'. mode: 'off' | 'now' | 'idle'."""
    if mode == "off" or paused:
        return "stop" if running else "keep"
    blocked = (block_fullscreen and fullscreen) or (block_on_battery and on_battery)
    if mode == "now":
        if blocked:
            return "stop" if running else "keep"
        return "keep" if running else "start"
    if running:
        return "stop" if (blocked or idle_seconds < LEAVE_SECONDS) else "keep"
    if not blocked and idle_seconds >= idle_minutes * 60:
        return "start"
    return "keep"


def idle_seconds():
    if sys.platform == "win32":
        try:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
            ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
            return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0
        except Exception:
            return 0.0
    try:
        r = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=3)
        return int(r.stdout.strip()) / 1000.0 if r.returncode == 0 else None
    except Exception:
        return None


def fullscreen_busy():
    """Full-screen game or presentation (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)."""
    if sys.platform != "win32":
        return False
    try:
        state = ctypes.c_int(0)
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        return state.value in (2, 3, 4)
    except Exception:
        return False


def on_battery():
    if sys.platform == "win32":
        try:
            class SPS(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
            s = SPS()
            ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
            return s.ACLineStatus == 0
        except Exception:
            return False
    try:
        for p in glob.glob("/sys/class/power_supply/BAT*/status"):
            if open(p).read().strip() == "Discharging":
                return True
    except Exception:
        pass
    return False
