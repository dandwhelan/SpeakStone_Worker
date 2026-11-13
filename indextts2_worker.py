#!/usr/bin/env python3
"""
indextts2_worker.py - IndexTTS-2 voice-line worker, run as a subprocess by net_worker.py.

Input   rows (a JSON batch file) whose model_engine is "indextts2".
Output  <sound_file>.ogg, 24 kHz mono Vorbis @ 32 kbps, levelled and encoded by runpod_worker.apply_post_fx_and_encode
        (imported: one level recipe for every clip), and the quality gate runpod_worker.qc_clip.
Resume  generation_provenance.jsonl with a recipe fingerprint; a clip with a valid file and a matching record is skipped.
Seed    the row's generation_seed, so a remake of unchanged text reproduces the same take (seed_salt changes it).
Modes   one batch per process (--batch-file ...), or --serve: load the model once and make one batch per request read
        from stdin (see serve()), so a run of jobs does not reload several GB of weights each time.

Run it in the IndexTTS-2 Python environment; it needs the weights in INDEXTTS2_DIR (a plain folder, not the
Hugging Face cache: that cache wants Windows symlinks, which an ordinary account may not create).
"""
import argparse
import hashlib
import json
import os
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")   # less fragmentation: matters on an 8 GB card
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import numpy as np
import soundfile as sf
import torch

import runpod_worker as rw   # shared helpers: level + encode, quality gate, seeds, validity check


def hf_cache_dir(env):
    """The folder the extra model files are kept in. An explicit HF_HUB_CACHE wins, then <HF_HOME>/hub, then the
    library's own default (None)."""
    if env.get("HF_HUB_CACHE"):
        return env["HF_HUB_CACHE"]
    if env.get("HF_HOME"):
        return str(Path(env["HF_HOME"]) / "hub")
    return None


def pin_hf_cache(env=None):
    """The engine sets HF_HUB_CACHE to './checkpoints/hf_cache' when it is imported: a path relative to the current
    folder. The worker runs each job in that job's own folder, so every job looked in an empty folder, fetched several
    GB again and deleted them with the job. huggingface_hub reads the setting once, when it is first imported, so
    importing it here, with an absolute path, makes the engine's later change harmless."""
    env = os.environ if env is None else env
    cache = hf_cache_dir(env)
    if cache:
        cache = str(Path(cache).resolve())
        env["HF_HUB_CACHE"] = cache
    if env is os.environ:
        try:
            # the package loads its parts lazily: name the constants module, or the setting is still unread
            import huggingface_hub.constants  # noqa: F401  (must happen before indextts.infer_v2 is imported)
        except ImportError:
            pass
    return cache


HF_CACHE = pin_hf_cache()

ENGINE = "indextts2"
MODEL_REPO = "IndexTeam/IndexTTS-2"
MODEL_DIR = Path(os.environ.get("INDEXTTS2_DIR", str(Path(__file__).resolve().parent / "models" / "IndexTTS-2")))
# IndexTTS-2's accel engine uses flash-attn and samples with temperature only. It ignores num_beams, top_k, top_p and
# repetition_penalty (gpt/model_v2.py inference_speech at commit 830f6f8), so its takes do not follow the recipe.
USE_ACCEL = os.environ.get("IDX2_USE_ACCEL") == "1"
EMO_ALPHA = 1.0          # the table below is the effective strength: the vector goes in as written (total capped at 0.8)

# The row's line_emotion -> IndexTTS-2's emotion vector
# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]. Tuned by ear (2026-11-02): one clip, all eight emotions at 0.8 were usable, at 0.4 none
# were ("mild" was too close to no emotion); all four mixes were usable (urgent = angry .3 + afraid .3 + surprised .2,
# excited = happy .5 + surprised .3, grim, warm); the sentence mode worked for 2 of 4 (not used here). So single strong
# emotions sit at 0.7-0.8, soft ones lower, and anger stays at 0.7: no shouted or strained lines.
# Surprised is toned down ("too manic" at 0.7): 0.4 alone, and its share in the mixes
# scaled by 0.6875, the factor the model authors' own web UI applies to it. An emotion not listed (neutral, questioning, curious, ...) gets calm 0.5 only (NEUTRAL_CALM, see
# emotion_vector): with no vector the model copies the source clip's own energy, which sounded like exclaiming.
_H, _A, _S, _F, _D, _M, _U, _C = range(8)


