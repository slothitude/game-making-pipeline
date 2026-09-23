#!/usr/bin/env python
"""cover_set -- upload keyart PNGs as the cover image on itch project pages.

Discovered widget (itch_web/debug/cover_audit_edit_*.html):
  cover UNSET -> .file_tools: input[name='game[cover_image_id]'] (empty)
                 + button "Upload Cover Image"
  click the button -> a lazy input[type=file] appears (same pattern as the
  devlog uploader in itch.py _attach_cover_image) -> set_input_files ->
  game[cover_image_id] fills once the upload finishes -> save.

Only two controls are ever touched: the upload button and the form's
.save_btn. No description, kind, visibility, or file changes.

Usage:
    python cover_set.py                    # run the planned table below
    python cover_set.py --id 5038940 --image ../build/keyart/octogram_arcade.png
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from itch import ItchWeb
from playwright.sync_api import TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent
KEYART = ROOT.parent / "build" / "keyart"

# (game_id, slug, keyart filename) -- from cover_audit.json: pages whose cover
# was missing AND for which an exactly-matching keyart file exists.
PLAN = [
    (5038940, "octogram-arcade", "octogram_arcade.png"),
    (5038990, "gyro-squadron-45", "gyro_squadron.png"),
]

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


def cover_state(web: ItchWeb, gid: int) -> dict:
    page = web.page
    web._goto_edit(page, gid)
    return page.evaluate(EDIT_CHECK_JS)


def set_cover(web: ItchWeb, gid: int, image: Path) -> dict:
    """Upload `image` as the cover of game `gid` and save. Idempotent."""
    page = web.page
    state = cover_state(web, gid)
    if state.get("thumb_src") or state.get("hidden_id"):
        return {"id": gid, "image": image.name, "skipped": True,
                "reason": "cover already set", "state": state}

    btn = page.locator(
        ".game_edit_cover_uploader_widget button, .cover_uploader_drop button"
    ).first
    if not btn.count():
        web.dump(page, f"cover_no_btn_{gid}")
        raise RuntimeError(f"no 'Upload Cover Image' button on edit {gid}")
    btn.click()

    # the widget lazily reveals a file input once the button is pressed
    file_input = page.locator("input[type='file']").last
    try:
        file_input.wait_for(state="attached", timeout=8_000)
    except PWTimeout:
        web.dump(page, f"cover_no_file_input_{gid}")
        raise RuntimeError(f"no file input appeared after upload click on {gid}")
    file_input.set_input_files(str(image))

    # hidden game[cover_image_id] fills when the upload completes
    deadline = time.time() + 90
    filled = None
    while time.time() < deadline:
        val = page.evaluate(
            """() => {
                 const el = document.querySelector("input[name='game[cover_image_id]']");
                 return el ? el.value : null;
               }"""
        )
        if val:
            filled = val
            break
        time.sleep(0.5)
    if not filled:
        web.dump(page, f"cover_upload_stuck_{gid}")
        raise RuntimeError(
            f"cover upload never completed on {gid} (cover_image_id empty)")

    web._save_button(page, context=f"cover_{gid}").click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)

    # verify on a fresh edit-page load
    after = cover_state(web, gid)
    ok = bool(after.get("thumb_src") or after.get("hidden_id"))
    if not ok:
        web.dump(page, f"cover_not_applied_{gid}")
        raise RuntimeError(f"cover did not stick on {gid} (state {after})")

    # independent confirmation: the public page's og:image
    row = web.check_page(after.get("slug") or "")
    og = None
    try:
        og = page.evaluate(
            """() => {
                 const m = document.querySelector("meta[property='og:image']");
                 return m ? m.content : null;
               }"""
        )
    except Exception:
        pass
    return {"id": gid, "image": image.name, "upload_id": filled,
            "thumb": after.get("thumb_src"), "public_og": og,
            "public_check": row, "verified": bool(og)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=None)
    ap.add_argument("--image", default=None)
    args = ap.parse_args()

    jobs = [(args.id, Path(args.image))] if args.id else [
        (gid, KEYART / fn) for gid, _slug, fn in PLAN
    ]
    web = ItchWeb()
    results = []
    try:
        web.ensure_session()
        for gid, image in jobs:
            if not image.exists():
                results.append({"id": gid, "image": str(image),
                                "error": "keyart file missing"})
                continue
            print(f"--- {gid} <- {image.name}", flush=True)
            try:
                out = set_cover(web, gid, image)
            except Exception as exc:  # noqa: BLE001
                out = {"id": gid, "image": image.name,
                       "error": f"{type(exc).__name__}: {exc}"}
            results.append(out)
            print(json.dumps(out, indent=2), flush=True)
        (ROOT / "cover_set_result.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8")
        return 0 if all("error" not in r for r in results) else 1
    finally:
        web.close()


if __name__ == "__main__":
    sys.exit(main())
