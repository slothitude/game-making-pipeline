"""Site LLM proxy — the ONLY place browser chat touches API keys.

The site bundle used to carry the NVIDIA + OpenRouter keys in cleartext
(index.html ~2684). Now the browser POSTs /api/llm {messages} and this
service runs the ladder server-side: big-NVIDIA boss → backup → openrouter
takeover (gemini rung fires if ~/.gemini_key exists). Keys never leave env.

Laws: same-origin only · per-IP token bucket (12/min, burst 4) ·
max_tokens capped at 1200 · body capped at 64 KB · UTF-8 reconfigure.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HOST = "127.0.0.1"
PORT = 8903
NVAPI_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_EXTRA = {"HTTP-Referer": "https://retromonkey.com.au", "X-Title": "Slothitude Games"}
MAX_TOKENS = 1200
RATE_WINDOW = 60.0
RATE_BURST = 4
RATE_RATE = 12
BODY_CAP = 64 * 1024
SITE = "retromonkey.com.au"

_buckets: dict[str, list[float]] = defaultdict(list)


def _allow(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _buckets[ip] if now - t < RATE_WINDOW]
    if len(hits) >= RATE_BURST + RATE_RATE:
        _buckets[ip] = hits
        return False
    hits.append(now)
    _buckets[ip] = hits
    return True


def _ladder() -> list[dict]:
    """Boss/backup from ladder.json (curated), then the openrouter takeover."""
    rungs: list[dict] = []
    path = os.environ.get("LADDER_PATH", "")
    try:
        with open(path, "r", encoding="utf-8") as f:
            roles = json.load(f).get("roles", {})
        for name in ("boss", "backup"):
            role = roles.get(name) or {}
            if role.get("model"):
                rungs.append({"url": role.get("url", NVAPI_URL), "model": role["model"],
                              "key_env": "NVAPI_KEY"})
    except (OSError, ValueError):
        pass
    if not rungs:  # ladder.json missing/corrupt → code fallbacks
        rungs = [{"url": NVAPI_URL, "model": "z-ai/glm-5.3", "key_env": "NVAPI_KEY"},
                 {"url": NVAPI_URL, "model": "moonshotai/kimi-k3", "key_env": "NVAPI_KEY"}]
    rungs.append({"url": OR_URL, "model": "openrouter/free", "key_env": "OPENROUTER_KEY",
                  "extra": OR_EXTRA})
    return rungs


def _gemini_key() -> str:
    try:
        with open(os.path.expanduser("~/.gemini_key"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _call(url: str, payload: dict, key: str, extra: dict | None, timeout: int) -> str:
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    headers.update(extra or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    m = (body.get("choices") or [{}])[0].get("message") or {}
    txt = str(m.get("content") or m.get("reasoning_content") or "")
    txt = re.sub(r"<think>[\s\S]*?</think>", "", txt).strip()
    if not txt:
        raise RuntimeError("empty reply")
    return txt


def _playerone_context(req: dict) -> list:
    """The investigator's briefcase: real state, fetched server-side, injected
    as the opening system message when a chat carries playerone_game."""
    game = str(req.get("playerone_game") or "").strip()
    if not game:
        return []
    mode = str(req.get("playerone_mode") or "critic").strip()
    facts = []
    ev = f"/home/ubuntu/playerone/evidence/{game}/latest.json"
    try:
        with open(ev, "r", encoding="utf-8") as f:
            d = json.load(f)
        facts.append(f"last evidence run: {d.get('seconds')}s, {d.get('shots')} "
                     f"screenshots, events: "
                     f"{json.dumps(d.get('events', [])[:3])[:300]}")
    except (OSError, ValueError):
        facts.append("no playthrough evidence on file yet")
    rev = f"/home/ubuntu/pipeline/reviews/{game}.jsonl"
    try:
        with open(rev, "r", encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()][-3:]
        facts.append("recent player reviews: " + json.dumps(rows)[:600])
    except (OSError, ValueError):
        facts.append("no player reviews yet")
    if mode == "helper":
        spec_facts = []
        for rel in ("spec/study_spec.json", "spec/jam_spec.json"):
            sp = f"/home/ubuntu/games-src/{game}/{rel}"
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    spec = json.load(f)
                systems = spec.get("systems_law") or spec.get("systems") or []
                if isinstance(systems, list):
                    spec_facts = [str(s)[:220] for s in systems[:8]]
                break
            except (OSError, ValueError):
                continue
        system = (
            "You are PlayerOne, a friendly in-game COACH for the game "
            f"'{game}'. You have read the game's design spec — its systems: "
            + (" | ".join(spec_facts) if spec_facts else "general knowledge")
            + ". You also know: " + " | ".join(facts) + ". "
            "HELP THEM PLAY: answer questions about mechanics and controls, "
            "give ONE hint at a time (never spoilers or full solutions unless "
            "they explicitly ask), celebrate their progress, keep it to 2-3 "
            "short sentences with warmth. If they're stuck on something that "
            "sounds like a design flaw, say you'll note it for the crew and "
            "output a ```plan block like the critic would."
        )
        return [{"role": "system", "content": system}]
    system = (
        "You are PlayerOne, the Pipeline's critic, chatting with a player "
        f"inside the game '{game}'. You have INVESTIGATED before speaking. "
        "Facts you found: " + " | ".join(facts) + ". "
        "Be brief (2-3 sentences), curious, honest. When the conversation "
        "reveals something fixable, MAKE A PLAN: output a fenced block "
        "```plan\\n[{\"type\": \"tune_tunable|generate_art|milestone|critique\", "
        "\"payload\": {...}, \"priority\": 4}]\\n``` with 1-3 concrete jobs, then "
        "one line asking if they want it filed for the crew."
    )
    return [{"role": "system", "content": system}]


def _gemini(messages: list, key: str, max_tokens: int) -> str:
    system, contents = "", []
    for msg in messages:
        c = msg.get("content") or ""
        if msg.get("role") == "system":
            system = c
            continue
        contents.append({"role": "model" if msg.get("role") == "assistant" else "user",
                         "parts": [{"text": c}]})
    payload = {"contents": contents,
               "generationConfig": {"temperature": 0.7, "maxOutputTokens": max_tokens}}
    if system:
        payload["system_instruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent",
        data=json.dumps(payload).encode(),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    parts = (body.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    txt = "".join(p.get("text", "") for p in parts).strip()
    if not txt:
        raise RuntimeError("empty gemini reply")
    return txt


class LLMProxy(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # one clean line per request
        print(f"[llm-proxy] {self.client_address[0]} {fmt % args}", flush=True)

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/llm":
            return self._json(404, {"error": "not found"})
        origin = self.headers.get("Origin") or ""
        referer = self.headers.get("Referer") or ""
        if SITE not in origin and SITE not in referer:
            return self._json(403, {"error": "same-origin only"})
        ip = self.client_address[0]
        if not _allow(ip):
            return self._json(429, {"error": "slow down"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > BODY_CAP:
            return self._json(413, {"error": "too large"})
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        messages = _playerone_context(req) + list(req.get("messages") or [])
        if not isinstance(messages, list) or not messages:
            return self._json(400, {"error": "messages required"})
        max_tokens = min(int(req.get("max_tokens") or 900), MAX_TOKENS)
        payload = {"model": "", "messages": messages,
                   "temperature": 0.7, "max_tokens": max_tokens}

        errors = []
        for rung in _ladder():
            key = os.environ.get(rung["key_env"], "")
            if not key:
                continue
            payload["model"] = rung["model"]
            try:
                txt = _call(rung["url"], payload, key, rung.get("extra"), 30)
                return self._json(200, {"text": txt})
            except Exception as exc:  # noqa: BLE001 — ladder law: try the next rung
                errors.append(f'{rung["model"]}: {exc}')
                print(f"[llm-proxy] rung down {errors[-1]}", flush=True)
        gkey = _gemini_key()
        if gkey:
            try:
                return self._json(200, {"text": _gemini(messages, gkey, max_tokens)})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"gemini: {exc}")
        self._json(502, {"error": "ladder exhausted", "detail": errors})


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="site LLM proxy")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), LLMProxy)
    print(f"[llm-proxy] listening on {args.host}:{args.port} — keys stay server-side",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
