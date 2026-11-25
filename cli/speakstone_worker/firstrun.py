"""The first-start menu: a new player picks which of the three jobs the Worker does before anything is downloaded.

  [1] Keep my SpeakStone add-ons up to date       (Settings [A])
  [2] Send the WoW text I read                    (Settings [W])
  [3] Lend my graphics card when the PC is idle   (Settings [G])
        sub-settings: idle minutes, and two workers on a 16 GB+ card

Shown once, on a PC that has never been set up (settings.first_run_done). A PC set up by an older version is never
shown it (settings.MIGRATIONS). Everything here can be changed later in Settings.

[3] is only offered with an NVIDIA card of 8 GB or more (stats.lines_support): the voice engine runs on CUDA. On any
other PC it is shown locked off, with the reason.
"""
from rich.markup import escape
from rich.text import Text

from . import board, setup, stats

SUGGEST_TWO_MIB = setup.MIN_TOTAL_MIB_FOR_TWO      # offered exactly where Settings [5] allows it (16 GB+)
IDLE_CHOICES = (5, 10, 20, 30, 60)


def gpu_facts(reader=None, adapters=None):
    """-> {"ok", "why", "mib", "name", "suggest_two"} for this PC. Never raises."""
    try:
        reading = (reader or (lambda: stats.gpu_reading(biggest=True)))()
    except Exception:
        reading = None
    names = []
    if not reading:                                  # only asked when it matters: it can take a few seconds on Windows
        try:
            names = (adapters or stats.display_adapters)()
        except Exception:
            names = []
    ok, why = stats.lines_support(reading, names)
    mib = int((reading or {}).get("mem_total_mib") or 0)
    return {"ok": ok, "why": why, "mib": mib, "name": (reading or {}).get("name") or "",
            "suggest_two": ok and mib >= SUGGEST_TWO_MIB}


def initial_choices(st, gpu):
    """What the menu starts with: the current settings, with [3] forced off when this PC cannot make lines."""
    return {"update_addons": bool(st["update_addons"]), "send_wow_text": bool(st["send_wow_text"]),
            "make_lines": bool(st["make_lines"]) and gpu["ok"], "idle_minutes": int(st["idle_minutes"]),
            "two_workers": bool(gpu["suggest_two"] and st["two_workers_wanted"]), "wow_folder": st["wow_folder"]}


def _onoff(v):
    return f"[{board.GREEN}]ON [/]" if v else f"[{board.DIM}]OFF[/]"


def render(c, gpu, note=""):
    """The whole screen as rich markup text. Pure."""
    g = board.GOLD
    out = [f"\n[{g}]WELCOME TO SPEAKSTONE WORKER[/]",
           "Choose what it does on this PC. You can change any of this later in Settings.\n",
           f"  [{g}][1][/] {_onoff(c['update_addons'])}  Keep my SpeakStone add-ons up to date",
           f"         [{board.DIM}]New releases of the add-ons you already have, installed while WoW is closed.[/]",
           f"  [{g}][2][/] {_onoff(c['send_wow_text'])}  Send the WoW text I read that has no voice yet",
           f"         [{board.DIM}]Quest, gossip and book text the add-on captured, so it can be voiced. Every upload is in the log.[/]"]
    if gpu["ok"]:
        out += [f"  [{g}][3][/] {_onoff(c['make_lines'])}  Lend my graphics card when the PC is idle",
                f"         [{board.DIM}]Makes voice lines for everyone from the shared queue. Downloads about 20 GB once.[/]"]
        if c["make_lines"]:
            out.append(f"         [{g}][I][/] Start after [bold]{c['idle_minutes']}[/] idle minutes")
            if gpu["suggest_two"]:
                out.append(f"         [{g}][T][/] {_onoff(c['two_workers'])}  Two workers on this card "
                           f"[{board.GREEN}](suggested: {round(gpu['mib'] / 1024)} GB card, more lines from the same card)[/]")
    else:
        out += [f"  [{board.DIM}][3] OFF  Lend my graphics card when the PC is idle  (not available)[/]",
                f"         [{board.AMBER}]{escape(gpu['why'])}[/]"]
    if c["update_addons"] or c["send_wow_text"]:
        out.append(f"  [{g}][F][/] WoW folder: [bold]{escape(c['wow_folder']) or 'find it by itself'}[/]")
    if gpu["ok"]:
        out.append(f"\n  Graphics card: {escape(gpu['why'])}")
    if not (c["update_addons"] or c["send_wow_text"] or c["make_lines"]):
        out.append(f"\n  [{board.AMBER}]Everything is off: the Worker will do nothing until you turn something on in Settings.[/]")
    if note:
        out.append(f"\n  [{board.AMBER}]{escape(note)}[/]")
    out.append(f"\n  [{g}][Enter][/] Save and continue")
    return "\n".join(out)


