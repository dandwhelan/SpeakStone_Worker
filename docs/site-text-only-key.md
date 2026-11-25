# Text-upload-only worker keys (Worker 1.2.26, site PR dandwhelan/speakstone#195)

When "Lend my graphics card" is off, the Worker skips the voice engine and runs a light setup that only gets a key for
sending captured WoW text.

**Enrol** `POST /api/worker/enroll` with the usual body plus `"purpose": "text"`: no VRAM check, `201` with the same
shape as a GPU key (the reply does not echo `purpose`). Until the site change is deployed it answers `422`, and the
Worker asks again without `purpose` (an ordinary key, NVIDIA 8 GB+ only).

**What the Worker does with it**
- Remembers the kind of key in `enrolled.json` (`"purpose": "text" | "gpu"`), from the enrol and from `GET /me`'s
  `worker.purpose`.
- A text key keeps the light setup happy, but does not count as a finished full setup. Turning the lines on
  (Settings [G]) and choosing Start now enrols again without `purpose` and replaces the key: the site cannot upgrade one.
- `net_worker.py` stops (exit 3, key) on `403 {"code": "text_only"}` instead of waiting as if paused.

**Site rules a text key lives under:** harvest and `/me` (GET, rename) only; claim, heartbeat, upload-urls, complete
and fail answer `403 text_only`. Text workers are left off the network page; the per-IP enrol limit is shared, the
network-full cap is separate.
