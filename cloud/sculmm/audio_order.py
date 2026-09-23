"""audio_order.py — the SCULMM audio work-order front (prompt/text -> engine
audio asset: sfx / music / voice). Companion to prop_order.py / anim_order.py;
SAME manifest law — the merge/slug helpers are imported from prop_order so
there is exactly one manifest writer convention in the lane (README documents
the leaning).

Two halves, both stdlib-only:

  ORDER (local)  make_audio_order(name, prompt=None, kind="sfx", seconds=None,
                                  seed=None, text=None, real=True) -> payload
                 {"kind": "sfx"|"music"|"voice",      # the payload kind IS the audio kind
                  "name": ..., "prompt": ...,         # voice carries "text" instead
                  "seconds": N, "real": true, ["seed": N]}
                 Ready for a queue POST; to_audio_job() wraps it in the
                 colab_worker envelope {"type": "gpu.audio", ...} -> the
                 gpu.audio runner (cloud/colab/scripts/audio.py, real path).
                 Caps (T4 law): sfx <= 10 s, music <= 30 s — out-of-range
                 seconds is a ValueError here; the runner clamps as a
                 last-line defense.

  RESOLVE (local) resolve_into_engine(wav_path, engine_assets_dir, name=None)
                 Copies the wav to <assets>/audio/<name>/<name>.wav (plus the
                 .ogg sibling when the runtime produced one) and merges into
                 <assets>/manifest.json an entry
                   {"name": ..., "file": "audio/<name>/<name>.wav",
                    "source": "colab-audio", "kind": "sfx"|"music"|"voice",
                    "seconds": N, "created": "<UTC ISO>"}
                 + informational sample_rate/channels/stub/ogg. Audio entries
                 share the manifest's single "props" list with mesh/anim
                 entries (one manifest, one writer, one idempotency law:
                 re-resolving a name REPLACES its entry — resolve under a
                 different name to keep both).

CLI (flat flags, anim_order style):
  python audio_order.py --make coin_pickup --prompt "retro coin blip" \
      --kind sfx --seconds 3 [--seed 7] [--out payload.json]
  python audio_order.py --resolve done/out/coin_pickup.wav --assets-dir <engine>/assets
  python audio_order.py --selftest     # temp dir, fake wav, resolve, show manifest
"""
import argparse
import json
import math
import shutil
import struct
import sys
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path

try:
    from prop_order import _merge_manifest, _slug  # one manifest law (direct-script run)
except ImportError:
    try:
        from .prop_order import _merge_manifest, _slug  # package import
    except ImportError:  # isolated copy — same law, last resort
        def _slug(name: str) -> str:
            import re
            s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_").lower()
            if not s or not re.match(r"[a-z]", s[0]):
                s = "audio_" + s
            return s

        def _merge_manifest(assets_dir: Path, entry: dict) -> Path:
            mf = assets_dir / "manifest.json"
            raw = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else None
            if raw is None:
                doc, key = {"props": [entry]}, "props"
            elif isinstance(raw, list):
                doc, key = [e for e in raw
                            if not (isinstance(e, dict) and e.get("name") == entry["name"])], None
            elif isinstance(raw, dict) and isinstance(raw.get("props"), list):
                doc, key = raw, "props"
            else:
                doc, key = raw, "props"
            if key is None:
                doc.append(entry)
            else:
                doc[key] = [e for e in doc[key]
                            if not (isinstance(e, dict) and e.get("name") == entry["name"])]
                doc[key].append(entry)
            mf.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return mf

SOURCE_TAG = "colab-audio"
KINDS = ("sfx", "music", "voice")
MAX_SECONDS = {"sfx": 10.0, "music": 30.0, "voice": 15.0}      # T4 law (runner clamps too)
DEFAULT_SECONDS = {"sfx": 5.0, "music": 15.0, "voice": 10.0}


