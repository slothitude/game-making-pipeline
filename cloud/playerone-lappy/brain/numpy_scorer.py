"""Pure-numpy TinyScorer inference — the PlayerOne brain without torch.

Mirrors OptionScorer.probabilities() from play.py bit-for-bit (same byte
collation, same attention head, same masking) so checkpoints exported with
export_numpy.py score identically on the 956MB retromonkey box.

    from numpy_scorer import NumpyScorer
    scorer = NumpyScorer("playerone-sonar.npz")
    probs = scorer.probabilities("depth 40m | air 12s", ["drag left", "ping", "wait"])
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

WAIT_OPTION = "wait"
F32_MIN = np.finfo(np.float32).min
LN_EPS = 1e-5


def _bytes(text: str, length: int) -> list[int]:
    return [byte + 1 for byte in text.encode("utf-8", errors="replace")[:length]]


def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = x - x.max(axis=axis, keepdims=True)
    exps = np.exp(shifted)
    return exps / exps.sum(axis=axis, keepdims=True)


def _layer_norm(x: np.ndarray, weight: np.ndarray, bias: np.ndarray) -> np.ndarray:
    mean = x.mean(axis=-1, keepdims=True)
    var = x.var(axis=-1, keepdims=True)
    return (x - mean) / np.sqrt(var + LN_EPS) * weight + bias


class NumpyScorer:
    def __init__(self, npz_path: str | Path) -> None:
        npz_path = Path(npz_path)
        self.weights = dict(np.load(npz_path))
        config_path = npz_path.with_suffix(".json")
        self.config = (json.loads(config_path.read_text(encoding="utf-8"))
                       if config_path.exists() else {})
        self.width = self.weights["embedding.weight"].shape[1]
        self.rank = self.weights["head.query.weight"].shape[0]
        self.context_tokens = self.config.get("context_tokens", 192)
        self.option_tokens = self.config.get("option_tokens", 32)

    def logits(self, context: str, options: list[str]) -> np.ndarray:
        w = self.weights
        emb = w["embedding.weight"]          # (257, W)
        pos = w["position.weight"]           # (ctx_tokens, W)

        ctx_ids = _bytes(context, self.context_tokens)
        ctx_mask = np.ones(len(ctx_ids), dtype=bool)
        context_vec = emb[np.asarray(ctx_ids)] + pos[:len(ctx_ids)]   # (L, W)

        rows = [_bytes(option, self.option_tokens) for option in options]
        max_t = max(len(row) for row in rows)
        opt_ids = np.zeros((len(rows), max_t), dtype=np.int64)
        for i, row in enumerate(rows):
            opt_ids[i, :len(row)] = row
        tok_mask = opt_ids != 0                                   # (N, T)
        token_vec = emb[opt_ids]                                  # (N, T, W)
        weights = tok_mask[..., None].astype(np.float32)
        options_vec = ((token_vec * weights).sum(axis=1)
                       / np.maximum(weights.sum(axis=1), 1.0))     # (N, W)

        c = _layer_norm(context_vec, w["head.context_norm.weight"],
                        w["head.context_norm.bias"])
        o = _layer_norm(options_vec, w["head.option_norm.weight"],
                        w["head.option_norm.bias"])
        q = o @ w["head.query.weight"].T                          # (N, R)
        k = c @ w["head.key.weight"].T                            # (L, R)
        v = c @ w["head.value.weight"].T                          # (L, R)

        scale = np.sqrt(self.rank, dtype=np.float32)
        scores = (q @ k.T) / scale                                # (N, L)
        scores[:, ~ctx_mask] = F32_MIN
        attended = _softmax(scores, axis=-1) @ v                  # (N, R)
        return (q * attended).sum(axis=-1) / scale                # (N,)

    def probabilities(self, context: str, options: list[str]) -> list[float]:
        padded = [*options, WAIT_OPTION] if len(options) < 2 else options
        logits = self.logits(context, padded)[:len(options)]
        return _softmax(logits).tolist()
