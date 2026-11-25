# Site change needed: text-upload-only worker keys (Worker 1.2.26)

When a player turns off "Lend my graphics card", the Worker skips the voice engine and runs a light setup that only
gets a worker key, so it can send captured WoW text. On a PC without an 8 GB NVIDIA card, `POST /api/worker/enroll`
currently refuses it (`422 gpu_too_small`).

**What the Worker sends now** (light setup only): the usual enrol body plus `"purpose": "text"`.

```json
{"machine_name": "PC", "gpu": "AMD Radeon RX 7900 XTX", "vram_mib": 0, "os": "windows",
 "worker_version": "1.2.26", "purpose": "text"}
```

**What the site should do**
1. `POST /api/worker/enroll` with `purpose: "text"`: skip the GPU/VRAM check and answer `201` with a key as usual.
   Store the purpose on the worker (for example `kind = 'text'`).
2. `POST /api/worker/harvest` (WoW text upload): accept these keys exactly like ordinary keys.
3. Job claim / lease endpoints: refuse `kind = 'text'` keys (403), so a text key can never take a job.
4. The network page and worker counts: leave text-only workers out, or show them apart.
5. Rate limits for enrolment stay the same.

**Compatibility:** if the site answers `422` to a body with `purpose`, the Worker asks again without it. Until the site
change ships, nothing changes for NVIDIA PCs, and other PCs still get the add-on updates.