# ---------------------------------------------------------------------------
# ORDER
# ---------------------------------------------------------------------------
def make_audio_order(name: str, prompt: str = None, kind: str = "sfx",
                     seconds=None, seed=None, text: str = None, real: bool = True) -> dict:
    """Build the gpu.audio payload. sfx/music take `prompt`; voice takes `text`
    (either key is read by the runner, but the payload carries the natural one).
    real=True rides the payload as the gate: worker env vars do NOT cross to
    the Colab runtime, so `real` in job.json is the reliable arm for audio.py's
    real path (GMP_AUDIO_REAL=1 is for direct/manual exec runs)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}, got {kind!r}")
    if kind == "voice":
        prompt, text = None, (text if text is not None else prompt)
    cond = (text if kind == "voice" else prompt)
    if not str(cond or "").strip():
        raise ValueError(f"a {kind} order needs {'text' if kind == 'voice' else 'prompt'}")
    seconds = DEFAULT_SECONDS[kind] if seconds in (None, "") else float(seconds)
    if not (0 < seconds <= MAX_SECONDS[kind]):
        raise ValueError(f"seconds must be in (0, {MAX_SECONDS[kind]}] for kind={kind}, "
                         f"got {seconds}")
    payload = {"kind": kind, "name": _slug(name), "seconds": seconds, "real": bool(real)}
    if kind == "voice":
        payload["text"] = str(text)
    else:
        payload["prompt"] = str(prompt)
    if seed is not None:
        payload["seed"] = int(seed)
    return payload


def to_audio_job(payload: dict, staging_dir, return_dir, gpu: str = "T4",
                 session: str = None) -> dict:
    """Wrap the payload in the colab_worker envelope. Audio needs no staged
    bytes, but the worker requires an existing staging_dir — it is created here.
    NOTE: colab_worker's runner map does not know "gpu.audio" yet — run_job
    returns bad_job until that line lands (README KNOWN_GAPS; human's edit)."""
    if payload.get("kind") not in KINDS:
        raise ValueError(f"not an audio payload: kind={payload.get('kind')!r}")
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    job = {"type": "gpu.audio", "staging_dir": str(staging),
           "return_dir": str(return_dir), "gpu": gpu, "payload": payload}
    if session:
        job["session"] = session
    return job


# ---------------------------------------------------------------------------
# RESOLVE — wav header read (stdlib wave)
# ---------------------------------------------------------------------------
def _wav_stats(path: Path) -> dict:
    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = (w.getframerate(), w.getnchannels(),
                         w.getsampwidth(), w.getnframes())
    return {"sample_rate": sr, "channels": ch, "sample_width": sw,
            "frames": n, "audio_seconds": round(n / float(sr), 3)}


def _sibling_result(wav: Path):
    """The worker writes result.json into the return dir; the wav lands in
    return_dir/out/. Check both neighbourhoods (anim_order law)."""
    for cand in (wav.parent / "result.json", wav.parent.parent / "result.json"):
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def resolve_into_engine(wav_path, engine_assets_dir, name: str = None) -> dict:
    """Copy the wav into assets/audio/<name>/ (+ the ogg sibling when present)
    and merge the manifest entry. Returns {"entry", "wav", "manifest"}."""
    wav = Path(wav_path)
    if not wav.is_file():
        raise FileNotFoundError(f"wav not found: {wav}")
    name = _slug(name or wav.stem)
    assets = Path(engine_assets_dir)
    dest_dir = assets / "audio" / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.wav"
    shutil.copy(wav, dest)

    stats = _wav_stats(dest)
    result = _sibling_result(wav)
    r = result if isinstance(result, dict) else {}
    entry = {"name": name,
             "file": (Path("audio") / name / f"{name}.wav").as_posix(),
             "source": SOURCE_TAG,
             "kind": r.get("kind") or "sfx",
             "seconds": r.get("audio_seconds") or stats["audio_seconds"],
             "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    entry["sample_rate"] = stats["sample_rate"]       # informational, like anim's verts
    entry["channels"] = stats["channels"]
    entry["stub"] = bool(r.get("stub"))               # a stub sine must not pass as generated
    ogg_src = wav.with_suffix(".ogg")
    if ogg_src.is_file():
        shutil.copy(ogg_src, dest_dir / ogg_src.name)
        entry["ogg"] = (Path("audio") / name / ogg_src.name).as_posix()
    manifest = _merge_manifest(assets, entry)
    return {"entry": entry, "wav": str(dest), "manifest": str(manifest)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_make(a) -> int:
    payload = make_audio_order(a.name, a.prompt, a.kind, a.seconds, a.seed, a.text)
    text = json.dumps(payload, indent=2)
    if a.kind == "voice":
        print("# NOTE kind=voice: no proven free TTS yet — the real path refuses "
              "and the job returns a stub (README KNOWN_GAPS)")
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
        print(f"payload -> {a.out}")
    print(text)
    return 0


def _cmd_resolve(a) -> int:
    res = resolve_into_engine(a.wav, a.assets_dir, a.name)
    print(json.dumps(res["entry"], indent=2))
    print(f"wav      -> {res['wav']}")
    print(f"manifest -> {res['manifest']}")
    return 0


def _fake_wav(path: Path, seconds: float = 0.25, hz: float = 440.0, sr: int = 44100) -> None:
    n = int(sr * seconds)
    x = [int(32767 * 0.5 * math.sin(2 * math.pi * hz * i / sr)) for i in range(n)]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{n}h", *x))


def _selftest() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    tmp = Path(tempfile.mkdtemp(prefix="sculmm_audio_selftest_"))
    assets = tmp / "engine" / "assets"
    print(f"SELFTEST tmp={tmp}")

    # 1. sfx order (explicit)
    payload = make_audio_order("Coin Pickup", "retro coin pickup blip",
                               kind="sfx", seconds=3, seed=7)
    checks = [("make_audio_order sfx payload", payload == {
        "kind": "sfx", "name": "coin_pickup", "seconds": 3.0,
        "real": True, "prompt": "retro coin pickup blip", "seed": 7})]
    print("sfx payload: " + json.dumps(payload))

    # 2. music defaults + voice text key + cap enforcement
    music = make_audio_order("Tavern Loop", "warm tavern ambience", kind="music")
    voice = make_audio_order("Narrator", text="welcome, traveler", kind="voice")
    try:
        make_audio_order("Too Long", "drone", kind="sfx", seconds=99)
        cap_ok = False
    except ValueError:
        cap_ok = True
    checks += [
        ("music default seconds 15", music["seconds"] == DEFAULT_SECONDS["music"]
         and "seed" not in music and music["prompt"] == "warm tavern ambience"),
        ("voice carries text, not prompt", voice["kind"] == "voice"
         and voice.get("text") == "welcome, traveler" and "prompt" not in voice),
        ("seconds over cap rejected", cap_ok),
    ]

    # 3. envelope + empty staging dir
    stage, ret = tmp / "stage", tmp / "done"
    job = to_audio_job(music, stage, ret, session="selftest-audio")
    checks += [("to_audio_job envelope", job["type"] == "gpu.audio" and job["gpu"] == "T4"
                and job["payload"] is music and stage.is_dir())]

    # 4. resolve with a real-shaped result.json + ogg sibling beside the wav
    out = ret / "out"
    out.mkdir(parents=True)
    done_wav = out / "tavern_loop.wav"
    _fake_wav(done_wav, seconds=0.25)
    (out / "tavern_loop.ogg").write_bytes(b"OggS-fake")
    (ret / "result.json").write_text(json.dumps(
        {"ok": True, "type": "gpu.audio", "stub": False, "name": "tavern_loop",
         "kind": "music", "audio_seconds": 15.0, "sr_out": 44100, "channels": 1,
         "ogg": "out/tavern_loop.ogg"}),
        encoding="utf-8")
    res = resolve_into_engine(done_wav, assets)
    entry = res["entry"]
    dest = Path(res["wav"])
    checks += [
        ("wav copied to audio/<name>/", dest == assets / "audio" / "tavern_loop"
         / "tavern_loop.wav" and dest.is_file()),
        ("ogg sibling copied", (assets / "audio" / "tavern_loop" / "tavern_loop.ogg").is_file()
         and entry["ogg"] == "audio/tavern_loop/tavern_loop.ogg"),
        ("entry law fields", entry["name"] == "tavern_loop"
         and entry["file"] == "audio/tavern_loop/tavern_loop.wav"
         and entry["source"] == "colab-audio" and entry["kind"] == "music"
         and entry["seconds"] == 15.0 and "T" in entry["created"]),
        ("entry wav header stats", entry["sample_rate"] == 44100
         and entry["channels"] == 1 and entry["stub"] is False),
    ]

    # 5. idempotent re-resolve + a second name merges
    resolve_into_engine(done_wav, assets)
    resolve_into_engine(done_wav, assets, "tavern_alt")
    doc = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    names = [e["name"] for e in doc["props"]]
    checks += [("re-resolve replaces, second name merges",
                names == ["tavern_loop", "tavern_alt"])]

    # 6. stub honesty: a stub result must record stub True on the entry
    stub_dir = tmp / "stubdone" / "out"
    stub_dir.mkdir(parents=True)
    (stub_dir / "coin_pickup.wav").write_bytes((done_wav).read_bytes())
    (stub_dir.parent / "result.json").write_text(json.dumps(
        {"ok": True, "type": "gpu.audio", "stub": True, "kind": "sfx",
         "audio_seconds": 0.5}), encoding="utf-8")
    stub_entry = resolve_into_engine(stub_dir / "coin_pickup.wav", assets, "coin_pickup")["entry"]
    checks += [("stub result -> entry stub: true", stub_entry["stub"] is True
                and stub_entry["kind"] == "sfx" and stub_entry["seconds"] == 0.5)]

    print("--- manifest.json ---")
    print((assets / "manifest.json").read_text(encoding="utf-8"))
    print("---------------------")

    ok = all(p for _, p in checks)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    print(f"SELFTEST {'GREEN' if ok else 'RED'}")
    if ok:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(description="SCULMM audio work-order front")
    ap.add_argument("--make", dest="name", metavar="NAME",
                    help="build a gpu.audio payload (needs --prompt or --text)")
    ap.add_argument("--resolve", dest="wav", metavar="WAV",
                    help="copy a done wav into an engine assets dir + manifest")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--prompt", default=None, help="(with --make) sfx/music conditioning text")
    ap.add_argument("--text", default=None, help="(with --make --kind voice) the line to speak")
    ap.add_argument("--kind", default="sfx", choices=list(KINDS))
    ap.add_argument("--seconds", type=float, default=None,
                    help="clip length; caps sfx 10 / music 30 (voice 15)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=None, help="(with --make) also write the payload JSON here")
    ap.add_argument("--assets-dir", default=None, help="(with --resolve)")
    ap.add_argument("--name", default=None, help="(with --resolve) override the asset name")

    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.name:
        return _cmd_make(a)
    if a.wav:
        if not a.assets_dir:
            ap.error("--resolve needs --assets-dir")
        return _cmd_resolve(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