def _vec(**kw):
    v = [0.0] * 8
    for k, x in kw.items():
        v[{"happy": _H, "angry": _A, "sad": _S, "afraid": _F, "disgusted": _D, "melancholic": _M,
           "surprised": _U, "calm": _C}[k]] = x
    return v


EMOTION_VECTORS = {
    # happy family
    "happy": _vec(happy=0.8), "joy": _vec(happy=0.8), "warm": _vec(happy=0.4, calm=0.3), "proud": _vec(happy=0.6),
    "amusement": _vec(happy=0.7), "excitement": _vec(happy=0.5, surprised=0.2), "optimism": _vec(happy=0.5),
    "admiration": _vec(happy=0.5), "gratitude": _vec(happy=0.4, calm=0.3), "love": _vec(happy=0.5, calm=0.3),
    "caring": _vec(happy=0.3, calm=0.4), "relief": _vec(happy=0.3, calm=0.4), "approval": _vec(happy=0.3),
    # angry family (firm, not screamed)
    "angry": _vec(angry=0.7), "threat": _vec(angry=0.5, calm=0.2), "forceful": _vec(angry=0.5),
    "annoyance": _vec(angry=0.4), "spiteful": _vec(angry=0.4, disgusted=0.2), "disapproval": _vec(angry=0.3),
    "sarcasm": _vec(disgusted=0.2, happy=0.2),
    # sad family
    "sad": _vec(sad=0.8), "grief": _vec(sad=0.8), "dismay": _vec(sad=0.5, afraid=0.2), "weary": _vec(melancholic=0.6),
    "disappointment": _vec(sad=0.6), "remorse": _vec(sad=0.6), "solemn": _vec(melancholic=0.5, calm=0.3),
    "ominous": _vec(melancholic=0.4, angry=0.2, calm=0.2),   # the "grim" mix
    # afraid / urgent
    "terrified": _vec(afraid=0.8), "distress": _vec(afraid=0.5, sad=0.3), "nervous": _vec(afraid=0.5),
    "embarrassment": _vec(afraid=0.3),
    "urgent": _vec(angry=0.3, afraid=0.3, surprised=0.14),   # the "urgent" mix
    # the rest
    "surprise": _vec(surprised=0.4), "disgust": _vec(disgusted=0.8), "confusion": _vec(surprised=0.27, afraid=0.2),
}


NEUTRAL_CALM = 0.5   # a plain line gets this much "calm" (calm test 2026-11-02: base was "exclaiming too much")
MAX_SPLIT_WORDS = 45 # split long passages into <=45-word sentence/clause pieces with 0.0s gap


def split_long_text(text, max_words=MAX_SPLIT_WORDS):
    """Split passages over max_words at sentence boundaries, then clause boundaries, then words."""
    sents = re.split(r'(?<=[.!?])\s+', (text or '').strip())
    out, cur = [], ''
    for s in sents:
        subs = [s] if len(s.split()) <= max_words else re.split(r'(?<=[,;:])\s+', s)
        for sub in subs:
            while len(sub.split()) > max_words:
                w = sub.split()
                out.append(' '.join(w[:max_words]))
                sub = ' '.join(w[max_words:])
            if cur and len(cur.split()) + len(sub.split()) > max_words:
                out.append(cur)
                cur = sub
            else:
                cur = (cur + ' ' + sub).strip()
    if cur:
        out.append(cur)
    return out or [text]


def emotion_vector(row):
    """The emotion vector for a row. A row can carry its own "emo_vector" (a list of 8). A line with no mapped emotion
    (neutral, questioning, curious, desire, realization, or untagged) gets calm only: with no vector the model copies the
    source clip's own energy, which made every base take sound like it was exclaiming (in the calm test no base take
    was accepted and calm 0.3 to 0.7 all were; 0.5 was the middle)."""
    v = row.get("emo_vector")
    if not (isinstance(v, (list, tuple)) and len(v) == 8):
        v = EMOTION_VECTORS.get((row.get("line_emotion") or "").strip().lower())
    if not v:
        v = _vec(calm=NEUTRAL_CALM)
    v = [float(x) for x in v]
    # IndexTTS-2's own rule (infer_v2.normalize_emo_vec): the emotions' total, after the alpha scale, stays at 0.8 or less;
    # beyond that the speaker's own emotion weight (1 - sum) would go negative and the voice distorts.
    total = sum(v) * EMO_ALPHA
    if total > 0.8:
        v = [x * 0.8 / total for x in v]
    return v


