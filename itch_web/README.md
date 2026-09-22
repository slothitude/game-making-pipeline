# itch_web

A small Playwright-based API for itch.io project management — the Game Making
Pipeline's way to do programmatically what itch's REST API refuses: **create
project pages**, set the project kind, edit descriptions, post devlogs, and
publish/unpublish. It drives the itch.io website itself, logged in as the
owner's own account (`slothitude`).

## Files

| file | role |
|------|------|
| `itch.py` | the API (`class ItchWeb`) + CLI |
| `acceptance.py` | acceptance run: creates the five Pipeline draft pages, sets HTML kind, posts each game's first devlog from its repo `DIARY.md`, verifies the pages |
| `session/secret.json` | owner credentials (**gitignored — never commit**) |
| `session/state.json` | saved browser storage state (cookies) so later runs skip login (**gitignored**) |
| `session/chrome_profile/` | the bridge Chrome's dedicated profile (**gitignored**) |
| `debug/` | DOM dumps from the debug law (**gitignored**) |

## Install

```
C:/Python313/python.exe -m pip install playwright
C:/Python313/python.exe -m playwright install chromium
```

Real **Google Chrome** must also be installed (it is on Rog) — see the session
model below. `ITCH_WEB_CHROME=/path/to/chrome.exe` overrides the lookup.

## Session model

itch.io sits behind Cloudflare. Any browser that Playwright *launches itself*
(chromium, chrome channel, firefox; headless or not) is detected and the
"Just a moment" challenge loops forever — the tell is the automation
protocol's `Runtime.enable`, not headlessness. A **plain real Chrome with no
automation attached clears the challenge on its own**, and the resulting
`cf_clearance` cookie keeps working afterwards.

So `ItchWeb.start()`:

1. subprocess-launches the installed real Chrome with `--remote-debugging-port`
   and a dedicated profile (`session/chrome_profile`), pointed at
   `https://itch.io/login` — a visible window on the desktop;
2. polls the DevTools `/json` endpoint (plain HTTP, no attach) until a *real*
   page title is showing — i.e. the challenge fully passed and clearance was
   minted;
3. attaches Playwright via `connect_over_cdp` and drives the forms from there.

Login (`itch.py login`) fills email/password, verifies the Creator Dashboard
loads, and saves the storage state to `session/state.json`. Later runs
re-inject those cookies and **skip the form login entirely** when the session
is still live (they still pass through the bridge, because Cloudflare cares
about the browser, not the itch session). The bridge Chrome's own profile also
keeps the itch session alive across runs.

The Chrome window appears on screen while the tool works. That is the honest
price of the bridge; do not run this headless.

## CLI

```
python itch.py login                       # establish/refresh the session
python itch.py list                        # dashboard -> titles + edit ids
python itch.py create --title "SONAR" --slug sonar --tagline "..." [--desc-file d.md]
python itch.py set-kind --id 12345         # Kind of project -> HTML (plays in browser)
python itch.py description --id 12345 --file page.md
python itch.py devlog --id 12345 --title "..." --file post.md [--image cover.png] [--no-publish]
python itch.py publish --id 12345          # NOT used by the acceptance run
python itch.py unpublish --id 12345
python itch.py check --slug sonar          # HTTP status of slothitude.itch.io/<slug>
```

All commands open the bridge, do the work, save state, and close Chrome.

## API

```python
from itch import ItchWeb

web = ItchWeb()
try:
    web.start()
    web.login()                 # reuse session/state.json when still valid
    out = web.create_project("SONAR", "sonar", tagline, description_md)
    web.set_kind_html(out["id"])
    web.edit_description(out["id"], markdown)
    web.post_devlog(out["id"], "SONAR devlog #1 — ...", body_md, image_path=None)
    web.publish(out["id"])      # explicit only; the acceptance run never calls this
    web.unpublish(out["id"])
    web.list_projects()         # [{"id": ..., "title": ..., "url_slug": ..., ...}]
    web.check_page("sonar")     # {"status": 200, ...} from the logged-in context
finally:
    web.close()
```

## Acceptance run

```
python acceptance.py
```

Creates the five Pipeline pages (SONAR, SLIME LINE, STAR VISITOR, GYRO
SQUADRON '45, Octogram Arcade) as **drafts**, switches each to HTML kind,
posts each game's first devlog built from the latest `##` entry in that
repo's `DIARY.md` (image attached as cover when present), and verifies every
page answers from the logged-in context. Idempotent: existing slugs are
reused, so a failed run can simply be re-run.

## Ethics note

This tool automates **the owner's own account, on the owner's explicit
direction**, for a small, finite, curated set of pages: the Pipeline's five
games plus future hand-picked projects. It is not a mass-generation tool and
must not become one — it mirrors itch/leafo's stance that the site is for
hand-made pages: we automate the *mechanical* form-filling of pages whose
games are genuinely ours, each disclosed as AI-assisted (the create flow
answers itch's Generative AI disclosure honestly with "Yes"). No spam, no
bulk registration, no other accounts, no CAPTCHA/circumvention services —
the Cloudflare bridge works by using a real browser the way its owner would,
not by defeating the check.

Publishing is always an explicit human decision: `publish()` exists for
completeness but the acceptance run leaves every page a draft for the owner
to review and publish.
