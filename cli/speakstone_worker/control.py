"""Shared state between the background loop and the tray icon: what the user asked for, and what the loop is doing.
Plain attributes behind a lock; the loop reads the requests, the tray reads the status."""
import threading
import time

WORKING, WAITING_WORK, PAUSED, WAITING_IDLE, UPDATING, STOPPED = (
    "Working", "Waiting for work", "Paused", "Waiting for the PC to be idle", "Updating", "Stopped")
PAUSING, UPDATING_AFTER = "Pausing after this job", "Updating after this job"
MAKING_IN_APP = "Making lines (started in the app window)"


class Control:
    def __init__(self):
        self.lock = threading.Lock()
        self.paused = False
        self.quit = False
        self.check_now = False           # "Check for updates now": look straight away and install even if auto is off
        self.status = WAITING_IDLE
        self.update_version = None       # a newer version that is known but not installed yet
        self.drain_reason = None         # None, "pause" or "update": why the worker was asked to finish its job and leave
        self.drain_since = 0.0
        self.force_start_until = 0.0     # after an update: start a worker at once (even if the PC is not idle) until this time
        self.paused_until = None         # epoch seconds when a timed pause ends; None = until the user resumes

    def set_paused(self, value):
        with self.lock:
            self.paused = bool(value)
            if not self.paused:
                self.paused_until = None

    def pause(self, until=None):
        """Pause, for good (until=None) or until the epoch time `until`."""
        with self.lock:
            self.paused, self.paused_until = True, until

    def pause_expired(self, now):
        return bool(self.paused and self.paused_until and now >= self.paused_until)

    def toggle_pause(self):
        with self.lock:
            self.paused = not self.paused
            return self.paused

    def request_quit(self):
        with self.lock:
            self.quit = True

    def request_check(self):
        with self.lock:
            self.check_now = True

    def take_check(self):
        with self.lock:
            v, self.check_now = self.check_now, False
            return v


def until_label(until):
    return time.strftime("%H:%M", time.localtime(until))


IDLE_STATUS = {"yield": "Paused for another program using the graphics card", "vram": "Waiting for the graphics card",
               "update_required": "Updating to a newer version"}


def status_for(running, making, paused, updating=False, drain=None, until=None, foreign=False, lines=0,
               idle_reason=None):
    """The one-line status the tray shows. Pure. `drain` is control.drain_reason."""
    if updating:
        return UPDATING
    if drain == "update" and running:
        return UPDATING_AFTER
    if paused:
        if running:
            return PAUSING
        return f"{PAUSED} until {until_label(until)}" if until else PAUSED
    if running and making:
        if foreign:                              # a worker this service did not start (the app window's)
            return MAKING_IN_APP + (f" - {lines} lines in this job" if lines else "")
        return WORKING
    if running:                                  # idle: say why when the log does (a foreign worker used to say "Making")
        return IDLE_STATUS.get(idle_reason, WAITING_WORK)
    return WAITING_IDLE
