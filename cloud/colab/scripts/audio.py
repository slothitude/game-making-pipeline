"""audio.py — the gpu.audio runner (prompt/text -> 44.1 kHz wav [+ ogg]). EXECUTES ON THE COLAB RUNTIME.

Contract with cloud/colab/colab_worker.py (the only thing that ships it here):
  - in.zip contains work/job.json — job.json payload:
      {"kind": "sfx" | "music" | "voice",
       "prompt": str,            # sfx/music conditioning text; voice uses "text"
       "text": str?,             # voice alias (either key is read)
       "name": "coin_pickup",    # out name -> out/coin_pickup.wav (default audio)
       "seconds": 5,             # clamped to the per-kind cap (sfx 10 / music 30)
       "seed": int?,             # torch generator seed before the gen call
       "real": true?}            # alt gate to env GMP_AUDIO_REAL=1
  - this script writes out/<name>.wav (+ out/<name>.ogg when ffmpeg exists)
    + result.json, zips out/ -> out.zip
  - the worker downloads out.zip + result.json and tears the runtime down.

STATUS OF THE CODE
  DEFAULT PATH = STUB: a 0.5 s 44.1 kHz 440 Hz sine (stdlib-only, runs anywhere)
  so the downstream pipeline always gets a well-formed wav and a truthful
  result.json. Never fake-claims generated audio.
  REAL PATH = written to the model cards' documented API, NEVER RUN (no Colab
  auth from the dev box — same KNOWN_GAP as the anim lane; first burn must be
  watched):
    sfx   -> diffusers AudioLDM2Pipeline, "cvssp/audioldm2", fp16, 16 kHz out
             (model-card form: pipe(prompt=..., audio_length_in_s=...).audios[0])
    music -> transformers MusicgenForConditionalGeneration,
             "facebook/musicgen-small", fp16, 32 kHz out, 50 audio tokens per
             second (max_new_tokens = 50 * seconds)
    voice -> REFUSES: no proven free TTS is picked yet (bark = candidate, heavy
             for T4 and untested). The job falls back to the stub with
             real_error instead of shipping fake speech. See README KNOWN_GAPS.
  T4 discipline (the mesh lane's LOW-RAM law): fp16 everywhere, load ->
  generate -> del + torch.cuda.empty_cache() inside each gen fn (never two
  models resident), seconds capped per kind, idempotent pip setup under a
  marker file, pin loop against idle reaping (hy_all law, same as mesh.py).
  Post: loudness normalize (RMS target, peak-capped) + linear resample to
  44.1 kHz (numpy on the runtime; stdlib wave writes the file) + a vorbis ogg
  sibling when ffmpeg exists on the runtime.

Local sanity run (stub, no network):
  python scripts/audio.py --base ./selftest_base     # in.zip with job.json inside
"""
import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import wave
import zipfile
from pathlib import Path

# models (model-card ids) + rates (model-card output rates)
SFX_MODEL = "cvssp/audioldm2"
MUSIC_MODEL = "facebook/musicgen-small"
SFX_SR = 16000    # audioldm2 output sample rate (model card)
MUSIC_SR = 32000  # musicgen output sample rate (model.config.audio_encoder.sampling_rate)
MUSIC_TOKENS_PER_SEC = 50   # musicgen: one audio token per 1/50 s
AUDIOLDM_STEPS = int(os.environ.get("GMP_AUDIO_STEPS", "200"))  # model-card default
AUDIOLDM_GUIDANCE = float(os.environ.get("GMP_AUDIO_GUIDANCE", "3.5"))

# T4 caps (task law): sfx <= 10 s, music <= 30 s. voice is N/A (refused real).
MAX_SECONDS = {"sfx": 10.0, "music": 30.0, "voice": 15.0}
DEFAULT_SECONDS = {"sfx": 5.0, "music": 15.0, "voice": 10.0}
KINDS = ("sfx", "music", "voice")