STRONG_EMOTION_AT = 0.5   # an emotion value (first seven slots, not calm) at or above this is a "strong" line
ENGINE_DEFAULT_BEAMS = 3  # infer_v2's own default


def beams_for(vec, environ=None):
    """num_beams for one line (blind A/B of 131 lines, 2026-11-07): 1 beam when the
    emotion vector carries a strong non-calm emotion (any of the first seven values >= 0.5), else 3 (neutral / calm-only /
    no vector). Env IDX2_NUM_BEAMS overrides everything (A/B runs); IDX2_BEAMS_BY_EMOTION=0 turns the rule off (engine
    default 3). Returns an int."""
    env = os.environ if environ is None else environ
    forced = (env.get("IDX2_NUM_BEAMS") or "").strip()
    if forced:
        return int(forced)
    if (env.get("IDX2_BEAMS_BY_EMOTION") or "1").strip() == "0":
        return ENGINE_DEFAULT_BEAMS
    try:
        strong = bool(vec) and any(float(x) >= STRONG_EMOTION_AT for x in list(vec)[:7])
    except (TypeError, ValueError):
        strong = False
    return 1 if strong else ENGINE_DEFAULT_BEAMS


def model_revision():
    """The weights are a plain folder, so there is no commit hash: fingerprint config.yaml (it names every file)."""
    cfg = MODEL_DIR / "config.yaml"
    h = hashlib.sha256(cfg.read_bytes()).hexdigest()[:12] if cfg.exists() else "missing"
    return f"{MODEL_REPO}@cfg-{h}"


def recipe_fingerprint(row, revision, bitrate):
    vec = emotion_vector(row)
    payload = "|".join(str(x) for x in (
        row["text_hash"], row["profile_key"], row["canonical_reference_clip"], ENGINE, revision,
        json.dumps(vec) if vec else "", EMO_ALPHA if vec else "", rw.compute_generation_seed(row),
        row.get("post_af", "") or "", str(bitrate).lower(), rw.OUTPUT_SAMPLE_RATE, rw.OUTPUT_CHANNELS))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_model(device):
    if not (MODEL_DIR / "config.yaml").exists():
        print(f"[FATAL] IndexTTS-2 weights not found in {MODEL_DIR} (set INDEXTTS2_DIR, or download "
              f"{MODEL_REPO} there with snapshot_download(local_dir=...)).", file=sys.stderr)
        sys.exit(1)
    import indextts.infer_v2
    class DummyQwenEmotion:
        def __init__(self, *args, **kwargs):
            pass
        def inference(self, *args, **kwargs):
            return {}
    indextts.infer_v2.QwenEmotion = DummyQwenEmotion
    if HF_CACHE:
        os.environ["HF_HUB_CACHE"] = HF_CACHE          # the import above overwrote it; put the real folder back
    try:
        from huggingface_hub import constants as _hfc
        print(f"[IndexTTS-2] model cache: {_hfc.HF_HUB_CACHE}", flush=True)
    except ImportError:
        pass
    from indextts.infer_v2 import IndexTTS2
    tts = IndexTTS2(cfg_path=str(MODEL_DIR / "config.yaml"), model_dir=str(MODEL_DIR), use_fp16=(device == "cuda"),
                    device=device, use_cuda_kernel=os.environ.get("IDX2_CUDA_KERNEL") == "1",
                    use_deepspeed=os.environ.get("IDX2_DEEPSPEED") == "1",
                    use_accel=USE_ACCEL)   # needs flash-attn; timing test 2026-11-07
    if USE_ACCEL:
        print("[warn] IDX2_USE_ACCEL=1: the accel engine ignores num_beams and repetition_penalty", file=sys.stderr)
    # The text-emotion model (Qwen 0.6B, ~1.2 GB on the GPU) is only used for emo_text; we always give an emotion VECTOR, so it
    # sits idle. On an 8 GB RunPod RTX 3070 the worker died with CUDA out of memory on the first long line (2026-11-04: beam-search
    # cache, 7.64 GiB in use); on Windows the driver spills into system RAM, which hid it here. Freeing it changes no output.
    try:
        import gc
        import torch
        if getattr(tts, "qwen_emo", None) is not None:
            tts.qwen_emo.model = None
            tts.qwen_emo = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception as e:
        print(f"[warn] could not free the text-emotion model: {e}", file=sys.stderr)
    return tts