def handle_key(c, key, gpu, ask=input, isdir=None):
    """Apply one key press to the choices. -> ("done" | "again", note). Pure apart from `ask` (for [F])."""
    import os
    isdir = isdir or os.path.isdir
    if key in ("\r", "\n", "s"):
        return "done", ""
    if key == "1":
        c["update_addons"] = not c["update_addons"]
    elif key == "2":
        c["send_wow_text"] = not c["send_wow_text"]
    elif key == "3":
        if not gpu["ok"]:
            return "again", gpu["why"]
        c["make_lines"] = not c["make_lines"]
    elif key == "i" and c["make_lines"]:
        i = IDLE_CHOICES.index(c["idle_minutes"]) if c["idle_minutes"] in IDLE_CHOICES else 0
        c["idle_minutes"] = IDLE_CHOICES[(i + 1) % len(IDLE_CHOICES)]
    elif key == "t" and c["make_lines"] and gpu["suggest_two"]:
        c["two_workers"] = not c["two_workers"]
    elif key == "f" and (c["update_addons"] or c["send_wow_text"]):
        v = (ask("WoW folder (for example D:\\Games\\World of Warcraft), blank to find it by itself: ") or "")
        v = v.strip().strip('"')
        if v and not isdir(v):
            return "again", "That folder does not exist."
        c["wow_folder"] = v
    return "again", ""


def save(st, c):
    """Write the choices to the settings, then mark the menu as answered (last, so a crash part-way asks again)."""
    st["update_addons"] = c["update_addons"]
    st["send_wow_text"] = c["send_wow_text"]
    st["make_lines"] = c["make_lines"]
    st["idle_minutes"] = c["idle_minutes"]
    st["wow_folder"] = c["wow_folder"]
    st["two_workers_wanted"] = bool(c["make_lines"] and c["two_workers"])
    st["first_run_done"] = True


def wanted(st, is_set_up):
    """Show the menu? Only on a PC that has never been set up and has not answered it."""
    return not st["first_run_done"] and not is_set_up


def run(console, st, keys_factory, gpu=None, ask=input, clear=None):
    """Show the menu until Enter, then save. `keys_factory()` is a KeyReader-like context manager."""
    gpu = gpu or gpu_facts()
    c = initial_choices(st, gpu)
    note = ""
    while True:
        if clear:
            clear(console)
        console.print(Text.from_markup(render(c, gpu, note)))
        with keys_factory() as keys:
            k = keys.wait()
        if k == "f":
            console.show_cursor(True)
        result, note = handle_key(c, k, gpu, ask)
        if result == "done":
            save(st, c)
            return c


def next_choice(c, service_installed):
    """The main-menu key to go on with after the menu: "1" (set up and start making lines) when lines are on; "2"
    (install the background service, which runs the add-on updates and the text upload) when only those are on;
    None (show the menu) otherwise."""
    if c["make_lines"]:
        return "1"
    if (c["update_addons"] or c["send_wow_text"]) and not service_installed:
        return "2"
    return None
