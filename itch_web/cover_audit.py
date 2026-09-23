#!/usr/bin/env python
"""cover_audit -- read-only: which itch projects have a cover image?

Discovered widget (itch_web/debug/cover_audit_edit_*.html):
  cover SET   -> .game_edit_cover_uploader_widget img.cover_thumb[src*='img.itch.zone']
  cover UNSET -> .file_tools with input[name='game[cover_image_id]'] (empty)
                 + button "Upload Cover Image"
Also reads the public page's og:image meta as an independent confirmation.
Changes nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from itch import ItchWeb

OUT_PATH = Path(__file__).resolve().parent / "cover_audit.json"

EDIT_CHECK_JS = """() => {
  const w = document.querySelector('.game_edit_cover_uploader_widget');
  if (!w) return {error: 'no cover uploader widget'};
  const thumb = w.querySelector('img.cover_thumb');
  const hidden = w.querySelector("input[name='game[cover_image_id]']");
  const btn = w.querySelector('button');
  return {
    thumb_src: thumb ? thumb.src : null,
    hidden_id: hidden ? hidden.value : null,
    button_text: btn ? (btn.innerText || '').trim() : null,
  };
}"""


def main() -> int:
    web = ItchWeb()
    results = []
    try:
        web.ensure_session()
        for row in web.list_projects():
            gid, slug = row["id"], row["url_slug"]
            page = web.page
            entry = {"id": gid, "title": row["title"], "slug": slug,
                     "status": row["published"]}
            try:
                web._goto_edit(page, gid)
                entry["edit"] = page.evaluate(EDIT_CHECK_JS)
                entry["edit"]["cover_set"] = bool(
                    entry["edit"].get("thumb_src")
                    or entry["edit"].get("hidden_id")
                )
                # independent confirmation from the public page's og:image
                if slug:
                    pub = web.goto(f"{web.games_base}/{slug}")
                    entry["public_status"] = pub.status if pub else None
                    entry["og_image"] = page.evaluate(
                        """() => {
                             const m = document.querySelector("meta[property='og:image']");
                             return m ? m.content : null;
                           }"""
                    )
            except Exception as exc:  # noqa: BLE001 - report, don't die
                entry["error"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)
            print(f"{gid} {slug}: "
                  f"cover_set={entry.get('edit', {}).get('cover_set')} "
                  f"og={str(entry.get('og_image'))[:60]}", flush=True)
        OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"wrote {OUT_PATH}")
        return 0
    finally:
        web.close()


if __name__ == "__main__":
    sys.exit(main())
