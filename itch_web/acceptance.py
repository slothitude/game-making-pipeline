#!/usr/bin/env python
"""Acceptance run for itch_web: create the five draft pages, set HTML kind,
post each game's first devlog from its repo DIARY.md, verify the pages.

Idempotent: projects that already exist (matched by slug) are reused, so a
failed run can simply be re-run. Nothing is published -- the games stay drafts.

    python acceptance.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from itch import ItchWeb, ItchError  # noqa: E402

PIPELINE_FOOTER = (
    "Built with the Game Making Pipeline (AI-assisted engineering & art, "
    "human-directed). Devlog: https://slothitude.itch.io/game-making-pipeline-diary"
)

# title, slug, tagline, diary repo
PROJECTS = [
    ("SONAR", "sonar",
     "A submarine in black water that sees only by pinging. It came from below.",
     r"C:\Users\aaron\sonar"),
    ("SLIME LINE", "slime-line",
     "A tilt-glide slug, a glowing slime trail, letter-bugs, words to spell.",
     r"C:\Users\aaron\slime-line"),
    ("STAR VISITOR", "star-visitor",
     "Ride enemies, burrow the field, spell with style — a run-and-gun systems study.",
     r"C:\Users\aaron\star-visitor"),
    ("GYRO SQUADRON '45", "gyro-squadron-45",
     "Tilt-steered shooter: WWII propellers vs the weather that shoots back.",
     r"C:\Users\aaron\gyro-squadron-45"),
    ("Octogram Arcade", "octogram-arcade",
     "Three word games in one: Word Poker arena, spell-casting campaign, Eight Letters.",
     r"C:\Users\aaron\octogram-arcade"),
]


def latest_diary_entry(repo: Path) -> tuple[str, str, str | None]:
    """Return (entry_title, body, image_path|None) of the last '## ' entry."""
    text = (repo / "DIARY.md").read_text(encoding="utf-8")
    blocks = re.split(r"^## ", text, flags=re.M)[1:]  # drop the header before entry 1
    if not blocks:
        raise ItchError(f"no '## ' entries in {repo / 'DIARY.md'}")
    last = blocks[-1].strip("\n")
    lines = last.splitlines()
    heading = lines[0].strip()            # e.g. "2026-09-22 13:55 — Milestone 2 — the deep is populated"
    entry_title = re.sub(r"^\d{4}-\d{2}-\d{2}\s+[\d:]+\s+—\s+", "", heading)
    body = "\n".join(lines[1:]).strip()
    image = None
    m = re.search(r"!\[[^\]]*\]\(([^)]+)\)", body)
    if m:
        candidate = repo / m.group(1)
        if candidate.exists():
            image = str(candidate)
        body = (body[:m.start()] + body[m.end():]).strip()
    return entry_title, body, image


def main() -> int:
    web = ItchWeb()
    results: list[dict] = []
    try:
        web.start()
        reused = web.login()
        print(f"session: {'reused' if web.session_reused else 'fresh login'} (ok={reused})")

        existing = {row["url_slug"]: row["id"] for row in web.list_projects()}
        print(f"existing projects: {len(existing)}")

        for title, slug, tagline, repo in PROJECTS:
            print(f"\n=== {title} (/{slug}) ===")
            game_id = existing.get(slug)
            if game_id:
                print(f"  already exists -> id {game_id} (reusing)")
            else:
                out = web.create_project(title, slug, tagline, f"{tagline}\n\n{PIPELINE_FOOTER}")
                game_id = out["id"]
                print(f"  created -> id {game_id}  edit: {out['edit_url']}")
            kind = web.set_kind_html(game_id)
            print(f"  kind: {kind['kind']}")
            entry_title, body, image = latest_diary_entry(Path(repo))
            devlog_title = f"{title} devlog #1 — {entry_title}"
            post = web.post_devlog(
                game_id, devlog_title,
                "From the project diary:\n\n" + body,
                image_path=image,
            )
            verb = "skipped (already posted)" if post.get("skipped") else "posted"
            print(f"  devlog {verb}: {post['title']!r} image={image is not None}")
            check = web.check_page(slug)
            print(f"  page check: HTTP {check['status']} -> {check['landed']} ({check['title']!r})")
            results.append({"title": title, "slug": slug, "id": game_id, "check": check})

        print("\n=== webs (rename candidate for 'The Game Making Pipeline') ===")
        for row in web.list_projects():
            if row["url_slug"] == "webs" or row["title"].lower() == "webs":
                print(f"  webs edit-id: {row['id']}  title: {row['title']!r}")

        print("\n=== THE FIVE PAGES ===")
        for r in results:
            print(f"{r['title']:<20} https://slothitude.itch.io/{r['slug']}  (id {r['id']}, HTTP {r['check']['status']})")
        return 0
    except ItchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        web.close()


if __name__ == "__main__":
    sys.exit(main())