# output normalization law
TARGET_SR = 44100
TARGET_RMS = 0.12    # ~ -18 dBFS loudness target
PEAK_CEIL = 0.98     # hard peak ceiling post-gain

ENV_MARKER = Path("/content/.gmp_audio_env_done")


def log(msg):
    print(f"[audio] {msg}", flush=True)


def _locate_base(explicit=None) -> Path:
    for cand in ([Path(explicit)] if explicit else []) + \
                [Path(os.environ.get("GMP_COLAB_BASE", "/content/gmp_job")), Path.cwd()]:
        if (cand / "in.zip").exists():
            return cand
    raise FileNotFoundError(f"in.zip not found (base={explicit} env={os.environ.get('GMP_COLAB_BASE')} cwd={Path.cwd()})")


def _load_work(base: Path) -> dict:
    work = base / "work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    with zipfile.ZipFile(base / "in.zip") as z:
        z.extractall(work)
    jf = work / "job.json"
    return json.loads(jf.read_text(encoding="utf-8")) if jf.exists() else {}


# ---------------------------------------------------------------------------
# wav writing — stdlib wave, int16. numpy arrays take a tobytes fast path.
# ---------------------------------------------------------------------------
def _write_wav16(path: Path, x, sr: int, channels: int = 1) -> None:
    if hasattr(x, "tobytes"):  # numpy fast path (real path only; stub stays stdlib)
        import numpy as np
        pcm = (np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0) * 32767.0).astype("<i2")
        frames = pcm.reshape(-1).tobytes()
    else:
        frames = b"".join(struct.pack("<h", max(-32768, min(32767, int(round(v * 32767.0)))))
                          for v in x)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(frames)


def _stub_sine(path: Path, seconds: float = 0.5, hz: float = 440.0, sr: int = TARGET_SR):
    n = int(sr * seconds)
    x = (0.5 * math.sin(2.0 * math.pi * hz * (i % sr) / sr) for i in range(n))
    _write_wav16(path, x, sr)
    return seconds, sr