def prepare_reference(refs_dir, ref_clip, cache_dir):
    """IndexTTS-2 reads a wav and the references are mostly Ogg, so each clip is converted once (24 kHz mono) and
    reused. In serve mode cache_dir outlives the job, so the next job with the same voice does not convert it again.
    The name carries the source file's size and modification time, so a changed source never reuses an old wav. The
    wav is written under a temporary name first, so a process killed mid-convert leaves no half file in the cache."""
    src = Path(refs_dir) / ref_clip
    st = src.stat()
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{Path(ref_clip).stem}_{st.st_size}_{st.st_mtime_ns}.wav"
    if not out.exists():
        part = out.with_name(out.stem + f".{os.getpid()}.part.wav")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "24000",
                            str(part)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            try:
                part.unlink()
            except OSError:
                pass
            raise RuntimeError(f"reference convert failed for {ref_clip}: {r.stderr[-300:]}")
        os.replace(str(part), str(out))
    return out


def seed_all(seed):
    """Every RNG the engine may draw from, set right before a line (or a piece of a split line) is made."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def by_voice(pending):
    """Lines that share a voice sample next to each other, first-seen voice first, each voice's lines in their own
    order. The engine keeps the last voice sample's setup and redoes it whenever the sample changes, so a batch that
    jumps between voices pays for that setup again and again. The seed is set per line, so the order changes no take."""
    first = {}
    for i, (r, _) in enumerate(pending):
        first.setdefault(r["canonical_reference_clip"], i)
    return sorted(pending, key=lambda p: first[p[0]["canonical_reference_clip"]])


def run_batch(tts, batch_file, output_dir, references, bitrate="32k", device="cuda", ref_cache=None):
    """Make one batch file. -> exit code (0 all made or held, 1 a failure). `tts` is a loaded model, or None to load
    one only if a line needs making (the one-shot command). `ref_cache` is a folder that keeps converted references
    between batches (serve mode); None keeps them in the batch's own temporary folder, as the one-shot command always
    did. Paths are made absolute first: in serve mode the process does not run in the job's folder."""
    batch = Path(batch_file).resolve()
    out = Path(output_dir).resolve()
    refs = Path(references).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / ".tmp_indextts2"
    tmp.mkdir(exist_ok=True)
    all_rows = json.loads(batch.read_text(encoding="utf-8"))
    rows = [r for r in all_rows if r.get("model_engine") == ENGINE]
    print(f"[IndexTTS-2] {batch.name}: {len(rows)} of {len(all_rows)} rows are this engine's", flush=True)
    if not rows:
        return 0

    missing = sorted({r["canonical_reference_clip"] for r in rows if not (refs / r["canonical_reference_clip"]).exists()})
    if missing:
        print(f"[FATAL] Missing {len(missing)} reference files in {refs}: {missing[:10]}", file=sys.stderr)
        return 1

    revision = model_revision()
    prov_path = out / "generation_provenance.jsonl"
    prov = {}
    if prov_path.exists():
        for line in prov_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                prov[rec["sound_file"]] = rec
            except (ValueError, KeyError):
                pass
    qc_log = out / rw.QC_LOG
    qc_held = {}
    if qc_log.exists():
        for line in qc_log.read_text(encoding="utf-8").splitlines():
            try:
                k = json.loads(line).get("sound_file")
            except ValueError:
                continue
            if k:
                qc_held[k] = qc_held.get(k, 0) + 1

    pending, skipped, held = [], 0, 0
    for r in rows:
        sf_name = r["sound_file"]
        fp = recipe_fingerprint(r, revision, bitrate)
        final = out / sf_name
        if final.exists() and rw.is_valid_output(final) and prov.get(sf_name, {}).get("recipe_fingerprint") == fp:
            skipped += 1
            continue
        if qc_held.get(sf_name, 0) >= rw.QC_TRIES:
            held += 1
            continue
        pending.append((r, fp))
    pending = by_voice(pending)
    print(f"[IndexTTS-2] {len(pending)} to make, {skipped} already made, {held} held by QC", flush=True)

    done = failed = 0
    failures = []
    t_start = time.time()
    if pending:
        if tts is None:
            if device == "cuda" and not torch.cuda.is_available():
                print("[FATAL] CUDA requested but not available.", file=sys.stderr)
                return 1
            tts = load_model(device)
        host = socket.gethostname()
        with open(prov_path, "a", encoding="utf-8") as pf:
            lock = threading.Lock()
            stats = {"done": 0, "held": 0, "failed": 0}
            # Post-processing (wav write, ffmpeg level + Ogg encode, QC) runs on two helper threads while the GPU makes the
            # next line. The seed is set per line right before infer, so the order lines finish in changes nothing in a take.
            pool = ThreadPoolExecutor(max_workers=2)
            futs = []

            def finish(row, fp, seed, vec, beams, sr, w, wav_path, ogg_path, t_infer):
                sf_name = row["sound_file"]
                text = row["cleaned_text"]
                t_fin = time.time()
                try:
                    arr = np.asarray(w).astype(np.float32) / 32768.0
                    if arr.ndim > 1:
                        arr = arr.reshape(arr.shape[0], -1).mean(axis=1)
                    peak = float(np.max(np.abs(arr))) if len(arr) else 0.0
                    if peak > 1.0:
                        arr = arr / peak * 0.98
                    sf.write(str(wav_path), arr, sr)
                    level = rw.apply_post_fx_and_encode(wav_path, ogg_path, row.get("post_af", ""), bitrate=bitrate,
                                                        shout="!" in (text or ""))
                    dur, osr, och = rw.get_audio_info(ogg_path)
                    if dur <= 0.05 or osr != rw.OUTPUT_SAMPLE_RATE or och != rw.OUTPUT_CHANNELS:
                        raise ValueError(f"encoded audio failed validation (duration={dur}s, sr={osr}, channels={och})")
                    why = rw.qc_clip(dur, text)
                    if why:      # a loop, a runaway or a sliver: waits in _held_qc/ for a listen, next run uses the next seed
                        held_dir = out / rw.QC_HELD_DIR
                        held_dir.mkdir(exist_ok=True)
                        os.replace(str(ogg_path), str(held_dir / sf_name))
                        rec = json.dumps({"sound_file": sf_name, "why": why, "duration_sec": round(dur, 3),
                                          "generation_seed": seed, "profile_key": row["profile_key"],
                                          "npc_name": row.get("npc_name", ""), "hostname": host, "engine": ENGINE,
                                          "timestamp": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False)
                        with lock:      # two finish threads can hold lines at the same moment, so writes take the lock
                            with open(qc_log, "a", encoding="utf-8") as qf:
                                qf.write(rec + "\n")
                            stats['held'] += 1
                        print(f"[QC] held {sf_name}: {why}", flush=True)
                        return
                    os.replace(str(ogg_path), str(out / sf_name))
                    gen = t_infer + (time.time() - t_fin)
                    rec = {"sound_file": sf_name, "text_hash": row["text_hash"], "recipe_fingerprint": fp,
                           "profile_key": row["profile_key"], "npc_name": row.get("npc_name", ""),
                           "canonical_reference_clip": row["canonical_reference_clip"], "ref_text": row.get("ref_text", ""),
                           "model_engine": ENGINE, "model_revision": revision, "generation_seed": seed,
                           "line_emotion": row.get("line_emotion", ""), "emo_vector": vec, "emo_alpha": EMO_ALPHA if vec else None,
                           "num_beams": 1 if USE_ACCEL else beams,   # the accel engine ignores num_beams
                           "post_af": row.get("post_af", ""), "bitrate": bitrate,
                           "measured_lufs": (level or {}).get("measured_lufs"), "level": (level or {}).get("level"),
                           "level_recipe": (level or {}).get("level_recipe"), "duration_sec": round(dur, 3),
                           "gen_time_sec": round(gen, 3), "rtf": round(gen / dur, 3) if dur > 0 else 0.0,
                           "timestamp": datetime.now(timezone.utc).isoformat(), "hostname": host,
                           "torch_version": torch.__version__}
                    with lock:
                        pf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        pf.flush()
                        prov[sf_name] = rec
                        stats['done'] += 1
                except Exception as e:
                    with lock:
                        stats["failed"] += 1
                        failures.append({"sound_file": sf_name, "error": str(e)})
                    print(f"[Error] Failed {sf_name}: {e}", file=sys.stderr)
                finally:
                    for p_ in (wav_path, ogg_path):
                        if p_.exists():
                            p_.unlink()

            for i, (row, fp) in enumerate(pending, start=1):
                sf_name = row["sound_file"]
                text = row["cleaned_text"]
                seed = rw.compute_generation_seed(row) + qc_held.get(sf_name, 0)   # a held take retries on the next seed
                vec = emotion_vector(row)
                beams = int(row["num_beams"]) if row.get("num_beams") else beams_for(vec)
                # Named by the line's place in the batch, not the clock. Two lines made close together could get the
                # same time_ns() on Windows, and the two finish threads then wrote and moved each other's files.
                wav_path = tmp / f"t_{os.getpid()}_{i}.wav"
                ogg_path = tmp / f"t_{os.getpid()}_{i}.ogg"
                t0 = time.time()
                try:
                    seed_all(seed)
                    ref = prepare_reference(refs, row["canonical_reference_clip"],
                                            Path(ref_cache) if ref_cache is not None else tmp / "refs")
                    # 2026-11-07: beams by emotion (beams_for); infer_v2 defaults to 3
                    kw = dict(spk_audio_prompt=str(ref), output_path=None, verbose=False, num_beams=beams)
                    if row.get("temperature") is not None:
                        kw["temperature"] = float(row["temperature"])
                    if row.get("repetition_penalty") is not None:
                        kw["repetition_penalty"] = float(row["repetition_penalty"])
                    if vec and any(vec):
                        kw.update(emo_vector=vec, emo_alpha=EMO_ALPHA)
                    parts = split_long_text(text)
                    if len(parts) <= 1:
                        sr, w = tts.infer(text=text, **kw)
                    else:
                        part_waves = []
                        for p_idx, part_text in enumerate(parts, start=1):
                            seed_all(int(hashlib.sha1(f"{seed}|split|{p_idx}".encode("utf-8")).hexdigest(), 16)
                                     % 2_000_000_000)
                            sr, p_w = tts.infer(text=part_text, **kw)
                            part_waves.append(np.asarray(p_w).reshape(-1))
                        w = np.concatenate(part_waves, axis=0)
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception as e:
                    with lock:
                        stats["failed"] += 1
                        failures.append({"sound_file": sf_name, "error": str(e)})
                    print(f"[Error] Failed {sf_name}: {e}", file=sys.stderr)
                    if rw.is_gpu_fatal(e):
                        print("[FATAL] GPU state is unsafe; stopping so the shard can resume in a fresh process.",
                              file=sys.stderr)
                        pool.shutdown(wait=True)
                        raise
                    continue
                futs.append(pool.submit(finish, row, fp, seed, vec, beams, sr, w, wav_path, ogg_path, time.time() - t0))
                if len(futs) > 8:                 # keep the backlog (and held audio arrays) small
                    futs.pop(0).result()
                if i % 25 == 0:
                    rate = stats["done"] / (time.time() - t_start) * 3600
                    print(f"[{ENGINE}] Progress: {i}/{len(pending)} | Done: {stats['done']} | Rate: {rate:.1f} clips/hr", flush=True)
            pool.shutdown(wait=True)
            done, failed, held = stats["done"], stats["failed"], held + stats["held"]
    shutil.rmtree(tmp, ignore_errors=True)
    summary = {"batch_file": str(batch.resolve()), "output_dir": str(out.resolve()), "engine": ENGINE,
               "total_items": len(rows), "newly_generated": done, "skipped_valid": skipped, "failed": failed,
               "held_qc": held, "walltime_sec": round(time.time() - t_start, 3),
               "completed": failed == 0 and done + skipped + held == len(rows),
               "timestamp": datetime.now(timezone.utc).isoformat()}
    (out / f"summary_{batch.stem}_{ENGINE}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[IndexTTS-2] made {done}, skipped {skipped}, held {held}, failed {failed}", flush=True)
    if failures:
        (out / f"failed_{batch.stem}_{ENGINE}.json").write_text(json.dumps(failures, indent=2), encoding="utf-8")
        return 1
    return 0


READY_MARKER = "[ENGINE-READY]"
DONE_MARKER = "[JOB-DONE]"
REF_CACHE_DAYS = 14          # converted references unused this long are removed when the engine starts


def _prune_ref_cache(cache_dir, days=REF_CACHE_DAYS):
    """Keep the serve-mode reference cache from growing for ever: drop wavs not touched for `days` and any half-written
    file a killed process left behind."""
    cutoff = time.time() - days * 86400
    try:
        for e in os.scandir(cache_dir):
            if e.is_file() and (".part." in e.name or e.stat().st_mtime < cutoff):
                try:
                    os.unlink(e.path)
                except OSError:
                    pass
    except OSError:
        pass


def serve(device="cuda", ref_cache=None, stdin=None, load=None, runner=None):
    """Load the model once, then make one batch per request line read from stdin, so a run of jobs does not pay the
    several-GB model load each time. Protocol (one JSON object per line):
        {"batch_file": ..., "output_dir": ..., "references": ..., "bitrate": ...}
    Prints READY_MARKER once the model is loaded and "[JOB-DONE] rc=<n>" after each request; end of stdin is a clean
    exit (0). Any exception that escapes a batch (a GPU fault, or anything unforeseen) ends the process (exit 3) after
    its marker: the caller starts a fresh one for the next job, as the one-process-per-job mode always did.
    `stdin`, `load` and `runner` are for tests."""
    stdin = sys.stdin.buffer if stdin is None else stdin
    runner = run_batch if runner is None else runner
    cache = Path(ref_cache).resolve() if ref_cache else (Path.cwd() / "ref_wav_cache").resolve()
    cache.mkdir(parents=True, exist_ok=True)
    _prune_ref_cache(cache)
    if load is None:
        if device == "cuda" and not torch.cuda.is_available():
            print("[FATAL] CUDA requested but not available.", file=sys.stderr, flush=True)
            return 1
        tts = load_model(device)
    else:
        tts = load()
    print(READY_MARKER, flush=True)

    def done(rc):
        sys.stderr.flush()                       # everything the batch printed comes before its marker
        print(f"{DONE_MARKER} rc={rc}", flush=True)

    for raw in iter(stdin.readline, b""):
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict) or not all(isinstance(req.get(k), str) and req.get(k)
                                                    for k in ("batch_file", "output_dir", "references")):
                raise ValueError("batch_file, output_dir and references are required")
        except ValueError as e:
            print(f"[serve] request refused: {e}", file=sys.stderr)
            done(2)
            continue
        try:
            rc = runner(tts, req["batch_file"], req["output_dir"], req["references"], str(req.get("bitrate") or "32k"),
                        device=device, ref_cache=cache)
        except BaseException:
            import traceback
            traceback.print_exc()
            done(1)
            return 3
        done(int(rc or 0))
    return 0


def no_error_boxes():
    """Windows only: a crashing program must not open a modal error box nobody is there to click (the job would sit
    behind it for ever). Children inherit the setting. Never raises."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


def main():
    no_error_boxes()
    ap = argparse.ArgumentParser(description="IndexTTS-2 worker")
    ap.add_argument("--batch-file")
    ap.add_argument("--output-dir")
    ap.add_argument("--references", default=str(rw.DEFAULT_REFS))
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--bitrate", default="32k")
    ap.add_argument("--serve", action="store_true", help="keep the model loaded; read one batch request per stdin line")
    ap.add_argument("--ref-cache", help="serve mode: folder that keeps converted references between batches")
    a = ap.parse_args()
    if a.serve:
        return serve(a.device, a.ref_cache)
    if not a.batch_file or not a.output_dir:
        ap.error("--batch-file and --output-dir are required (or --serve)")
    return run_batch(None, a.batch_file, a.output_dir, a.references, a.bitrate, device=a.device)


if __name__ == "__main__":
    sys.exit(main())
