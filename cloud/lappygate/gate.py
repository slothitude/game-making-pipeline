#!/usr/bin/env python3
"""lappy-gate — Telegram-verified access to Lappy's public services.

nginx (the gate proxy) sends auth_request to /__verify; unauthenticated users
get bounced to /__login (a Telegram Login Widget page). The widget callback
lands on /__auth with Telegram's signed auth_data; we verify the standard
Telegram HMAC (key = sha256(bot_token)) with our own double-HMAC session
cookie (key = GATE_SECRET). Cookie domain is the funnel host, so one login
unlocks every service port.

Env: GATE_SECRET (hex), TG_BOT_TOKEN, TG_BOT_USERNAME, COOKIE_HOST.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_raw_secret = os.environ.get("GATE_SECRET", "")
GATE_SECRET = (bytes.fromhex(_raw_secret) if _raw_secret
               else b"").lstrip(b"\x00") or _raw_secret.encode()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_BOT_USERNAME = os.environ.get("TG_BOT_USERNAME", "Slothitude_bot")
COOKIE_HOST = os.environ.get("COOKIE_HOST", "lappy-ubuntu.tail7c489f.ts.net")
COOKIE = "lappy_gate"
TTL = 12 * 3600  # 12h sessions


def _sign(payload: str) -> str:
    return hmac.new(GATE_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def _mint(uid: str) -> str:
    exp = int(time.time()) + TTL
    payload = f"{uid}:{exp}"
    return f"{payload}:{_sign(payload)}"


def _session_ok(token: str) -> bool:
    try:
        uid, exp, sig = token.rsplit(":", 2)
        payload = f"{uid}:{exp}"
        if not hmac.compare_digest(sig, _sign(payload)):
            return False
        return int(exp) > time.time()
    except (ValueError, TypeError):
        return False


def _verify_telegram(q: dict) -> dict | None:
    """Standard Telegram Login Widget verification (key = sha256(bot_token))."""
    data = {k: v[0] for k, v in q.items()
            if k not in ("hash", "back") and v}
    got = (q.get("hash") or [""])[0]
    if not got or "auth_date" not in data:
        return None
    if time.time() - int(data.get("auth_date", 0)) > 300:
        return None
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    key = hashlib.sha256(TG_BOT_TOKEN.encode()).digest()
    want = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(got, want):
        return None
    return data


class Gate(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[gate] {self.client_address[0]} {fmt % args}", flush=True)

    def _reply(self, code, body=b"", ctype="text/plain", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)

        if parsed.path == "/__verify":          # forward_auth target
            if _session_ok(self._cookie_token()):
                self._reply(200, b"ok")
            else:
                # 302 through forward_auth bounces browsers to the widget
                self._reply(302, b"", headers=[("Location", "/__login")])
            return

        if parsed.path == "/__login":
            back = (q.get("back") or ["/"])[0]
            page = LOGIN_PAGE.format(
                bot=TG_BOT_USERNAME, host=COOKIE_HOST,
                back=html.escape(back, quote=True))
            self._reply(200, page.encode(), "text/html; charset=utf-8")
            return

        if parsed.path == "/__auth":             # Telegram widget callback
            user = _verify_telegram(q)
            back = (q.get("back") or ["/"])[0]
            if user is None:
                self._reply(403, b"Telegram verification failed")
                return
            uid = str(user.get("id") or user.get("username") or "tg")
            token = _mint(uid)
            cookie = (f"{COOKIE}={token}; Domain={COOKIE_HOST}; "
                      f"Path=/; Max-Age={TTL}; Secure; HttpOnly; SameSite=Lax")
            dest = back if back.startswith("https://") else f"https://{COOKIE_HOST}{back}"
            self._reply(302, b"", headers=[
                ("Set-Cookie", cookie), ("Location", dest)])
            print(f"[gate] GRANTED uid={uid} -> {dest[:80]}", flush=True)
            return

        if parsed.path == "/__whoami":
            tok = self._cookie_token()
            if _session_ok(tok):
                uid, exp, _ = tok.rsplit(":", 2)
                self._reply(200, json.dumps(
                    {"uid": uid, "expires": int(exp)}).encode(),
                    "application/json")
            else:
                self._reply(401, b"{}")
            return

        self._reply(404, b"not found")


LOGIN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lappy Gate — Telegram Verification</title>
<style>
body{{background:radial-gradient(800px 600px at 50% -10%, #16244a, #070c1c);
color:#e6ecfb;font-family:Verdana,sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;text-align:center}}
.box{{max-width:420px;padding:40px 32px;background:#101a30;border:2px solid
#283a66;border-radius:14px}}h1{{font-size:20px;color:#ffd76e}}
p{{color:#93a1c4;font-size:14px;margin:12px 0 24px}}
</style></head><body><div class="box">
<h1>&#128274; Lappy Gate</h1>
<p>Sign in with Telegram to reach the Slothitude satellite services.<br>
One sign-in unlocks every service for 12 hours.</p>
<script async src="https://telegram.org/js/telegram-widget.js?22"
  data-telegram-login="{bot}"
  data-size="large"
  data-auth-url="https://{host}/__auth"
  data-request-access="write"></script>
<p style="font-size:11px;margin-top:24px">back to <span style="color:#6fd3e8">{back}</span></p>
</div></body></html>"""


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9010)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    if not GATE_SECRET or not TG_BOT_TOKEN:
        raise SystemExit("GATE_SECRET and TG_BOT_TOKEN env required")
    ThreadingHTTPServer((a.host, a.port), Gate).serve_forever()


if __name__ == "__main__":
    main()
