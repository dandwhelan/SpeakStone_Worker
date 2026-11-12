# SpeakStone Worker (command line)

Run: `SpeakStoneWorker.exe` (Windows) or `./run.sh` / `python -m speakstone_worker` (Linux, `pip install rich`).
Menu: [1] Start now, [2] Install as a service (runs when your PC is idle), [3] Settings, [Q] Quit.
First run sets itself up (about 20 GB free, 8 GB graphics card), joins the queue automatically and needs no key.
Everything lives in `%LOCALAPPDATA%\SpeakStoneWorker` (Linux: `~/.local/share/speakstone-worker`).
`SPEAKSTONE_API_BASE` overrides the site URL (testing). Tests: `pytest cli/tests`. Build: `pyinstaller SpeakStoneWorker.spec`.
Unofficial; not affiliated with Blizzard Entertainment.

## Updates, the tray icon and where the program lives
- **Own folder.** "Install as a service" first copies the program into `%LOCALAPPDATA%\SpeakStoneWorker\app` (Linux: `~/.local/share/speakstone-worker/app`) and the log-on task runs that copy, so it does not matter if you started it from a download folder or a network share. If the copy cannot be made, the service is not installed.
- **Version.** The number shows in the menu, on the live board, in the window title and in the tray tooltip. `SpeakStoneWorker.exe --version` prints it.
- **Updates.** The program looks at this project's GitHub releases when it starts and every 6 hours in the background. A release is only installed when it carries a `.sha256` fingerprint file that matches the download and the new file starts and reports the right version. It never swaps while a job is running, and only the copy in its own folder swaps itself; from anywhere else it just says a newer version exists. Menu: `[U] Update now` appears when one is waiting; Settings `[7]` turns "Update automatically" on or off (when off it only tells you). `SSW_NO_UPDATE=1` switches the whole thing off.
- **Tray icon.** While the background service runs, the SpeakStone icon sits in the notification area. Right-click: status, Pause / Resume, the settings toggles, Open the board, Check for updates now, Open the log folder, Quit. Double-click opens the board. Pause hands the current job back and takes no new one until you Resume or the service restarts. Toggles are saved and take effect within a few seconds. No desktop, no `pystray`, or `SSW_NO_TRAY=1`: the service runs exactly as before, without the icon.

When an update is ready and the worker is busy, the program downloads and checks the new file first, then asks the worker to finish the job in hand (it never cuts one short, and gives up for that round if the job is still going after 30 minutes), swaps, and the new copy carries straight on: the service as `--background`, a working board with `--start`, the menu back at the menu. The tray's Pause works the same way: it finishes this job, then stops taking new ones.

Pause (tray menu, or [P] on the board) asks how long: 15 minutes, 1 hour, 2 hours, 4 hours or until you resume. A timed pause from the tray ends by itself and is remembered across a restart or an update; "until I resume" is not. Every time the background mode ends it writes one line saying why to `work\logs\background.log`.

Opening the app on a finished setup starts making lines straight away (the menu is one key away with [M], which stops nothing; `--menu` opens it without starting; Settings [8] turns the auto-start off). After an update the service starts a worker again at once if one was working. The tray says so when the worker was started in the app window.
