"""Everything drawn on screen: banner, menu, live board, setup board. All pure functions returning rich renderables."""
from rich import box
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from . import APP_NAME, TAGLINE, __version__
from .stats import format_duration

GOLD, PARCH, DIM, GREEN, AMBER, BIG, BG = "#e9bb4d", "#d4bb8c", "#7d6b52", "#8fb04a", "#e0962c", "bold #f6e3b0", "#14100c"
SPARK = "▁▂▃▄▅▆▇█"

FONT = {
    "S": ["█████", "█    ", "█████", "    █", "█████"],
    "P": ["█████", "█   █", "█████", "█    ", "█    "],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "K": ["█   █", "█  █ ", "███  ", "█  █ ", "█   █"],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
}


# Why the worker is idle, in the board's words (anything else, or no reason: it simply has no job).
WAIT_TEXT = {
    "yield": "Paused: another program on this PC is using the graphics card. It carries on by itself.",
    "vram": "Waiting: the graphics card's memory is in use by another program.",
    "update_required": "This version is too old for new work. Updating...",
}


def word_rows(word):
    return ["  ".join(FONT[c][r] for c in word) for r in range(5)]


def banner():
    t = Text()
    for word, col in (("SPEAK", GOLD), ("STONE", PARCH)):
        for row in word_rows(word):
            t.append(row.rstrip() + "\n", style=col)
    return t


def menu_screen(service_installed=False, update_version=None, label=""):
    t = Text()
    t.append_text(banner())
    t.append("\n  " + TAGLINE + "\n", style=PARCH)
    t.append(f"  {APP_NAME} {__version__}" + (f" - {label}" if label else "") + "\n\n", style=DIM)
    items = [("1", "Start now", ""), ("2", "Uninstall the service" if service_installed else "Install as a service",
                                      "" if service_installed else "(runs when your PC is idle)"),
             ("3", "Settings", "")]
    if update_version:
        items.append(("U", "Update now", f"(version {update_version} is available)"))
    items.append(("Q", "Quit", ""))
    for k, label, note in items:
        t.append(f"  [{k}] ", style=GOLD)
        t.append(label, style=BIG)
        if note:
            t.append(" " + note, style=DIM)
        t.append("\n")
    t.append("\n  > ", style=GOLD)
    return t


def menu_panel(service_installed=False, update_version=None, label=""):
    """The whole menu as ONE panel, printed once; then a single key is read."""
    return Panel(menu_screen(service_installed, update_version, label), title=f"[{GOLD}]MENU[/]", title_align="left", border_style=GOLD,
                 box=box.SQUARE, style=f"on {BG}", padding=(0, 1))


def bar(frac, width=18, colour=GREEN):
    frac = max(0.0, min(1.0, frac))
    n = round(frac * width)
    t = Text()
    t.append("█" * n, style=colour)
    t.append("░" * (width - n), style=DIM)
    return t


def spark(values):
    top = max(values) if values else 0
    if top <= 0:
        return Text(SPARK[0] * len(values), style=DIM)
    return Text("".join(SPARK[min(7, int(v / top * 7))] if v > 0 else SPARK[0] for v in values), style=GOLD)


def _panel(body, title, **kw):
    return Panel(body, title=f"[{GOLD}]{title}[/]", title_align="left", border_style=GOLD, box=box.SQUARE,
                 style=f"on {BG}", padding=(0, 1), **kw)


def _stat(big, unit, sub, sub_style=PARCH):
    t = Text()
    t.append(big, style=BIG)
    t.append("  " + unit + "\n", style=DIM)
    t.append(sub, style=sub_style)
    return t


def _n(v):
    return f"{int(v):,}"


