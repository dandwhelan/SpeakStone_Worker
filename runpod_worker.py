#!/usr/bin/env python3
"""
runpod_worker.py - shared helpers for indextts2_worker.py: level + encode (ffmpeg), the quality gate, the per-clip
seed, the output validity check and the GPU-fault test.

Until 2026-11-12 this file also held a Qwen3-TTS batch worker (HardenedRunpodWorker). Nothing in this repository ran
it after the move to IndexTTS-2, so it was removed. The file keeps its name because the app installs and bundles it.
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


# Enforce persistent cache directories if running in RunPod environment (/workspace exists)
WORKSPACE_PATH = Path("/workspace")
if WORKSPACE_PATH.exists():
    os.environ.setdefault("HF_HOME", "/workspace/hf_cache")
    os.environ.setdefault("TORCH_HOME", "/workspace/torch_cache")
    Path("/workspace/hf_cache").mkdir(parents=True, exist_ok=True)
    Path("/workspace/torch_cache").mkdir(parents=True, exist_ok=True)

import soundfile as sf
import torch

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REFS = SCRIPT_DIR / "references"
OUTPUT_SAMPLE_RATE = 24000
OUTPUT_CHANNELS = 1


QC_HELD_DIR = "_held_qc"
QC_LOG = "_qc_held.jsonl"
QC_TRIES = 3


def qc_clip(duration, text):
    """Why a made take fails the quality gate, or "": under 0.3 s, or longer than 0.75 s per word + 6 s (a loop or a
    runaway). The rule previously applied by hand."""
    words = len(re.sub(r"<[^>]*>", " ", text or "").split())
    if duration < 0.3:
        return f"too short: {duration:.2f} s"
    if words and duration > words * 0.75 + 6:
        return f"too long for {words} words: {duration:.1f} s (limit {words * 0.75 + 6:.1f} s)"
    return ""


def is_gpu_fatal(error):
    """Return True when retrying later rows in this process is unsafe."""
    if isinstance(error, torch.OutOfMemoryError):
        return True
    message = str(error).lower()
    fatal_markers = (
        "cuda out of memory",
        "device-side assert",
        "illegal memory access",
        "cublas_status",
        "cudnn_status",
        "cuda error",
    )
    return any(marker in message for marker in fatal_markers)


def get_audio_info(filepath):
    """Verify audio file readability and extract duration using libsndfile / soundfile."""
    try:
        info = sf.info(str(filepath))
        return info.duration, info.samplerate, info.channels
    except Exception:
        return 0.0, 0, 0

def is_valid_output(filepath):
    """Require a readable, non-empty 24 kHz mono output file."""
    duration, sample_rate, channels = get_audio_info(filepath)
    return (
        duration > 0.05
        and sample_rate == OUTPUT_SAMPLE_RATE
        and channels == OUTPUT_CHANNELS
    )

def compute_generation_seed(row):
    """Return the stable per-clip seed used for stochastic model sampling."""
    if "generation_seed" in row:
        seed = int(row["generation_seed"])
        if not 0 <= seed <= 0x7FFFFFFF:
            raise ValueError(f"generation_seed must be between 0 and 2147483647, got {seed}")
        return seed

    seed_material = "|".join([
        str(row["sound_file"]),
        str(row["text_hash"]),
        str(row["profile_key"]),
        str(row["model_engine"]),
    ])
    return int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF

LEAD_KEEP_S = 0.15
QUIET_FLOOR_DB = -26.0
TARGET_RMS_DB = -18.0
# Loudness (2026-10-10: narration "louder or quieter ... not consistent"). The in-game voice-over sits at about
# -16.0 LUFS median. Until then this encoder only lifted clips under
# -26 dB RMS. Now every clip is measured (EBU R128 integrated loudness, after trim and post_af) and:
#   under TARGET - WINDOW            -> levelled to TARGET (two-pass loudnorm, linear gain, limiter);
#   over TARGET + WINDOW             -> levelled down to TARGET, "!" lines included;
#   within the window                -> gain untouched (so a shouted line may sit up to -14), peaks limited.
# Until 2026-11-12 (recipe v2) a clip inside the window skipped the limiter along with the gain. Measured that day on
# 60 such clips: median true peak +0.9 dBTP, 50 of 60 above 0 dBTP (they can clip), against -2.4 dBTP for levelled
# clips. Recipe v3 runs the same limiter on every clip.
# Until 2026-11-11 a "!" line over the window was left loud. Lines kept that way came out far too hot (around -8 LUFS,
# true peaks above 0 dBTP, so they clipped) and sounded distorted; on a listening test -14 to -16 was preferred.
TARGET_LUFS = -16.0
LOUDNESS_WINDOW_LU = 2.0
PEAK_CEILING_DB = -3.0  # loudnorm true-peak target; Vorbis at 32 kbps overshoots it by 1-2 dB
LIMIT_LINEAR = 0.7      # alimiter ceiling (-3.1 dBFS) after loudnorm
LEVEL_RECIPE = "lufs-16-w2-v3"


def loudness_plan(measured_i, shout):
    """'up', 'down' or None (leave) for a clip at measured_i LUFS. `shout` is kept for callers; it no longer exempts."""
    if measured_i is None or measured_i < -70:
        return None
    if measured_i < TARGET_LUFS - LOUDNESS_WINDOW_LU:
        return "up"
    if measured_i > TARGET_LUFS + LOUDNESS_WINDOW_LU:
        return "down"
    return None


def _loudnorm_measure(in_path, pre_filters):
    """First loudnorm pass over the processed audio: the measured_* values loudnorm wants back, or None."""
    chain = ",".join(list(pre_filters) + [f"loudnorm=I={TARGET_LUFS}:TP={PEAK_CEILING_DB}:LRA=11:print_format=json"])
    r = _run_with_retry(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(in_path), "-af", chain, "-f", "null", "-"],
                        timeout=90)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr or "", re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
        float(j["input_i"])
        return j
    except (ValueError, KeyError):
        return None



RETRY_TIMEOUT = 300


def _run_with_retry(cmd, timeout=90):
    """subprocess.run with no stdin and a timeout; on a timeout it runs ONCE more with RETRY_TIMEOUT before giving up.
    2026-11-07: a 90 s ffmpeg timeout fired on one clip of a 132-clip batch only
    while the GPU and CPU were shared with other jobs; the clip failed, the batch exited 1 and nothing installed. The
    retry is a plain re-run of the same command (subprocess.run has already killed the first ffmpeg)."""
    kw = dict(stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return subprocess.run(cmd, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        print(f"[ffmpeg] timed out after {timeout}s, retrying once with {RETRY_TIMEOUT}s: {Path(str(cmd[-1])).name}",
              flush=True)
        return subprocess.run(cmd, timeout=max(RETRY_TIMEOUT, timeout), **kw)


def _ffmpeg(args, timeout=90):
    """ffmpeg with no stdin (a backgrounded ffmpeg that inherits a terminal stdin stops on its first read) and a
    timeout, so one bad clip can never freeze a worker again. A timeout is retried once with a longer one
    (_run_with_retry, 2026-11-07)."""
    return _run_with_retry(["ffmpeg", "-nostdin", "-hide_banner", "-y"] + args, timeout=timeout)


def apply_post_fx_and_encode(in_wav, out_ogg, post_af=None, bitrate="32k", shout=False):
    """
    Trim leading silence, apply the profile's filters (pitch, formant, bass), level the loudness (see TARGET_LUFS
    above) and encode to fixed 32 kbps 24 kHz mono Vorbis OGG. Returns {"measured_lufs", "level"} for provenance.

    2026-10-10: silenceremove and a measured loudnorm in ONE filter chain hung ffmpeg on some clips (all workers on
    two pods froze ~0.2 s into the output). Now three steps: trim + post_af into a temp WAV; measure that WAV; encode
    it with a plain gain to the target plus a limiter (what loudnorm's linear mode does anyway).
    """
    # Owner 2026-10-04 (3.0.0 listening sample): "long gap at start" (Constable Zo'ardaz 90127_progress, 2.86 s;
    # worst found 6.45 s) and "quiet clip compared to the rest" (Zapetta, -30 dB vs library median -17.8 dB).
    pre = [f"silenceremove=start_periods=1:start_threshold=-45dB:start_silence={LEAD_KEEP_S}"]
    if post_af:
        pre.append(post_af)
    mid = str(out_ogg) + ".pre.wav"
    info = {"measured_lufs": None, "level": None, "level_recipe": LEVEL_RECIPE}
    try:
        r = _ffmpeg(["-i", str(in_wav), "-af", ",".join(pre), "-c:a", "pcm_s16le", mid])
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg pre-filter failed: {r.stderr[-500:]}")
        j = _loudnorm_measure(mid, [])
        filters = []
        if j is not None:
            mi = float(j["input_i"])
            info["measured_lufs"] = round(mi, 2)
            plan = loudness_plan(mi, shout)
            if plan:
                filters.append(f"volume={TARGET_LUFS - mi:.2f}dB,alimiter=limit={LIMIT_LINEAR}:level=false")
                info["level"] = plan
        if not filters:                     # inside the window, or not measurable: no gain, but the same peak limit
            filters.append(f"alimiter=limit={LIMIT_LINEAR}:level=false")
        args = ["-i", mid] + (["-af", ",".join(filters)] if filters else [])
        r = _ffmpeg(args + ["-c:a", "libvorbis", "-b:a", str(bitrate), "-ar", str(OUTPUT_SAMPLE_RATE),
                            "-ac", str(OUTPUT_CHANNELS), str(out_ogg)])
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg encoding failed: {r.stderr[-500:]}")
    finally:
        try:
            os.remove(mid)
        except OSError:
            pass
    return info
