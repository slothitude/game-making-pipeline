#!/usr/bin/env python
"""itch_web.itch -- a small Playwright API for itch.io project management.

Drives https://itch.io with a real (headless) chromium session against the
owner's OWN account. See README.md for the session model and the ethics note
(finite, curated use -- this is not a mass-generation tool).

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
import re
import sys
import time
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

DEFAULT_BASE = "https://itch.io"
UA_STRIP = ("HeadlessChrome", "Chrome")  # UA rewrite: present as plain desktop Chrome
CF_TIMEOUT = 20.0          # seconds to wait out a Cloudflare "Just a moment"
DEFAULT_TIMEOUT = 30_000   # ms


class ItchError(RuntimeError):
    """Raised on any itch-web failure. Message should be human-actionable."""


class ItchWeb:
    """Playwright driver for the owner's itch.io account."""

    def __init__(self, headless: bool = True):
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
        self.headless = headless
        self.session_reused = False
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        DEBUG_DIR.mkdir(exist_ok=True)
        SESSION_DIR.mkdir(exist_ok=True)

    # ------------------------------------------------------------------ plumbing

    @property
    def page(self) -> Page:
        if self._page is None:
            self.start()
        assert self._page is not None
        return self._page

    def start(self) -> None:
        if self._context is not None:
            return
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        probe = self._browser.new_context()
        ua = probe.new_page().evaluate("navigator.userAgent")
        probe.close()
        # A "HeadlessChrome" UA is an instant Cloudflare flag; present as desktop Chrome.
        kwargs: dict[str, Any] = {"user_agent": ua.replace(*UA_STRIP)}
        if STATE_PATH.exists():
            kwargs["storage_state"] = str(STATE_PATH)
        self._context = self._browser.new_context(**kwargs)
        self._context.set_default_timeout(DEFAULT_TIMEOUT)
        self._page = self._context.new_page()

    def close(self) -> None:
        for attr in ("_page", "_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
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
                f"(dumped {self.dump(page, 'first_' + sels[:40])})"
            )
        return loc

    def wait_cf(self, page: Page | None = None, timeout: float = CF_TIMEOUT) -> bool:
        """Wait out a Cloudflare 'Just a moment' interstitial (up to `timeout` s)."""
        page = page or self.page
        deadline = time.time() + timeout
        cf_seen = False
        while time.time() < deadline:
            try:
                title = (page.title() or "").lower()
                cf = page.locator("#challenge-form, #challenge-running, #challenge-error-text, .cf-turnstile, #cf-chl-widget, #cf-spinner-please-wait").count()
            except Exception:
                time.sleep(0.4)
                continue
            if "just a moment" in title or "attention required" in title or cf:
                cf_seen = True
                time.sleep(0.5)
                continue
            return True
        if cf_seen:
            raise ItchError(f"Cloudflare challenge did not clear within {timeout}s (at {page.url}); dumped {self.dump(page, 'cloudflare_stuck')}")
        return False

    # ------------------------------------------------------------------ session

    def _logged_in(self) -> bool:
        """Hit /dashboard: a live session stays there, a dead one bounces to /login."""
        page = self.page
        page.goto(self.base + "/dashboard", wait_until="domcontentloaded")
        self.wait_cf(page)
        if "/login" in page.url or "/register" in page.url:
            return False
        try:
            page.wait_for_selector(".header .user_tools, #user_tools, .dashboard_header, .header .dropdown", timeout=8_000)
            return True
        except PWTimeout:
            self.dump(page, "dashboard_unrecognised")
            return False

    def login(self, force: bool = False) -> bool:
        """Ensure a logged-in session; reuse session/state.json when still valid."""
        page = self.page
        if not force and STATE_PATH.exists() and self._logged_in():
            self.session_reused = True
            return True
        page.goto(self.base + "/login", wait_until="domcontentloaded")
        self.wait_cf(page)
        page.wait_for_selector("form", timeout=20_000)
        email = str(self.secret["email"])
        password = str(self.secret["password"])
        user_field = self.first([
            "input[name='username']",
            "input[name='login[username]']",
            "input[placeholder*='sername']",
            "input[type='text']",
        ])
        user_field.fill(email)
        page.locator("input[type='password']").first.fill(password)
        try:
            page.get_by_role("button", name=re.compile(r"log\s*in", re.I)).first.click(timeout=5_000)
        except PWTimeout:
            self.first(["button[type='submit']", ".button.login_btn", "form button"]).click()
        # Wait for the post-login redirect (usually back to /dashboard or /games).
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if "/logged_in" not in page.url and not page.locator("form input[type='password']").count():
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not self._logged_in():
            raise ItchError(f"login failed -- still logged out; dumped {self.dump(page, 'login_failed')}")
        self.save_state()
        return True

    def ensure_session(self) -> None:
        self.start()
        self.login()

    # ------------------------------------------------------------------ projects

    def create_project(self, title: str, slug: str | None = None, tagline: str = "",
                       description_md: str = "") -> dict[str, Any]:
        """Create a draft project at /game/new; returns {id, url, edit_url}."""
        page = self.page
        page.goto(self.base + "/game/new", wait_until="domcontentloaded")
        self.wait_cf(page)
        page.wait_for_selector("input[name='game[title]']", timeout=20_000)
        page.locator("input[name='game[title]']").first.fill(title)
        if slug:
            slug_field = page.locator("input[name='game[url]'], input[name='game[url_slug]'], .url_slug input, input[name*='slug']").first
            try:
                slug_field.wait_for(state="visible", timeout=4_000)
                slug_field.fill(slug)
            except PWTimeout:
                self.dump(page, "create_no_slug_field")
                raise ItchError("could not find the project-URL/slug field (dumped create_no_slug_field.html)")
        if tagline:
            tag = page.locator("input[name='game[short_text]'], textarea[name='game[short_text]']").first
            if tag.count():
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
        while time.time() < deadline:
            m = re.search(r"/game/edit/(\d+)", page.url)
            if m:
                break
            time.sleep(0.5)
        else:
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
                page.wait_for_selector("textarea.redactor_source:not([style*='display: none']), textarea[name='game[body]']:visible", timeout=5_000)
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
            """([text]) => {
                 const ta = document.querySelector('textarea[name="game[body]"]');
                 if (!ta) return;
                 ta.value = text;
                 ta.dispatchEvent(new Event('input', {bubbles: true}));
                 ta.dispatchEvent(new Event('change', {bubbles: true}));
               }""",
            [text],
        )
        if ok is not False:
            self.dump(page, f"editor_js_error_{context}")

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
        btn = page.locator(f"a.publish_game_btn, .form .button:has-text('{word.capitalize()}'), .buttons a:has-text('{word.capitalize()}')").first
        if not btn.count():
            self.dump(page, f"publish_no_btn_{game_id}")
            raise ItchError(f"no {word} control on edit page {game_id} (dumped publish_no_btn_{game_id}.html)")
        btn.click()
        page.wait_for_timeout(600)
        # confirm inside the dialog if one opened
        confirm = page.locator(f".modal, .dialog, .lightbox_wrapper").locator(f"button:has-text('{word.capitalize()}'), .button:has-text('{word.capitalize()}')").first
        if confirm.count():
            confirm.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(800)
        return {"id": game_id, "published": want}

    def list_projects(self) -> list[dict[str, Any]]:
        """Parse /dashboard -> game titles + edit ids."""
        page = self.page
        page.goto(self.base + "/dashboard", wait_until="domcontentloaded")
        self.wait_cf(page)
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

    def _goto_edit(self, page: Page, game_id: int) -> None:
        page.goto(f"{self.base}/game/edit/{game_id}", wait_until="domcontentloaded")
        self.wait_cf(page)
        if "/login" in page.url:
            raise ItchError(f"session expired while opening edit page {game_id}; run `python itch.py login`")
        page.wait_for_selector("input[name='game[title]']", timeout=20_000)

    # ------------------------------------------------------------------ verify

    def check_page(self, slug: str) -> dict[str, Any]:
        """Visit https://<user>.itch.io/<slug> from the logged-in context."""
        page = self.page
        url = f"{self.games_base}/{slug}"
        resp = page.goto(url, wait_until="domcontentloaded")
        self.wait_cf(page)
        status = resp.status if resp else None
        return {"slug": slug, "url": page.url, "status": status, "title": page.title()}


# --------------------------------------------------------------------- CLI

PROJECTS_HELP = "create: writes a draft project (never published automatically)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="itch.py", description="itch.io project management via Playwright")
    ap.add_argument("--headed", action="store_true", help="run chromium headed (debug)")
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
    web = ItchWeb(headless=not args.headed)
    try:
        web.start()
        if args.cmd == "login":
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
            web.page.goto(args.path if args.path.startswith("http") else web.base + args.path)
            web.wait_cf()
            print(f"dumped {web.dump(web.page, args.name)}")
        return 0
    except ItchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        web.close()


if __name__ == "__main__":
    sys.exit(main())