def render_board(s):
    """`s` is the board state dict built by cli.board_state(). Returns a Layout."""
    since = s.get("since_label") or "this session"
    top = Layout(name="top", size=4)
    top.split_row(
        Layout(_panel(_stat(_n(s["lines"]), "lines", f"▲ {_n(s['lines_hour'])} this hour", GREEN),
                      "LINES TODAY - 2 workers" if s.get("workers", 1) == 2 else "LINES TODAY")),
        Layout(_panel(_stat(_n(s["words"]), "words", f"{_n(s['words_hour'])} / hour"), "WORDS")),
        Layout(_panel(_stat(_n(s["audio_min"]), "min", f"made since {since}"), "AUDIO MINUTES")))

    q = s.get("queue")
    ql = Text()
    ql.append("Waiting   ")
    ql.append(_n(q["queued_lines"]) if q else "-", style=BIG)
    ql.append(" lines\nTime left ")
    ql.append("~ " + format_duration(s.get("time_left")) if s.get("time_left") is not None else "-", style=GOLD)
    ql.append("\nSpeed  ")
    ql.append_text(spark(s.get("spark") or []))
    ql.append("\nwords / hour, last 2 h", style=DIM)

    g = s.get("gpu")
    gt = Text()
    if g:
        gt.append(f"{g.get('name', 'NVIDIA graphics card')}\n", style=PARCH)
        gt.append("Utilisation ")
        gt.append_text(bar(g["util_pct"] / 100))
        gt.append(f" {g['util_pct']}%\n")
        if g.get("util3d_pct") is not None:
            gt.append("3D ")
            gt.append_text(bar(g["util3d_pct"] / 100))
            gt.append(f" {g['util3d_pct']}%\n")
        gt.append("Temp ")
        gt.append_text(bar(g["temp_c"] / 100, colour=AMBER if g["temp_c"] >= 70 else GREEN))
        gt.append(f" {g['temp_c']} C\nVRAM ")
        gt.append_text(bar(g["mem_used_mib"] / max(1, g["mem_total_mib"])))
        gt.append(f" {g['mem_used_mib'] / 1024:.1f}/{g['mem_total_mib'] / 1024:.0f} GB")
    else:
        gt.append("No graphics card reading yet.", style=DIM)
    mid = Layout(name="mid", size=7 if g and g.get("util3d_pct") is not None else 6)
    mid.split_row(Layout(_panel(ql, "QUEUE / TIME LEFT")), Layout(_panel(gt, "GPU HEALTH")))

    now = Text()
    state = s.get("state", "idle")
    if state == "paused":
        now.append("> ", style=GOLD)
        now.append("Paused", style=BIG)
        until = f" until {s['paused_until']}" if s.get("paused_until") else ""
        now.append(f"{until} - the current job was given back. Press [P] to carry on.", style=DIM)
    elif s.get("making") and s.get("job"):
        now.append("> ", style=GOLD)
        now.append(f"Job {s['job'][:8]}", style=BIG)
        if s.get("job_lines", 0) == 0 and (g or {}).get("util_pct", 0) < 10:
            now.append(" - Getting ready: first job loads the voice engine (a minute or two)", style=PARCH)
        else:
            now.append(f" - {s.get('job_lines', 0)} lines ", style=PARCH)
            now.append("[", style=DIM)
            now.append_text(bar(s.get("job_pct", 0.0), 19))
            now.append(f"] {round(s.get('job_pct', 0.0) * 100)}%", style=DIM)
    else:
        now.append("> ", style=GOLD)
        now.append(WAIT_TEXT.get(s.get("idle_reason"), "Waiting for work...") if state != "stopped"
                   else (s.get("stop_note") or "Stopped."), style=DIM)

    ev = Text()
    rows = s.get("events") or []
    if not rows:
        ev.append("Nothing finished yet.", style=DIM)
    for i, (ts, kind, text, note) in enumerate(rows[:5]):
        import time as _t
        ev.append(_t.strftime("%H:%M", _t.localtime(ts)) + " ", style=DIM)
        ev.append(f"{kind:<5} ", style={"done": GREEN, "paused": AMBER, "retry": AMBER, "error": AMBER, "dropped": AMBER}.get(kind, PARCH))
        ev.append(text + " ", style=PARCH)
        if note:
            ev.append(f"({note})", style=DIM)
        if i < min(4, len(rows) - 1):
            ev.append("\n")

    foot = Text()
    if s.get("watching"):
        foot.append(" Worker is running in another window / the service. [S] asks it to stop, [M] menu.", style=AMBER)
        foot.append("\n")
    if s.get("pause_choice"):
        foot.append(" Pause for: ", style=GOLD)
        foot.append("[1] 15 minutes  [2] 1 hour  [3] 2 hours  [4] 4 hours  [5] until I resume  (any other key cancels)", style=PARCH)
        foot.append("\n")
    foot.append(" [P] ", style=GOLD)
    foot.append("resume" if state == "paused" else "pause", style=DIM)
    foot.append("   [S] ", style=GOLD)
    foot.append("stop and exit", style=DIM)
    foot.append("   [M] ", style=GOLD)
    foot.append("menu", style=DIM)
    label = s.get("label")
    foot.append(f"   v{__version__}" + (f" - {label}" if label else ""), style=DIM)
    if s.get("update"):
        foot.append(f" (update {s['update']} available)", style=AMBER)
    if s.get("net"):
        job_mb, today_mb = s["net"]
        foot.append(f"   App downloads: {job_mb:,.1f} MB this job / {today_mb:,.0f} MB today",
                    style=AMBER if job_mb > 200 else DIM)           # one job fetching 200 MB+ means files are re-downloaded

    root = Layout(name="root")
    root.split_column(top, mid, Layout(_panel(now, "NOW MAKING"), name="now", size=3),
                      Layout(_panel(ev, "RECENT EVENTS"), name="events", minimum_size=7),
                      Layout(foot, name="foot", size=3 if (s.get("watching") or s.get("pause_choice")) else 2))
    return root


# the key pressed after [P] on the board -> minutes to pause (0 = until resumed)
PAUSE_KEYS = {"1": 15, "2": 60, "3": 120, "4": 240, "5": 0}
STEP_ICON = {"done": ("✓", GREEN), "run": ("●", GOLD), "todo": (" ", DIM)}


def render_setup(steps, labels, done, current, frac=None, line="", dl=None, error=None):
    """Setup board: steps with ticks, a bar for the current download, the last output line."""
    t = Text()
    for name in steps:
        kind = "done" if name in done else ("run" if name == current else "todo")
        icon, col = STEP_ICON[kind]
        t.append(f" {icon} ", style=col)
        t.append(labels[name] + "\n", style=BIG if kind == "run" else (PARCH if kind == "done" else DIM))
    parts = [t]
    if frac is not None:
        b = Text(" ")
        b.append_text(bar(frac, 40, GOLD))
        b.append(f" {round(frac * 100)}%", style=PARCH)
        if dl:
            b.append("  " + dl, style=DIM)
        parts.append(b)
    if line:
        parts.append(Text(" " + line[:100], style=DIM))
    if error:
        parts.append(Text("\n " + error, style=AMBER))
    parts.append(Text("\n One-off setup, safe to stop and resume. ", style=DIM))
    return Panel(Group(*parts), title=f"[{GOLD}]SPEAK STONE - FIRST-TIME SETUP[/]", title_align="left",
                 border_style=GOLD, box=box.SQUARE, style=f"on {BG}")
