"""Render README screenshots of SpeakStone Worker from the app's own screen code, with sample data (no real accounts).

Run from SpeakStone_Worker/cli with the package importable:  PYTHONPATH=. python ../docs/make_screenshots.py ../docs/images
"""
import io
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from speakstone_worker import board, wowtext, __version__

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
TITLE = f"SpeakStone Worker {__version__}"


def console(width=104, height=None):
    return Console(record=True, file=io.StringIO(), width=width, height=height, force_terminal=True,
                   color_system="truecolor", highlight=False)     # the app's own consoles print plain text


def save(c, name):
    (out / name).write_text(c.export_svg(title=TITLE), encoding="utf-8")
    print("wrote", out / name)


# 1. the board while it works
now = time.time()
state = {"lines": 1284, "words": 38910, "audio_min": 271, "lines_hour": 212, "words_hour": 4210,
         "since_label": "08:14", "queue": {"queued_lines": 12406, "eta_seconds": 3 * 3600 + 12 * 60},
         "time_left": 3 * 3600 + 12 * 60, "spark": [1, 2, 3, 3, 4, 5, 6, 5, 4, 5, 6, 8],
         "gpu": {"name": "NVIDIA GeForce RTX 3070 Ti", "util_pct": 92, "temp_c": 68, "mem_used_mib": 4608,
                 "mem_total_mib": 8192},
         "making": True, "job": "abcdef123456", "job_lines": 12, "job_pct": 0.68, "state": "idle",
         "events": [(now, "done", "job 2307256a - 30 lines", "140 s"),
                    (now - 180, "paused", "another program is using the graphics card", ""),
                    (now - 170, "resumed", "the graphics card is free again", "carrying on"),
                    (now - 400, "done", "job 9be41c02 - 24 lines", "118 s")]}
c = console(104, 26)
c.print(board.render_board(state))
save(c, "board.svg")

# 2. the menu
c = console(96)
c.print(board.menu_screen(True, None, "Dusk Hollow"))
save(c, "menu.svg")

# 3. what it found in the WoW folders, the add-on updates and the upload log (Settings)
rows = [
    {"folder": r"C:\Program Files (x86)\World of Warcraft", "game": "_retail_", "name": "Retail", "test_realm": False,
     "addons": {"retail": "6.1.3"}, "packs": 16,
     "files": [{"account": "ACCOUNT#1", "flavour": "retail", "entries": 22, "waiting": 0, "error": ""}]},
    {"folder": r"C:\Program Files (x86)\World of Warcraft", "game": "_forever_", "name": "WoW Forever",
     "test_realm": False, "addons": {"forever": "6.1.3"}, "packs": 4,
     "files": [{"account": "ACCOUNT#1", "flavour": "forever", "entries": 3, "waiting": 1, "error": ""}]},
]
c = console(132)
c.print("\n[#e9bb4d]SETTINGS[/]  [dim](part)[/]")
for key, text, val in (("W", "Send my captured WoW text automatically", "on"),
                       ("A", "Keep my SpeakStone add-ons up to date", "on"),
                       ("U", "Update my SpeakStone add-ons now (close WoW first)", None),
                       ("L", "Show what was sent (upload log)", None)):
    c.print(f"  [#e9bb4d][{key}][/] {text}" + (f": [bold]{val}[/]" if val else ""))
import tempfile
from speakstone_worker import addons, cli
fake = Path(tempfile.mkdtemp())                     # sample upload log and add-on state, no real data
wowtext.log_upload(fake, "_forever_ / ACCOUNT#1: sent 2 entries, the site took 1 new lines (1 already known)  "
                         "https://speakstone.beanw.co.uk/s/...")
addons.save_state(fake, {"last_check": time.time(), "games": {
    "_retail_": {"checked": True},
    "_forever_": {"checked": True, "updated": ["SpeakStone_Forever_Main to 6.1.3", "SpeakStone_Forever_Chatter to 6.1.3"]}}})
c.print(cli.wow_survey_view(fake, {"wow_folder": "", "update_addons": True}, surveyor=lambda *a: rows))
save(c, "settings.svg")

# 4. an add-on update run
c = console(110)
for line in ("Looking for SpeakStone add-on updates on GitHub (WoW must be closed for them to be installed)...",
             "add-ons: updating _forever_/SpeakStone_Forever_Main 6.1.2 -> 6.1.3",
             "add-ons: updated _forever_/SpeakStone_Forever_Main to 6.1.3",
             "add-ons: updating _forever_/SpeakStone_Forever_Chatter 6.1.2 -> 6.1.3",
             "add-ons: updated _forever_/SpeakStone_Forever_Chatter to 6.1.3",
             "Add-ons (_retail_): up to date",
             "Add-ons (_forever_): updated SpeakStone_Forever_Main to 6.1.3, SpeakStone_Forever_Chatter to 6.1.3"):
    c.print(Text(line))
save(c, "addon-update.svg")
