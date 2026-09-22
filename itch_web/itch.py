#!/usr/bin/env python
"""itch_web.itch -- a small Playwright API for itch.io project management.

Drives https://itch.io against the owner's OWN account. See README.md for the
session model and the ethics note (finite, curated use -- not a mass tool).

TRANSPORT (important): itch.io sits behind Cloudflare, which detects and
loops the challenge forever on any Playwright-attached browser launched BY
playwright (chromium/chrome/firefox, headless or not) -- all of them call
CDP ``Runtime.enable``, which is the tell. A *plain* real Chrome (launched by
us but untouched by automation) clears the challenge on its own, and the
resulting ``cf_clearance`` cookie keeps working after we attach. So:

    1. subprocess-launch the installed real Chrome with --remote-debugging-port
       and a dedicated profile (itch_web/session/chrome_profile), pointed at
       itch.io/login.
    2. poll the DevTools /json endpoint until the challenge title is gone.
    3. playwright.chromium.connect_over_cdp() and drive the form from there.

Session cookies persist in itch_web/session/state.json and are re-injected on
later runs so login is skipped while the itch session is still valid.

Usage (CLI):
    python itch.py login
    python itch.py create --title "SONAR" --slug sonar --tagline "..." [--desc-file d.md]
    python itch.py set-kind --id 12345          # kind -> HTML (plays in browser)
    python itch.py description --id 12345 --file d.md
    python itch.py publish --id 12345 / unpublish --id 12345
    python itch.py list
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

ROOT = Path(__file__).resolve().parent
SESSION_DIR = ROOT / "session"
DEBUG_DIR = ROOT / "debug"
STATE_PATH = SESSION_DIR / "state.json"
SECRET_PATH = SESSION_DIR / "secret.json"
CHROME_PROFILE = SESSION_DIR / "chrome_profile"

DEFAULT_BASE = "https://itch.io"
CF_TIMEOUT = 20.0          # seconds to wait out a Cloudflare "Just a moment" on a page
BRIDGE_TIMEOUT = 60.0      # seconds for plain Chrome to clear CF before we attach
DEFAULT_TIMEOUT = 30_000   # ms

CHROME_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
]


class ItchError(RuntimeError):
    """Raised on any itch-web failure. Message should be human-actionable."""


class ItchWeb:
    """Playwright driver for the owner's itch.io account (via a real-Chrome bridge)."""

    def __init__(self, headless: bool = True):
        # `headless` is retained for API compat but unused: the CF bridge needs a
        # real headed Chrome window (see module docstring).
        if not SECRET_PATH.exists():
            raise ItchError(
                f"missing {SECRET_PATH} -- create it (gitignored) with "
                '{"email":"...","password":"...","username":"..."}'
            )
        self.secret: dict[str, Any] = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
        self.base = str(self.secret.get("base_url", DEFAULT_BASE)).rstrip("/")
        user = str(self.secret.get("username", ""))
        self.games_base = str(self.secret.get("games_url", f"https://{user}.itch.io")).rstrip("/")
        self.user = user
        self.session_reused = False
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._chrome: subprocess.Popen | None = None
        self._port: int | None = None
        DEBUG_DIR.mkdir(exist_ok=True)
        SESSION_DIR.mkdir(exist_ok=True)

    # ------------------------------------------------------------------ plumbing

    @property
    def page(self) -> Page:
        if self._page is None:
            self.start()
        assert self._page is not None
        return self._page

    @staticmethod
    def _chrome_exe() -> str:
        override = os.environ.get("ITCH_WEB_CHROME")
        if override:
            if Path(override).exists():
                return override
            raise ItchError(f"ITCH_WEB_CHROME={override} does not exist")
        for cand in CHROME_CANDIDATES:
            if cand and Path(cand).exists():
                return cand
        raise ItchError(
            "no Chrome/Edge found in the standard install locations "
            "(set ITCH_WEB_CHROME=/path/to/chrome.exe)"
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _http_ok(self, port: int, path: str = "/json/version") -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def _cf_cleared_via_json(self, port: int) -> bool:
        """True only when a page target shows a REAL rendered page (challenge fully
        passed, cf_clearance minted) -- not merely the absence of the challenge title,
        which can race with the challenge spinning up."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
                targets = json.loads(r.read().decode("utf-8"))
        except Exception:
            return False
        pages = [t for t in targets if t.get("type") == "page"]
        for t in pages:
            title = (t.get("title") or "").lower()
            if not title or "just a moment" in title or "attention required" in title:
                return False
            if "itch" not in title:  # e.g. still about:blank
                return False
        return bool(pages)

    def start(self) -> None:
        """Launch the real-Chrome bridge, let it clear Cloudflare, attach Playwright."""
        if self._context is not None:
            return
        exe = self._chrome_exe()
        self._pw = sync_playwright().start()
        port = self._free_port()
        self._port = port
        CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
        cmd = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={CHROME_PROFILE}",
            "--no-first-run", "--no-default-browser-check",
            "--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
            "--window-size=1200,850", "--window-position=60,60",
            self.base + "/login",   # plain Chrome clears the CF challenge on its way there
        ]
        self._chrome = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + BRIDGE_TIMEOUT
        while time.time() < deadline:
            if self._chrome.poll() is not None:
                raise ItchError(f"bridge Chrome exited early (code {self._chrome.returncode})")
            if self._http_ok(port):
                break
            time.sleep(0.4)
        else:
            raise ItchError(f"bridge Chrome DevTools port {port} never opened within {BRIDGE_TIMEOUT}s")
        while time.time() < deadline:
            if self._chrome.poll() is not None:
                raise ItchError(f"bridge Chrome exited early (code {self._chrome.returncode})")
            if self._cf_cleared_via_json(port):
                time.sleep(2.0)  # settle, then confirm the real page is still showing
                if self._cf_cleared_via_json(port):
                    break
            time.sleep(0.5)
        else:
            self.close_chrome()
            raise ItchError(
                f"Cloudflare challenge never cleared in the plain-Chrome bridge within {BRIDGE_TIMEOUT}s"
            )
        self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = self._browser.contexts[0]
        self._context = ctx
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            cookies = [c for c in state.get("cookies", []) if not c["name"].startswith("cf_")]
            if cookies:
                ctx.add_cookies(cookies)
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._context.set_default_timeout(DEFAULT_TIMEOUT)

    def close_chrome(self) -> None:
        if self._chrome is not None and self._chrome.poll() is None:
            self._chrome.terminate()
            try:
                self._chrome.wait(timeout=10)
            except Exception:
                self._chrome.kill()

    def close(self) -> None:
        for attr in ("_page", "_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self.close_chrome()
        self._chrome = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def __enter__(self) -> "ItchWeb":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def save_state(self) -> None:
        if self._context is not None:
            self._context.storage_state(path=str(STATE_PATH))

    def dump(self, page: Page | None = None, name: str = "dump") -> Path:
        """Debug law: whenever a step fails, dump the DOM to itch_web/debug/."""
        page = page or self.page
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        html_path = DEBUG_DIR / f"{safe}.html"
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception as exc:  # page may be mid-navigation
            html_path.write_text(f"<dump failed: {exc}>", encoding="utf-8")
        summary: list[str] = [f"url: {page.url}", f"title: {page.title()!r}", ""]
        try:
            summary.append(page.evaluate(
                """() => {
                  const out = [];
                  const grab = (sel) => {
                    for (const el of document.querySelectorAll(sel)) {
                      out.push(`${sel}\\t${el.tagName.toLowerCase()}\\tname=${el.getAttribute('name') || ''}\\tid=${el.id || ''}\\ttype=${el.getAttribute('type') || ''}\\tvalue=${(el.value || '').slice(0, 60)}\\ttext=${(el.innerText || el.textContent || '').trim().slice(0, 80).replace(/\\s+/g, ' ')}`);
                    }
                  };
                  for (const sel of ['form', 'button', '.button', 'input', 'textarea', 'select', '[role=button]', '[role=textbox]', '.radio_row', '.radio_button', 'label', '.redactor', '.redactor-box', '.redactor-toolbar']) grab(sel);
                  return out.join('\\n');
                }"""
            ))
        except Exception as exc:
            summary.append(f"(dom summary failed: {exc})")
        (DEBUG_DIR / f"{safe}.dom.txt").write_text("\n".join(summary), encoding="utf-8")
        return html_path

    def first(self, selectors: list[str], page: Page | None = None) -> Locator:
        """Return the first locator matching any candidate selector."""
        page = page or self.page
        sels = ", ".join(selectors)
        loc = page.locator(sels).first
        try:
            loc.wait_for(state="attached", timeout=5_000)
        except PWTimeout:
            raise ItchError(
                f"none of these selectors matched: {selectors} "
                f"(dumped {self.dump(page, 'first_' + re.sub(r'[^A-Za-z0-9]', '_', sels)[:40])})"
            )
        return loc

    def goto(self, url: str, wait: str = "domcontentloaded") -> Any:
        """Navigate with a CF guard: if a challenge appears, wait it out once."""
        page = self.page
        resp = page.goto(url, wait_until=wait)
        if not self._cf_page(page):
            return resp
        deadline = time.time() + CF_TIMEOUT
        while time.time() < deadline and self._cf_page(page):
            time.sleep(0.5)
        if self._cf_page(page):
            raise ItchError(f"Cloudflare challenge re-appeared at {url}; re-run (dumped {self.dump(page, 'cf_midrun')})")
        return resp

    @staticmethod
    def _cf_page(page: Page) -> bool:
        try:
            title = (page.title() or "").lower()
        except Exception:
            return True
        return "just a moment" in title or "attention required" in title

    def wait_cf(self, page: Page | None = None, timeout: float = CF_TIMEOUT) -> bool:
        """Wait out a Cloudflare 'Just a moment' interstitial (up to `timeout` s)."""
        page = page or self.page
        deadline = time.time() + timeout
        cf_seen = self._cf_page(page)
        while time.time() < deadline and self._cf_page(page):
            time.sleep(0.5)
        if self._cf_page(page):
            raise ItchError(f"Cloudflare challenge did not clear within {timeout}s (at {page.url}); dumped {self.dump(page, 'cloudflare_stuck')}")
        return cf_seen

    # ------------------------------------------------------------------ session

    def _logged_in(self) -> bool:
        """Hit /dashboard: a live session stays there, a dead one bounces to /login."""
        page = self.page
        self.goto(self.base + "/dashboard")
        if "/login" in page.url or "/register" in page.url:
            return False
        try:
            # discovered: the logged-in header carries .header_buttons / .header_button
            # (e.g. .dashboard_btn) and the creator dashboard has a.nav_btn[href='/dashboard']
            page.wait_for_selector(".header_buttons, .header_button.dashboard_btn, a.nav_btn[href='/dashboard']", timeout=8_000)
            return True
        except PWTimeout:
            self.dump(page, "dashboard_unrecognised")
            return False

    def login(self, force: bool = False) -> bool:
        """Ensure a logged-in session; reuse session/state.json when still valid.

        `force` skips trusting state.json and verifies live instead. Note the bridge
        Chrome keeps its own profile (session/chrome_profile) whose cookies can hold
        a live itch session across runs -- that counts as logged in too.
        """
        page = self.page
        if self._logged_in():
            self.session_reused = True
            self.save_state()
            return True
        self.goto(self.base + "/login")
        page.wait_for_selector("input[type='password']", timeout=20_000)
        email = str(self.secret["email"])
        password = str(self.secret["password"])
        # scope to the login form: the page also has a site-search input[name=q]
        # that DOM-orders before the form and would eat a bare input[type=text] match
        user_field = self.first([
            "form input[name='username']",
            "form input[name='login[username]']",
            "form input[placeholder*='sername']",
        ])
        user_field.click()
        user_field.fill(email)
        pw_field = page.locator("input[type='password']").first
        pw_field.click()
        pw_field.fill(password)
        # the visible "Log in" submit -- page also contains a HIDDEN .submit_btn in the
        # site-search form, so role/name matching alone grabs the wrong (invisible) one
        submit = page.locator("form button", has_text=re.compile(r"^\s*Log in\s*$", re.I)).first
        if not submit.count():
            submit = self.first(["button[type='submit']:visible", ".button.login_btn"])
        submit.click()
        deadline = time.time() + 30
        while time.time() < deadline:
            if "/logged_in" not in page.url and not page.locator("form input[type='password']").count():
                break
            time.sleep(0.5)
        if not self._logged_in():
            raise ItchError(f"login failed -- still logged out; dumped {self.dump(page, 'login_failed')}")
        self.save_state()
        return True

    def ensure_session(self) -> None:
        self.start()
        try:
            self.login()
        except ItchError as exc:
            if "Cloudflare" not in str(exc):
                raise
            # one recovery pass: tear the bridge down, let a fresh plain Chrome mint a
            # new clearance, retry login
            self.close()
            self.start()
            self.login(force=True)

    # ------------------------------------------------------------------ projects

    def create_project(self, title: str, slug: str | None = None, tagline: str = "",
                       description_md: str = "") -> dict[str, Any]:
        """Create a draft project at /game/new; returns {id, url, edit_url}."""
        page = self.page
        self.goto(self.base + "/game/new")
        page.wait_for_selector("input[name='game[title]']", timeout=20_000)
        page.locator("input[name='game[title]']").first.click()
        page.locator("input[name='game[title]']").first.fill(title)
        if slug:
            slug_field = page.locator("input[name='game[url]'], input[name='game[url_slug]'], .url_slug input, input[name*='slug']").first
            try:
                slug_field.wait_for(state="visible", timeout=4_000)
                slug_field.click()
                slug_field.fill(slug)
            except PWTimeout:
                self.dump(page, "create_no_slug_field")
                raise ItchError("could not find the project-URL/slug field (dumped create_no_slug_field.html)")
        if tagline:
            tag = page.locator("input[name='game[short_text]'], textarea[name='game[short_text]']").first
            if tag.count():
                tag.click()
                tag.fill(tagline)
            else:
                self.dump(page, "create_no_tagline_field")
                raise ItchError("could not find the tagline/short-text field (dumped create_no_tagline_field.html)")
        if description_md:
            self._set_editor(page, description_md, context="create")
        save = self._save_button(page, context="create")
        save.click()
        # After save we land on /game/edit/<id> (or the project page).
        deadline = time.time() + 30
        m = None
        while time.time() < deadline:
            m = re.search(r"/game/edit/(\d+)", page.url)
            if m:
                break
            time.sleep(0.5)
        if m is None:
            self.dump(page, "create_no_redirect")
            raise ItchError(f"save did not lead to /game/edit/<id> (at {page.url})")
        game_id = int(m.group(1))
        # verify the edit page actually loads
        page.wait_for_selector("input[name='game[title]'], .game_edit_page, .edit_game_sidebar", timeout=20_000)
        url = f"{self.games_base}/{slug}" if slug else None
        return {"id": game_id, "url": url, "edit_url": f"{self.base}/game/edit/{game_id}"}

    def _save_button(self, page: Page, context: str = "edit") -> Locator:
        """The submit is the 'Save' button in the bottom meta bar."""
        candidates = [
            ".meta_row .buttons .button[type='submit']",
            ".meta_row .buttons button[type='submit']",
            ".meta_row .buttons .button",
            "button[type='submit']",
            ".button.save_btn",
        ]
        for sel in candidates:
            for i in range(page.locator(sel).count()):
                loc = page.locator(sel).nth(i)
                txt = (loc.inner_text() or "").strip().lower()
                if txt == "save":
                    return loc
        self.dump(page, f"no_save_button_{context}")
        raise ItchError(f"no 'Save' button found on {context} page (dumped no_save_button_{context}.html)")

    def _set_editor(self, page: Page, text: str, context: str = "editor") -> None:
        """Write markdown/HTML into the description editor (Redactor rich text).

        Strategy order:
          1. the redactor source-code toggle (<> button) + its textarea
          2. direct Redactor JS API (code.set)
          3. set the backing textarea + sync events
        """
        ta = page.locator("textarea[name='game[body]'], textarea.redactor_source").first
        if not ta.count():
            self.dump(page, f"editor_no_textarea_{context}")
            raise ItchError(f"no description textarea found ({context}; dumped editor_no_textarea_{context}.html)")
        # 1. source toggle
        toggle = page.locator(".redactor-toolbar a[title*='ource'], .redactor-toolbar .re-source, .redactor-box a[title*='ource'], .toolbar a[title*='Source']").first
        if toggle.count():
            try:
                toggle.click()
                page.wait_for_timeout(600)
                ta.fill(text)
                toggle.click()  # back to visual
                return
            except PWTimeout:
                pass
        # 2. Redactor JS API
        ok = page.evaluate(
            """(text) => {
                 try {
                   if (!window.jQuery || !jQuery.fn.redactor) return false;
                   const $el = jQuery('textarea[name="game[body]"]');
                   if (!$el.length) return false;
                   $el.redactor('code.set', text);
                   $el.redactor('sync');
                   return true;
                 } catch (e) { return String(e); }
               }""",
            text,
        )
        if ok is True:
            return
        # 3. raw textarea + events (redactor syncs from textarea on submit in some versions)
        page.evaluate(
            """(text) => {
                 const ta = document.querySelector('textarea[name="game[body]"]');
                 if (!ta) return;
                 ta.value = text;
                 ta.dispatchEvent(new Event('input', {bubbles: true}));
                 ta.dispatchEvent(new Event('change', {bubbles: true}));
               }""",
            text,
        )
        if ok is not False:
            self.dump(page, f"editor_js_error_{context}")

    def _goto_edit(self, page: Page, game_id: int) -> None:
        self.goto(f"{self.base}/game/edit/{game_id}")
        if "/login" in page.url:
            raise ItchError(f"session expired while opening edit page {game_id}; run `python itch.py login`")
        page.wait_for_selector("input[name='game[title]']", timeout=20_000)

    def set_kind_html(self, game_id: int) -> dict[str, Any]:
        """On /game/edit/<id>: 'Kind of project' row -> select HTML (plays in browser) -> save."""
        page = self.page
        self._goto_edit(page, game_id)
        radio = page.locator("input[name='game[kind]'][value='html']").first
        if not radio.count():
            self.dump(page, f"kind_no_radio_{game_id}")
            raise ItchError(f"no game[kind]=html radio on edit page {game_id} (dumped kind_no_radio_{game_id}.html)")
        # the input is visually hidden behind a styled label -- click the label
        label = page.locator("label:has(input[name='game[kind]'][value='html'])").first
        (label if label.count() else radio).click()
        page.wait_for_timeout(400)  # let the HTML options (viewport etc.) unfold
        self._save_button(page, context="kind").click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(800)
        self._goto_edit(page, game_id)
        checked = page.locator("input[name='game[kind]'][value='html']").first.is_checked()
        if not checked:
            self.dump(page, f"kind_not_applied_{game_id}")
            raise ItchError(f"kind=html did not stick on {game_id} (dumped kind_not_applied_{game_id}.html)")
        return {"id": game_id, "kind": "html"}

    def edit_description(self, game_id: int, markdown: str) -> dict[str, Any]:
        page = self.page
        self._goto_edit(page, game_id)
        self._set_editor(page, markdown, context=f"edit_{game_id}")
        self._save_button(page, context="description").click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(800)
        return {"id": game_id, "length": len(markdown)}

    def publish(self, game_id: int) -> dict[str, Any]:
        return self._publish_toggle(game_id, want=True)

    def unpublish(self, game_id: int) -> dict[str, Any]:
        return self._publish_toggle(game_id, want=False)

    def _publish_toggle(self, game_id: int, want: bool) -> dict[str, Any]:
        """The publish/unpublish control on the edit page (opens a confirm dialog)."""
        page = self.page
        self._goto_edit(page, game_id)
        word = "publish" if want else "unpublish"
        btn = page.locator(
            f"a.publish_game_btn, .form .button:has-text('{word.capitalize()}'), "
            f".buttons a:has-text('{word.capitalize()}'), a:has-text('{word.capitalize()}')"
        ).first
        if not btn.count():
            self.dump(page, f"publish_no_btn_{game_id}")
            raise ItchError(f"no {word} control on edit page {game_id} (dumped publish_no_btn_{game_id}.html)")
        btn.click()
        page.wait_for_timeout(600)
        # confirm inside the dialog if one opened
        confirm = page.locator(".modal, .dialog, .lightbox_wrapper, .close_button_overlay").locator(
            f"button:has-text('{word.capitalize()}'), .button:has-text('{word.capitalize()}')"
        ).first
        if confirm.count():
            confirm.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(800)
        return {"id": game_id, "published": want}

    def list_projects(self) -> list[dict[str, Any]]:
        """Parse /dashboard -> game titles + edit ids."""
        page = self.page
        self.goto(self.base + "/dashboard")
        page.wait_for_selector(".game_row, .manage_game_widget, .dashboard_game_row", timeout=20_000)
        rows = page.evaluate(
            """() => {
                 const out = [];
                 const seen = new Set();
                 for (const a of document.querySelectorAll("a[href*='/game/edit/']")) {
                   const m = a.href.match(/\\/game\\/(?:edit|url)\\/(\\d+)/);
                   if (!m) continue;
                   const row = a.closest('.game_row, .manage_game_widget, .dashboard_game_row, tr, li') || a;
                   const title = (row.querySelector('.game_title, .title, .game_name') || a).innerText.trim();
                   const key = m[1];
                   if (seen.has(key)) continue;
                   seen.add(key);
                   let pub = row.querySelector('.published_flag, .game_publish_flag, [class*=publish]');
                   out.push({id: parseInt(key, 10), title: title, url_slug: (a.pathname || '').split('/').pop(), published: pub ? pub.innerText.trim() : ''});
                 }
                 return out;
               }"""
        )
        return rows

    # ------------------------------------------------------------------ verify

    def check_page(self, slug: str) -> dict[str, Any]:
        """Visit https://<user>.itch.io/<slug> from the logged-in context."""
        page = self.page
        url = f"{self.games_base}/{slug}"
        resp = self.goto(url)
        status = resp.status if resp else None
        return {"slug": slug, "requested": url, "landed": page.url, "status": status, "title": page.title()}


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="itch.py", description="itch.io project management via Playwright")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="establish/refresh the session (session/state.json)")
    sub.add_parser("list", help="list projects on the dashboard")

    p = sub.add_parser("create", help="create a draft project")
    p.add_argument("--title", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--tagline", default="")
    p.add_argument("--desc-file", default=None, help="markdown/HTML file for the description")
    p.add_argument("--desc-text", default=None)

    p = sub.add_parser("set-kind", help="set 'Kind of project' to HTML")
    p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("description", help="set the project description")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--file", default=None)
    p.add_argument("--text", default=None)

    for name in ("publish", "unpublish"):
        p = sub.add_parser(name)
        p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("check", help="check a project page's HTTP status")
    p.add_argument("--slug", required=True)

    p = sub.add_parser("dump", help="debug: dump a page's DOM to itch_web/debug/")
    p.add_argument("--path", required=True, help="path to fetch, e.g. /game/new")
    p.add_argument("--name", default="cli_dump")

    args = ap.parse_args(argv)
    web = ItchWeb()
    try:
        if args.cmd == "login":
            web.start()
            fresh = web.login(force=True)
            print(f"login OK (fresh session, state saved to {STATE_PATH}): {fresh}")
            return 0
        web.ensure_session()
        if args.cmd == "list":
            for row in web.list_projects():
                print(f"{row['id']:>9}  {row['title']}  [{row['published']}]")
        elif args.cmd == "create":
            desc = ""
            if args.desc_file:
                desc = Path(args.desc_file).read_text(encoding="utf-8")
            elif args.desc_text:
                desc = args.desc_text
            out = web.create_project(args.title, args.slug, args.tagline, desc)
            print(json.dumps(out, indent=2))
        elif args.cmd == "set-kind":
            print(json.dumps(web.set_kind_html(args.id), indent=2))
        elif args.cmd == "description":
            if not (args.file or args.text):
                ap.error("description needs --file or --text")
            md = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
            print(json.dumps(web.edit_description(args.id, md or ""), indent=2))
        elif args.cmd in ("publish", "unpublish"):
            print(json.dumps(getattr(web, args.cmd)(args.id), indent=2))
        elif args.cmd == "check":
            print(json.dumps(web.check_page(args.slug), indent=2))
        elif args.cmd == "dump":
            web.goto(args.path if args.path.startswith("http") else web.base + args.path)
            print(f"dumped {web.dump(web.page, args.name)}")
        return 0
    except ItchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        web.close()


if __name__ == "__main__":
    sys.exit(main())
