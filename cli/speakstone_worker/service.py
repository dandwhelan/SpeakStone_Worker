"""'Install as a service (runs when your PC is idle)'.

Windows: a per-user Scheduled Task at log-on that runs this program with `--background`. A real Windows service
would sit in session 0 with no view of keyboard idle time or full-screen games and needs administrator rights, and the
Task Scheduler's own idle trigger cannot see full-screen apps or hand the job back. The background mode does the
watching itself (idle.decide) and stops the worker through its STOP file.
Linux: a systemd --user unit running the same `--background` mode.

The `build_*` functions are pure. `install`/`uninstall` take a `runner` so tests never touch the machine.
"""
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

from . import locking

TASK_NAME = "SpeakStone Worker"
UNIT_NAME = "speakstone-worker.service"
QUIT_FILE = "QUIT_SERVICE"          # written in the data folder to ask the running background program to end
BACKGROUND_PID = "background.pid"   # the running background program's process number (removed when it ends)


def launch_command(frozen=None, executable=None):
    """(program, [args]) that starts the background mode, from the exe or from `python -m speakstone_worker`."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    exe = executable or sys.executable
    return (exe, ["--background"]) if frozen else (exe, ["-m", "speakstone_worker", "--background"])


def windowless(program, args, environ=None):
    """(program, [args]) that runs a console program with no window: Windows' own console host in headless mode. The app
    is a console program, so started straight from the Scheduled Task it opened an empty black window at every log-on
    (the task's Hidden setting only hides the task in the list, not its window)."""
    root = (environ if environ is not None else os.environ).get("SystemRoot") or r"C:\Windows"
    return (root + r"\System32\conhost.exe", ["--headless", str(program), *args])


def windows_user(env=None):
    """The signed-in person as Windows names them (PC\\name), or "" when it cannot be told."""
    env = os.environ if env is None else env
    name = (env.get("USERNAME") or "").strip()
    domain = (env.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{name}" if name and domain else name


def build_task_xml(program, args, user=None):
    # The start-up trigger names the person. Without a name it means "when ANYONE signs in", and Windows lets only an
    # administrator create that: from a normal window the install was refused with "Access is denied" on a PC with
    # no older entry at all (owner's PC, 2026-11-14; proven there with a throw-away entry, with and without the name).
    user = windows_user() if user is None else user
    who = f"<UserId>{escape(user)}</UserId>" if user else ""
    arguments = " ".join(f'"{a}"' if " " in a else a for a in args)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Makes voice lines for the SpeakStone queue when your PC is idle.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled>{who}</LogonTrigger></Triggers>
  <Principals><Principal id="Author">{who}<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Hidden>true</Hidden>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(str(program))}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
</Task>
"""


def build_unit(program, args, workdir=None):
    exec_start = " ".join(shlex.quote(str(x)) for x in [program, *args])
    wd = f"WorkingDirectory={workdir}\n" if workdir else ""
    return f"""[Unit]
Description=SpeakStone Worker (makes voice lines when this PC is idle)

[Service]
Type=simple
ExecStart={exec_start}
{wd}Nice=10
Restart=on-failure
RestartSec=30
KillSignal=SIGINT
TimeoutStopSec=150

[Install]
WantedBy=default.target
"""


def schtasks_create(xml_path):
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]


def schtasks_delete():
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def schtasks_query():
    return ["schtasks", "/Query", "/TN", TASK_NAME]


def systemctl(*a):
    return ["systemctl", "--user", *a]


def _ok(runner, cmd):
    try:
        return runner(cmd, capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def _run_text(runner, cmd):
    """-> (worked, what the command printed). Never raises."""
    try:
        r = runner(cmd, capture_output=True, text=True)
    except OSError as e:
        return False, str(e)
    return r.returncode == 0, f"{getattr(r, 'stdout', '') or ''}\n{getattr(r, 'stderr', '') or ''}"


def access_denied(text):
    """Did Windows refuse for lack of rights? The task then belongs to an administrator: it was created from an
    administrator window, and an ordinary window may read and run it but not replace or delete it."""
    t = (text or "").lower()
    return "access is denied" in t or "zugriff verweigert" in t or "accès refusé" in t or "acceso denegado" in t


ADMIN_REMOVE = 'schtasks /Delete /TN "SpeakStone Worker" /F'


class Installed(int):
    """Truthy when the service was installed. `.denied`: Windows refused because an older service entry belongs to an
    administrator (see access_denied)."""
    denied = False

    def __new__(cls, ok, denied=False):
        self = super().__new__(cls, 1 if ok else 0)
        self.denied = bool(denied)
        return self

    def sentence(self, idle_minutes):
        if self:
            return f"Installed. It starts when your PC has been idle for {idle_minutes} minutes."
        if self.denied:
            return ("Windows would not let this program install the service. If an older copy of it was installed "
                    "from an administrator window, open a terminal as administrator, run\n    " + ADMIN_REMOVE +
                    "\nand then choose this again from a normal window. (If that command says it cannot find the "
                    "file, there is no older copy: please report this.)")
        return ("Could not install the service (the program could not be copied into its own folder, "
                "or the task was refused).")


def own_copy(frozen=None, executable=None, target=None, copier=None):
    """The program the service should run. A frozen program first copies itself into the app's own folder on the local
    disk and the service points at that copy: started from a network share or a download folder, the log-on task
    would otherwise depend on that place being there. From source nothing is copied (None). Raises OSError."""
    from . import paths, update
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return None
    return update.install_copy(executable or sys.executable, target or paths.app_exe(), **({"copy": copier} if copier else {}))


def install(data_dir, runner=subprocess.run, platform=None, unit_dir=None, app_target=None, copier=None, **launch_kw):
    platform = platform or sys.platform
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        copy = own_copy(launch_kw.get("frozen"), launch_kw.get("executable"), app_target, copier)
    except OSError:
        return False                                  # no copy, no service: never point the task at a share
    if copy is not None:
        launch_kw = dict(launch_kw, frozen=True, executable=str(copy))
    program, args = launch_command(**launch_kw)
    if platform == "win32":
        xml = data_dir / "task.xml"
        xml.write_text(build_task_xml(*windowless(program, args)), encoding="utf-16")
        ok, said = _run_text(runner, schtasks_create(xml))
        if ok:                                       # also start it for this session so no log-off is needed
            _ok(runner, ["schtasks", "/Run", "/TN", TASK_NAME])
        return Installed(ok, denied=not ok and access_denied(said))
    unit_dir = Path(unit_dir) if unit_dir else Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / UNIT_NAME).write_text(build_unit(program, args, workdir=data_dir), encoding="utf-8")
    return (_ok(runner, systemctl("daemon-reload")) and _ok(runner, systemctl("enable", "--now", UNIT_NAME)))


class Removal:
    """What uninstall did. Truthy when the service is removed AND the background program has gone (or was not running);
    `removed` is the start-up entry, `closing` is a background program that was asked to end and has not gone yet."""

    def __init__(self, removed, closing=False, denied=False):
        self.removed, self.closing, self.denied = bool(removed), bool(closing), bool(denied)

    def __bool__(self):
        return self.removed and not self.closing

    def sentence(self, missing="No service was installed."):
        if self.removed and not self.closing:
            return "Service removed."
        if self.removed:
            return ("Service removed from start-up, but the background program is still closing (it hands its job "
                    "back first). It will be gone in a minute or two.")
        if self.denied:
            # seen on the owner's PC 2026-11-14: the entry had been created from an administrator window
            return ("Windows would not let this program remove the service: it was installed from an administrator "
                    "window. The background program has " + ("been asked to stop" if self.closing else "stopped") +
                    " for now, but it will start again when you next sign in. To remove it for good, open a terminal "
                    "as administrator and run\n    " + ADMIN_REMOVE)
        return missing + (" The background program is still closing." if self.closing else "")


def background_pid(data_dir):
    try:
        return int((Path(data_dir) / BACKGROUND_PID).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def background_running(data_dir, alive=locking.pid_alive):
    """Is the background program (the service, with its tray icon) running right now?"""
    pid = background_pid(data_dir)
    return bool(pid) and bool(alive(pid))


def start(runner=subprocess.run, platform=None):
    """Start the installed service now (it normally starts when the person signs in). -> True when the request was
    accepted. The Windows task ignores a second start while it is already running, so asking twice is harmless."""
    platform = platform or sys.platform
    if platform == "win32":
        return _ok(runner, ["schtasks", "/Run", "/TN", TASK_NAME])
    return _ok(runner, systemctl("start", UNIT_NAME))


def stop_background(data_dir, alive=locking.pid_alive, sleep=time.sleep, clock=time.monotonic, wait=20.0):
    """Windows: ending the Scheduled Task does not end the background program it started (conhost --headless keeps it,
    with its worker and its tray icon), so the person was told "removed" while it carried on. Ask it to quit through
    the QUIT_SERVICE file (it checks every few seconds, hands the job back and leaves) and wait up to `wait` seconds.
    -> True when it has gone or was not running."""
    pid = background_pid(data_dir)
    if not pid or not alive(pid):
        return True
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        (Path(data_dir) / QUIT_FILE).write_text("quit", encoding="utf-8")
    except OSError:
        return False
    end = clock() + wait
    while clock() < end:
        if not alive(pid):
            return True
        sleep(0.5)
    return not alive(pid)


def uninstall(runner=subprocess.run, platform=None, unit_dir=None, data_dir=None, alive=locking.pid_alive,
              sleep=time.sleep, clock=time.monotonic, wait=20.0):
    platform = platform or sys.platform
    if platform == "win32":
        _ok(runner, ["schtasks", "/End", "/TN", TASK_NAME])
        removed, said = _run_text(runner, schtasks_delete())
        gone = stop_background(data_dir, alive, sleep, clock, wait) if data_dir else True
        return Removal(removed, closing=not gone, denied=not removed and access_denied(said))
    _ok(runner, systemctl("disable", "--now", UNIT_NAME))
    unit_dir = Path(unit_dir) if unit_dir else Path.home() / ".config" / "systemd" / "user"
    try:
        (unit_dir / UNIT_NAME).unlink()
    except OSError:
        pass
    return Removal(_ok(runner, systemctl("daemon-reload")))


def is_installed(runner=subprocess.run, platform=None, unit_dir=None):
    platform = platform or sys.platform
    if platform == "win32":
        return _ok(runner, schtasks_query())
    unit_dir = Path(unit_dir) if unit_dir else Path.home() / ".config" / "systemd" / "user"
    return (unit_dir / UNIT_NAME).exists()
