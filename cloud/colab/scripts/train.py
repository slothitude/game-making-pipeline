"""train.py — the gpu.train runner. EXECUTES ON THE COLAB RUNTIME (Linux, pip, torch).

Contract with cloud/colab/colab_worker.py (the only thing that ships it here):
  - the worker uploaded the job inputs as in.zip into the remote base dir
  - this script extracts to work/, reads work/job.json, does the training,
    writes everything to out/ + result.json, then zips out/ -> out.zip
  - the worker downloads out.zip + result.json and tears the runtime down.

STATUS OF THE CODE
  - the training loop is REAL (torch, next-token cross-entropy, Adam, epochs)
    but the model is a deliberately tiny transformer (jevlike tiny-nets law:
    small enough that a free T4 finishes it in seconds/minutes, not hours).
  - STUB SCALE: this is the trainer *stub*. The proven per-game jevlike
    trainer plugs in at PROVEN_PLUG_IN below without touching the loop shell.

Local sanity run (off Colab, torch required):
  python scripts/train.py --base ./selftest_base
"""
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

BASE = Path(os.environ.get("GMP_COLAB_BASE", "/content/gmp_job"))


def log(msg):
    print(f"[train] {msg}", flush=True)


def _ensure_torch():
    try:
        import torch  # noqa: F401
        return
    except ImportError:
        log("torch missing on this runtime -> pip install (Colab preinstalls torch; "
            "this path only fires off-Colab)")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "torch"],
                       check=True)


def _locate_base() -> Path:
    """in.zip lives at /content/gmp_job on a real runtime; fall back to cwd
    so the same script can be smoke-tested locally or under a CLI shim."""
    for cand in (BASE, Path.cwd()):
        if (cand / "in.zip").exists():
            return cand
    raise FileNotFoundError(f"in.zip not found under {BASE} or {Path.cwd()}")


def _load_work(base: Path) -> dict:
    work = base / "work"
    if work.exists():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    with zipfile.ZipFile(base / "in.zip") as z:
        z.extractall(work)
    job = {}
    jf = work / "job.json"
    if jf.exists():
        job = json.loads(jf.read_text(encoding="utf-8"))
    return job


def _iter_jsonl(work: Path, cfg: dict):
    """Yield text documents from work/data/*.jsonl (or any *.jsonl in work/)."""
    data_dir = work / "data"
    paths = sorted(data_dir.glob("*.jsonl")) if data_dir.exists() else []
    if not paths:
        paths = sorted(work.glob("*.jsonl"))
    if not paths:
        raise FileNotFoundError("no *.jsonl training data found in the staging zip")
    for p in paths:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                # accept {"text": ...} rows or bare strings
                yield row["text"] if isinstance(row, dict) else str(row)


# ---------------------------------------------------------------------------
# PROVEN PLUG-IN (jevlike): swap _build_model/_train_batch for the proven
# per-game jevlike trainer here. The worker contract (in.zip -> out/model.pt
# -> result.json) does not change. Nothing below this line needs editing.
# ---------------------------------------------------------------------------
def _build_model(vocab_size, cfg, torch):
    d = int(cfg.get("d_model", 64))
    nl = int(cfg.get("n_layers", 2))
    nh = int(cfg.get("n_heads", 4))
    block = int(cfg.get("block", 64))
    enc = torch.nn.TransformerEncoderLayer(d_model=d, nhead=nh, dim_feedforward=4 * d,
                                           batch_first=True, norm_first=True)
    return torch.nn.ModuleDict({
        "tok": torch.nn.Embedding(vocab_size, d),
        "pos": torch.nn.Embedding(block, d),
        "enc": torch.nn.TransformerEncoder(enc, num_layers=nl),
        "head": torch.nn.Linear(d, vocab_size),
    })


def _forward(model, idx, torch):
    cfg_block = model["pos"].num_embeddings
    idx = idx[:, -cfg_block:]
    pos = torch.arange(idx.shape[1], device=idx.device).unsqueeze(0)
    h = model["tok"](idx) + model["pos"](pos)
    h = model["enc"](h)
    return model["head"](h)


def main():
    t0 = time.time()
    _ensure_torch()
    import torch

    base = _locate_base()
    job = _load_work(base)
    cfg = job.get("payload", {}).get("config") or {}
    cfile = base / "work" / "config.json"
    if cfile.exists():
        cfg = {**json.loads(cfile.read_text(encoding="utf-8")), **cfg}

    docs = list(_iter_jsonl(base / "work", cfg))
    epochs = int(cfg.get("epochs", 3))
    log(f"job={job.get('type')} docs={len(docs)} epochs={epochs}")

    # --- char-level vocab (tiny-nets law: no tokenizer dependency) ---
    text = "\n".join(docs)
    chars = sorted(set(text))
    stoi = {c: i for i, c in enumerate(chars)}
    ids = torch.tensor([stoi[c] for c in text], dtype=torch.long)
    block = int(cfg.get("block", 64))
    if ids.numel() < block + 1:
        ids = ids.repeat((block + 1) // max(ids.numel(), 1) + 1)

    model = _build_model(len(chars), cfg, torch)
    opt = torch.optim.AdamW(model.parameters(), lr=float(cfg.get("lr", 3e-4)))
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)
    if dev == "cuda":
        log(f"GPU: {torch.cuda.get_device_name(0)}")

    final_loss = None
    tokens_seen = 0
    for ep in range(epochs):
        # one random window per step, cfg.steps_per_epoch windows per epoch
        for _ in range(int(cfg.get("steps_per_epoch", 50))):
            i = torch.randint(0, ids.numel() - block - 1, (1,)).item()
            x = ids[i:i + block].unsqueeze(0).to(dev)
            y = ids[i + 1:i + block + 1].unsqueeze(0).to(dev)
            logits = _forward(model, x, torch)
            loss = torch.nn.functional.cross_entropy(
                logits.reshape(-1, logits.shape[-1]), y.reshape(-1))
            opt.zero_grad()
            loss.backward()
            opt.step()
            tokens_seen += block
            final_loss = float(loss.detach())
        log(f"epoch {ep + 1}/{epochs} loss={final_loss:.4f}")

    out = base / "out"
    out.mkdir(parents=True, exist_ok=True)
    torch.save({"model_state": model.state_dict(), "stoi": stoi,
                "config": cfg, "vocab_size": len(chars)}, out / "model.pt")

    result = {
        "ok": True,
        "type": "gpu.train",
        "stub": "tiny-transformer (real loop, stub scale)",
        "real_trainer_hook": "PROVEN_PLUG-IN comment in scripts/train.py",
        "device": dev,
        "docs": len(docs),
        "vocab_size": len(chars),
        "epochs": epochs,
        "tokens_seen": tokens_seen,
        "final_loss": final_loss,
        "seconds": round(time.time() - t0, 2),
    }
    (base / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    # worker downloads exactly one artifact: zip out/ -> out.zip
    with zipfile.ZipFile(base / "out.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out).as_posix())
    log(f"done: {result['seconds']}s loss={final_loss}")


if __name__ == "__main__":
    main()
