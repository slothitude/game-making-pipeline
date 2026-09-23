#!/usr/bin/env python3
"""exec_review — real player reviews land here and feed the evolve loop.

The in-game portal POSTs {game, scores{fun..adhd_friendly 1-5}, text}.
This executor appends to ~/pipeline/reviews/<game>.jsonl (the human-telemetry
corpus the monthly evolve cycle and the LLM critic both read) and says thanks
in the log. Never fails on content — a malformed review is logged and skipped.
"""
from __future__ import annotations

import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REVIEWS_ROOT = os.environ.get("GMP_REVIEWS_ROOT", "/home/ubuntu/pipeline/reviews")
DIMS = ("fun", "polish", "readability", "phone_ux", "adhd_friendly")


def run(job: dict) -> dict:
    payload = job.get("payload") or {}
    game = str(payload.get("game") or "unknown").strip()
    scores = payload.get("scores") or {}
    clean_scores = {}
    for dim in DIMS:
        try:
            val = int(scores.get(dim, 0))
            if 1 <= val <= 5:
                clean_scores[dim] = val
        except (TypeError, ValueError):
            continue
    text = str(payload.get("text") or "").strip()[:2000]
    chat = payload.get("chat") or []
    chat_clean = [{"role": str(m.get("role", "?"))[:12],
                   "content": str(m.get("content", ""))[:600]}
                  for m in chat if isinstance(m, dict)][:12]

    os.makedirs(REVIEWS_ROOT, exist_ok=True)
    path = os.path.join(REVIEWS_ROOT, f"{game}.jsonl")
    row = {"game": game, "scores": clean_scores,
           "text": text, "chat": chat_clean,
           "chat_turns": len(chat_clean),
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "job": job.get("id")}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    avg = (sum(clean_scores.values()) / len(clean_scores)
           if clean_scores else 0.0)
    print(f"[exec_review] {game}: avg {avg:.1f}/5 over {len(clean_scores)} dims "
          f"+ {len(text)} chars of player truth -> {path}", flush=True)
    return {"ok": True, "game": game, "dims_scored": len(clean_scores),
            "average": round(avg, 2), "text_chars": len(text)}
