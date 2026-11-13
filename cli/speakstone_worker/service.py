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
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "SpeakStone Worker"
UNIT_NAME = "speakstone-worker.service"


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


def build_task_xml(program, args):
    arguments = " ".join(f'"{a}"' if " " in a else a for a in args)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Makes voice lines for the SpeakStone queue when your PC is idle.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
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
        ok = _ok(runner, schtasks_create(xml))
        if ok:                                       # also start it for this session so no log-off is needed
            _ok(runner, ["schtasks", "/Run", "/TN", TASK_NAME])
        return ok
    unit_dir = Path(unit_dir) if unit_dir else Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / UNIT_NAME).write_text(build_unit(program, args, workdir=data_dir), encoding="utf-8")
    return (_ok(runner, systemctl("daemon-reload")) and _ok(runner, systemctl("enable", "--now", UNIT_NAME)))


def uninstall(runner=subprocess.run, platform=None, unit_dir=None):
    platform = platform or sys.platform
    if platform == "win32":
        _ok(runner, ["schtasks", "/End", "/TN", TASK_NAME])
        return _ok(runner, schtasks_delete())
    _ok(runner, systemctl("disable", "--now", UNIT_NAME))
    unit_dir = Path(unit_dir) if unit_dir else Path.home() / ".config" / "systemd" / "user"
    try:
        (unit_dir / UNIT_NAME).unlink()
    except OSError:
        pass
    return _ok(runner, systemctl("daemon-reload"))


def is_installed(runner=subprocess.run, platform=None, unit_dir=None):
    platform = platform or sys.platform
    if platform == "win32":
        return _ok(runner, schtasks_query())
    unit_dir = Path(unit_dir) if unit_dir else Path.home() / ".config" / "systemd" / "user"
    return (unit_dir / UNIT_NAME).exists()