# ---------------------------------------------------------------------------
# REAL PATH — gated (GMP_AUDIO_REAL=1 or payload.real), Colab runtime only.
# ---------------------------------------------------------------------------
def _pin_loop():
    """Proven anti-reap trick (hy_all law, same as mesh.py: idle T4s get reaped)."""
    try:
        p = subprocess.Popen(
            ["bash", "-lc",
             "( while true; do date +\"PIN %s\" >> /content/gmp_pin.log; sleep 60; done )"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("pin loop up (anti-reap, hy_all law)")
        return p
    except Exception as exc:  # a dead pin loop must never kill the job
        log(f"pin loop skipped: {exc}")
        return None


def _sh(cmd, **kw):
    log(f"$ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, check=True, **kw)


# ---------------------------------------------------------------------------
# cold-start cache (retromonkey capability URL — unguessable path only; the
# contents are public PyPI wheels / public model weights). One HTTPS pull per
# tar per session replaces the per-session PyPI + HF hub re-download tax.
# EVERY step here is non-fatal: a miss or a stale tar degrades to exactly the
# pre-cache download path. Build + refresh procedure: cloud/colab/CACHE.md.
# ---------------------------------------------------------------------------
CC_BASE = "https://retromonkey.com.au/cc/fbf37bb3bf14379a/"
CC_ROOT = Path("/content/gmp_cc")
CC_WHEELS = CC_ROOT / "wheels"      # wheels.tar  -> pip --no-index --find-links
CC_WEIGHTS = CC_ROOT / "weights"    # weights.tar -> hub/models--* (HF_HOME target)
_CC_WHEEL_NAMES = None              # lazy listing cache


def _pip_raw(*args):
    _sh([sys.executable, "-m", "pip", "install", "-q", *args])


def _req_lines(args) -> list:
    """['-r', file, 'pkg', ...] -> individual requirement strings, so the
    per-line fallback can isolate one bad line (sdist-only, torch, stale pin)
    without losing the cached rest. -r files exist by the time _pip runs."""
    reqs, i = [], 0
    while i < len(args):
        a = str(args[i])
        i += 1
        if a in ("-r", "--requirement"):
            p = Path(str(args[i])) if i < len(args) else None
            i += 1
            if p is not None and p.is_file():
                reqs += [ln.strip() for ln in
                         p.read_text(encoding="utf-8").splitlines()
                         if ln.strip() and not ln.strip().startswith("#")]
            else:
                reqs.append(f"{a} {p}")     # let pip report the missing file
        else:
            reqs.append(a)
    return reqs


def _wheel_cached(req: str) -> bool:
    """True when the cold-start dir holds a wheel for this requirement's name
    (case/underscore-normalized prefix match; git/URL lines never match)."""
    global _CC_WHEEL_NAMES
    name = re.split(r"[<>=!~\[;@ ]", req.strip(), 1)[0].strip().lower()
    if not name or "://" in name or name.startswith(("-", "git+")):
        return False
    if _CC_WHEEL_NAMES is None:
        _CC_WHEEL_NAMES = ([p.name.lower() for p in CC_WHEELS.iterdir()
                            if p.name.lower().endswith(".whl")]
                           if CC_WHEELS.is_dir() else [])
    key = name.replace("-", "_") + "-"
    return any(n.startswith(key) for n in _CC_WHEEL_NAMES)


def _pip(*args):
    """Cache-first install (cold-start law): one --no-index pass when the cache
    satisfies the whole set, else per-requirement lines with cached wheels
    first and the normal index last. A cache miss never gates the job."""
    if CC_WHEELS.is_dir():
        try:
            _pip_raw("--no-index", "--find-links", str(CC_WHEELS), *args)
            return
        except subprocess.CalledProcessError as exc:
            log(f"cache-only install failed (rc={exc.returncode}) -> per-line fallback")
    for req in _req_lines(args):
        cached = _wheel_cached(req)
        try:
            if cached:
                _pip_raw("--no-index", "--find-links", str(CC_WHEELS), req)
            else:
                _pip_raw(req)
        except subprocess.CalledProcessError:
            if not cached:
                raise               # index attempt failed — same as pre-cache behavior
            log(f"cached wheel unusable for {req!r} -> normal index")
            _pip_raw(req)


def _cc_pull(tar_name: str, dest: Path) -> bool:
    """curl one cache tar into /content and untar it. False = miss -> fallback."""
    tgz = CC_ROOT / tar_name
    try:
        _sh(["curl", "-fsSL", "--retry", "2", "--max-time", "900",
             "-o", str(tgz), CC_BASE + tar_name])
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tgz) as t:
            try:
                t.extractall(dest, filter="data")   # py>=3.11.4 / 3.12+
            except TypeError:
                t.extractall(dest)
        log(f"cold-start: {tar_name} pulled + untarred")
        return True
    except Exception as exc:
        log(f"cold-start cache miss ({tar_name}): {type(exc).__name__}: {exc} "
            f"-> normal download path")
        return False
    finally:
        if tgz.exists():
            tgz.unlink()


def _cc_bootstrap() -> dict:
    """Fill /content/gmp_cc from the cache, once per runtime. Never raises."""
    info = {"wheels": CC_WHEELS.is_dir(), "weights": (CC_WEIGHTS / "hub").is_dir()}
    if info["wheels"] or info["weights"]:
        return info
    try:
        CC_ROOT.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        info["wheels"] = _cc_pull("wheels.tar", CC_ROOT)
        info["weights"] = _cc_pull("weights.tar", CC_WEIGHTS)
        if info["weights"]:
            os.environ["HF_HOME"] = str(CC_WEIGHTS)   # diffusers/transformers/HF hub read this
        log(f"cold-start cache: wheels={info['wheels']} weights={info['weights']} "
            f"in {time.time() - t0:.0f}s")
    except Exception as exc:  # cache is a speedup, never a gate
        log(f"cold-start cache skipped: {type(exc).__name__}: {exc}")
    return info


def _setup_env() -> None:
    """Idempotent pip setup. torch + numpy are runtime-provided (Colab); scipy/
    soundfile are NOT needed — the wav is written by stdlib wave."""
    if ENV_MARKER.exists():
        log("setup: env marker found — skipping (idempotent re-entry)")
        return
    t0 = time.time()
    _cc_bootstrap()   # cold-start wheels + weights (non-fatal)
    import torch
    log(f"torch {torch.__version__} cuda={torch.cuda.is_available()}")
    _pip("diffusers", "transformers", "accelerate")
    ENV_MARKER.write_text("ok\n", encoding="utf-8")
    log(f"setup done in {time.time() - t0:.0f}s")


def _require_colab_runtime() -> None:
    """Same gate as mesh.py: the real path provisions pip packages and needs
    CUDA — it may ONLY run on the Colab runtime. Armed runs elsewhere fail
    fast into the stub instead of touching the host."""
    if os.name != "posix" or not Path("/content").exists():
        raise RuntimeError(
            "real audio path requires the Colab Linux runtime (/content present); "
            f"refusing on {sys.platform} — leave the gate off on dev boxes, the "
            "stub path is the default")


def _clamp_seconds(kind: str, seconds) -> float:
    cap = MAX_SECONDS.get(kind, 10.0)
    val = DEFAULT_SECONDS.get(kind, 5.0) if seconds in (None, "") else float(seconds)
    val = max(1.0, min(cap, val))
    return val


def _gen_sfx(prompt: str, seconds: float, seed):
    """AudioLDM2 (cvssp/audioldm2), diffusers, fp16 — model-card call form.
    UNVERIFIED-as-in-never-run: the kwarg names come from the diffusers docs."""
    import torch
    from diffusers import AudioLDM2Pipeline
    pipe = AudioLDM2Pipeline.from_pretrained(SFX_MODEL, torch_dtype=torch.float16)
    pipe = pipe.to("cuda")
    log(f"SFX_LOADED {SFX_MODEL} fp16")
    gen = torch.Generator(device="cuda").manual_seed(int(seed)) if seed is not None else None
    try:
        with torch.inference_mode():
            audio = pipe(prompt=prompt,
                         audio_length_in_s=float(seconds),
                         num_inference_steps=AUDIOLDM_STEPS,
                         guidance_scale=AUDIOLDM_GUIDANCE,
                         generator=gen).audios[0]
    finally:
        del pipe
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
    log(f"SFX_DONE {seconds:.1f}s VRAM peak {torch.cuda.max_memory_allocated() / 1e9:.1f}GB")
    return audio, SFX_SR


def _gen_music(prompt: str, seconds: float, seed):
    """MusicGen small (facebook/musicgen-small), transformers, fp16 — model-card
    call form (50 audio tokens per second). UNVERIFIED-as-in-never-run."""
    import torch
    from transformers import AutoProcessor, MusicgenForConditionalGeneration
    proc = AutoProcessor.from_pretrained(MUSIC_MODEL)
    model = MusicgenForConditionalGeneration.from_pretrained(
        MUSIC_MODEL, torch_dtype=torch.float16).to("cuda")
    log(f"MUSIC_LOADED {MUSIC_MODEL} fp16")
    try:
        inputs = proc(text=[prompt], padding=True, return_tensors="pt").to("cuda")
        gen = None
        if seed is not None:
            gen = torch.Generator(device="cuda").manual_seed(int(seed))
        with torch.inference_mode():
            audio = model.generate(**inputs,
                                   do_sample=True,
                                   guidance_scale=3.0,
                                   max_new_tokens=int(seconds * MUSIC_TOKENS_PER_SEC),
                                   generator=gen)[0, 0].float().cpu().numpy()
    finally:
        del model
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
    log(f"MUSIC_DONE {seconds:.1f}s VRAM peak {torch.cuda.max_memory_allocated() / 1e9:.1f}GB")
    return audio, MUSIC_SR


def _refuse_voice():
    raise NotImplementedError(
        "voice: no proven free TTS is picked yet (bark/suno-bark is the candidate "
        "but is heavy for T4 and untested here). Refusing to ship fake speech — "
        "re-order kind='voice' once the model choice lands. See README KNOWN_GAPS.")


def _normalize_np(x):
    """RMS-target loudness normalize, peak-capped. numpy is runtime-provided."""
    import numpy as np
    x = np.asarray(x, dtype=np.float64).reshape(-1)  # mono downmix if ever stereo
    if x.size == 0:
        return x
    peak = max(float(np.max(np.abs(x))), 1e-9)
    rms = max(float(np.sqrt(np.mean(np.square(x)))), 1e-9)
    gain = min(TARGET_RMS / rms, PEAK_CEIL / peak)
    return x * gain


def _resample_np(x, sr_in: int, sr_out: int = TARGET_SR):
    """Linear-interp resample to 44.1 kHz. APPROXIMATION (no scipy/sinc) —
    flagged in README KNOWN_GAPS; fine for game sfx/music, not for mastering."""
    import numpy as np
    sr_in = int(sr_in)
    if sr_in == sr_out:
        return x
    n_out = max(1, int(round(len(x) * sr_out / float(sr_in))))
    t_in = np.arange(len(x), dtype=np.float64) / float(sr_in)
    t_out = np.arange(n_out, dtype=np.float64) / float(sr_out)
    return np.interp(t_out, t_in, x)


def _maybe_ogg(wav_path: Path, out: Path):
    """Godot-friendly vorbis sibling — only when ffmpeg exists on the runtime."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log("ogg skipped: no ffmpeg on the runtime")
        return None
    ogg = out / (wav_path.stem + ".ogg")
    try:
        proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path),
                               "-vn", "-c:a", "libvorbis", "-qscale:a", "5", str(ogg)],
                              capture_output=True, text=True)
    except Exception as exc:
        log(f"ogg failed: {exc}")
        return None
    if proc.returncode != 0 or not ogg.is_file():
        log(f"ogg failed rc={proc.returncode} {(proc.stderr or '').strip()[-200:]}")
        return None
    log(f"OGG_DONE {ogg.name}")
    return ogg


def _real_audio(payload: dict, work: Path, out: Path, wav_name: str) -> dict:
    _require_colab_runtime()
    t0 = time.time()
    timings = {}
    pin = _pin_loop()
    try:
        t = time.time()
        _setup_env()
        timings["setup_s"] = round(time.time() - t, 1)

        kind = payload.get("kind")
        prompt = str(payload.get("prompt") or payload.get("text") or "").strip()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {list(KINDS)}, got {kind!r}")
        if not prompt:
            raise ValueError(f"kind={kind!r} real path needs payload.prompt (voice: payload.text)")
        seed = payload.get("seed")
        seconds = _clamp_seconds(kind, payload.get("seconds"))
        if kind == "voice":
            _refuse_voice()

        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("real audio path is a T4 law (fp16 CUDA); no CUDA here")

        t = time.time()
        if kind == "sfx":
            audio, sr_in = _gen_sfx(prompt, seconds, seed)
        else:
            audio, sr_in = _gen_music(prompt, seconds, seed)
        timings["gen_s"] = round(time.time() - t, 1)
        peak_gb = round(float(torch.cuda.max_memory_allocated()) / 1e9, 2)

        t_post = time.time()
        wav_path = out / wav_name
        norm = _resample_np(_normalize_np(audio), sr_in)
        _write_wav16(wav_path, norm, TARGET_SR)
        audio_seconds = round(len(norm) / float(TARGET_SR), 3)
        timings["post_s"] = round(time.time() - t_post, 1)
        log(f"WAV_SAVED {wav_path} {audio_seconds}s @{TARGET_SR}Hz")

        ogg = _maybe_ogg(wav_path, out)
        return {"kind": kind,
                "model": SFX_MODEL if kind == "sfx" else MUSIC_MODEL,
                "prompt": prompt,
                "requested_seconds": payload.get("seconds"),
                "seconds_generated": seconds,
                "audio_seconds": audio_seconds,
                "sr_in": sr_in, "sr_out": TARGET_SR, "channels": 1,
                "seed": seed, "ogg": ogg.name if ogg else None,
                "steps": AUDIOLDM_STEPS if kind == "sfx" else None,
                "timings": timings, "vram_peak_gb": peak_gb}
    finally:
        if pin:
            pin.kill()


def main(argv=None):
    ap = argparse.ArgumentParser(description="gpu.audio runner (stub default, gated real path)")
    ap.add_argument("--base", default=None,
                    help="job base dir holding in.zip (default $GMP_COLAB_BASE, /content/gmp_job, cwd)")
    args = ap.parse_args(argv)

    t0 = time.time()
    base = _locate_base(args.base)
    job = _load_work(base)
    payload = job.get("payload", {})
    prompt = str(payload.get("prompt") or payload.get("text") or "")
    kind = payload.get("kind")
    name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(payload.get("name") or "")) or "audio"
    out = base / "out"
    out.mkdir(parents=True, exist_ok=True)
    wav_name = f"{name}.wav"

    want_real = os.environ.get("GMP_AUDIO_REAL") == "1" or payload.get("real") is True
    real_err = None
    result = None
    if want_real:
        try:
            stats = _real_audio(payload, base / "work", out, wav_name)
            wav = out / wav_name
            result = {"ok": True, "type": "gpu.audio", "stub": False,
                      "pipeline": ("diffusers AudioLDM2Pipeline" if stats["kind"] == "sfx"
                                   else "transformers MusicgenForConditionalGeneration"),
                      "name": name, "wav": f"out/{wav_name}",
                      "wav_bytes": wav.stat().st_size,
                      "seconds": round(time.time() - t0, 2), **stats}
            log(f"WAV_SAVED {wav}")
        except Exception as exc:  # fall through to the stub, never lose the slot
            log(f"real path FAILED ({type(exc).__name__}: {exc}) -> stub audio so the job still returns")
            real_err = f"{type(exc).__name__}: {exc}"

    if not want_real or real_err:
        wav = out / wav_name
        stub_seconds, stub_sr = _stub_sine(wav)
        result = {"ok": True, "type": "gpu.audio", "stub": True,
                  "stub_note": "0.5s 440 Hz sine; no model ran",
                  "real_path": ("GMP_AUDIO_REAL=1 -> audioldm2 (sfx) / musicgen-small "
                                "(music); voice refused pending a TTS pick (see module docstring)"),
                  "real_error": real_err,
                  "name": name, "wav": f"out/{wav_name}", "wav_bytes": wav.stat().st_size,
                  "kind": kind, "prompt": prompt,
                  "requested_seconds": payload.get("seconds"),
                  "audio_seconds": stub_seconds, "sr_out": stub_sr, "channels": 1,
                  "ogg": None,
                  "seconds": round(time.time() - t0, 2)}
    else:
        result["total_s"] = round(time.time() - t0, 1)

    (base / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with zipfile.ZipFile(base / "out.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out).as_posix())
    log(f"done: stub={result['stub']} kind={result.get('kind')} wav={result['wav']} "
        f"audio_s={result.get('audio_seconds')} total={result['seconds']}s")


if __name__ == "__main__":
    main()
