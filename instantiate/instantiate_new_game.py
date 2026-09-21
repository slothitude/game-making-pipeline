#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P8 -- Template instantiation front of the Game Making Pipeline.

Turns a NEW-GAME work-order (produced by the bot from "/newgame arcade <Title>")
into THAT PLAYER'S game: a branded copy of the Arcade template, gated by the
7-suite wall, with a hub_bundle/ ready for the caller (GitHub Actions workflow)
to commit into the hub Pages repo under games/<telegram_id>-<slug>/.

Usage:
    python instantiate_new_game.py --order <order.json> \
        --template-dir <arcade repo path> --out <staging dir> [--dry-run]

Layout produced:
    <out>/            staging copy of the template (rebranded, gated)
    <out>-out/        sibling outputs dir
    <out>-out/hub_bundle/   web export files + game.json  <- the deploy payload
    <out>-out/gate_report.json  per-suite results

Exit codes:
    0  success (or --dry-run success)
    1  validation / environment error (bad order, missing template, no godot)
    2  gate failure -- staging is LEFT IN PLACE for inspection

v1 template law: "arcade", "rpg" and "eight" are all accepted order values and
ALL THREE resolve to the same Arcade template. rpg/eight exist so the hub
template picker can offer them on day one; separate templates are a later
phase (swap TEMPLATE_ALIASES to point them elsewhere when they exist).

Python stdlib only. No network. Windows-safe paths.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# NAMED LAW constants (architecture law 2: every tunable is a named constant)
# ---------------------------------------------------------------------------

GODOT_BIN_DEFAULT = r"C:/Users/aaron/AppData/Local/Godot/Godot_v4.7.1-stable_win64.exe"

# Order "template" values -> actual template used. v1: rpg/eight ALIAS arcade.
TEMPLATE_ALIASES = {"arcade": "arcade", "rpg": "arcade", "eight": "arcade"}

# Top-level template entries NEVER copied into staging (.git added defensively
# so a CI checkout's history never leaks into a player's game bundle).
EXCLUDED_FROM_COPY = frozenset({
    ".godot",
    "build",
    "web_deploy",
    "cloud_editor",
    "letter_to_tash.md",
    "ARCADE_REVIEW.md",
    ".git",
})

# The 7-suite gate wall (PLAN.md: "7 suites, 654 checks"). Each is a headless
# SceneTree script at tests/<name>.gd, exit 0 = green. A missing suite is a
# HARD FAIL: the wall must be complete before anything ships.
GATE_SUITES = (
    "run_tests",
    "run_rpg_tests",
    "smoke_battle",
    "smoke_menu",
    "run_e2e",
    "run8_tests",
    "run8_e2e",
)

WEB_PRESET_NAME = "Web"          # export_presets.cfg preset to export
WEB_EXPORT_SUBDIR = ("build", "web")   # preset's export_path build/web/index.html
LOGO_DROP_NAME = "in_logo.png"   # Flux logo lands here in staging first
LOGO_DEST_PARTS = ("assets", "generated", "logo_plate.png")
BRANDING_FILE = "branding.json"

TITLE_MIN_CHARS = 3
TITLE_MAX_CHARS = 40
SLUG_MAX_CHARS = 40

IMPORT_TIMEOUT_S = 900     # first headless import of a fresh copy
GATE_TIMEOUT_S = 900       # per suite (no silent hangs -- timed kill, memory law)
EXPORT_TIMEOUT_S = 1800    # web export
REPORT_TAIL_LINES = 15     # log tail kept per suite in the report

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_GATES = 2

_SLUG_OK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PALETTE_OK = re.compile(r"^#?[0-9a-fA-F]{6}$")
_CONFIG_NAME_RE = re.compile(r"(?m)^(\s*config/name\s*=\s*).*$")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Order loading + validation
# ---------------------------------------------------------------------------

class OrderError(Exception):
    """Bad work-order / environment. exit 1."""


def load_order(path: str) -> dict:
    """Read + validate the work-order JSON. Returns a normalized dict."""
    if not os.path.isfile(path):
        raise OrderError("order file not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise OrderError("order file is not valid JSON (%s): %s" % (path, exc))

    if not isinstance(raw, dict):
        raise OrderError("order must be a JSON object")

    owner = raw.get("from_telegram_id")
    if isinstance(owner, bool) or not isinstance(owner, (int, str)):
        raise OrderError("from_telegram_id must be an integer (got %r)" % (owner,))
    if isinstance(owner, str):
        if not owner.strip().isdigit():
            raise OrderError("from_telegram_id must be a positive integer (got %r)" % (owner,))
        owner = int(owner.strip())
    if owner <= 0:
        raise OrderError("from_telegram_id must be a positive integer (got %r)" % (owner,))

    template_req = raw.get("template")
    if not isinstance(template_req, str) or template_req.strip().lower() not in TEMPLATE_ALIASES:
        raise OrderError(
            "template must be one of %s (got %r)" % (", ".join(sorted(TEMPLATE_ALIASES)), template_req)
        )
    template_req = template_req.strip().lower()

    title = raw.get("title")
    if not isinstance(title, str):
        raise OrderError("title must be a string (got %r)" % (title,))
    title = title.strip()
    if not TITLE_MIN_CHARS <= len(title) <= TITLE_MAX_CHARS:
        raise OrderError(
            "title must be %d-%d chars (got %d): %r" % (TITLE_MIN_CHARS, TITLE_MAX_CHARS, len(title), title)
        )

    palette = raw.get("palette")
    if palette is not None:
        if not isinstance(palette, str) or not _PALETTE_OK.match(palette.strip()):
            raise OrderError("palette must be a 6-digit hex color like #7C4DFF (got %r)" % (palette,))
        palette = "#" + palette.strip().lstrip("#").lower()

    slug = raw.get("slug")
    if slug is not None:
        if not isinstance(slug, str):
            raise OrderError("slug must be a string (got %r)" % (slug,))
        slug = slug.strip()
        if not slug or len(slug) > SLUG_MAX_CHARS or not _SLUG_OK.match(slug):
            raise OrderError(
                "slug must be 1-%d chars of [a-z0-9-], no leading/trailing/double hyphens (got %r)"
                % (SLUG_MAX_CHARS, slug)
            )
    else:
        slug = sanitize_slug(title)
        if not slug:
            raise OrderError(
                "title %r produces an empty slug (needs at least one a-z/0-9 char)" % title
            )

    return {
        "owner_id": owner,
        "template_requested": template_req,
        "template_resolved": TEMPLATE_ALIASES[template_req],
        "title": title,
        "slug": slug,
        "palette": palette,
    }


def sanitize_slug(title: str) -> str:
    """Title -> slug: lowercase, [a-z0-9-], single hyphens, trimmed."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:SLUG_MAX_CHARS].strip("-")


# ---------------------------------------------------------------------------
# Step 1 -- copy template into staging
# ---------------------------------------------------------------------------

def check_template_dir(template_dir: str) -> None:
    if not os.path.isdir(template_dir):
        raise OrderError("template dir not found: %s" % template_dir)
    if not os.path.isfile(os.path.join(template_dir, "project.godot")):
        raise OrderError("template dir has no project.godot (not a Godot project): %s" % template_dir)


def copy_template(template_dir: str, staging: str) -> None:
    check_template_dir(template_dir)

    template_root = os.path.normpath(os.path.abspath(template_dir))

    def _ignore(current_dir: str, names):
        if os.path.normpath(os.path.abspath(current_dir)) == template_root:
            return [n for n in names if n in EXCLUDED_FROM_COPY]
        return []

    if os.path.isdir(staging):
        shutil.rmtree(staging)  # idempotent: same order overwrites cleanly
    shutil.copytree(template_dir, staging, ignore=_ignore)


# ---------------------------------------------------------------------------
# Step 2 -- rebrand staging
# ---------------------------------------------------------------------------

def rebrand(staging: str, order: dict, created: str) -> None:
    pg_path = os.path.join(staging, "project.godot")
    try:
        with open(pg_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise OrderError("cannot read %s: %s" % (pg_path, exc))

    new_text, n = _CONFIG_NAME_RE.subn(
        lambda m: m.group(1) + json.dumps(order["title"]), text, count=1
    )
    if n != 1:
        raise OrderError("could not find config/name in %s -- template malformed?" % pg_path)
    with open(pg_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)

    branding = {
        "title": order["title"],
        "palette": order["palette"],
        "owner_telegram_id": order["owner_id"],
        "created": created,
    }
    with open(os.path.join(staging, BRANDING_FILE), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(branding, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    # Flux logo drop: staging/in_logo.png -> assets/generated/logo_plate.png.
    logo_src = os.path.join(staging, LOGO_DROP_NAME)
    logo_dst = os.path.join(staging, *LOGO_DEST_PARTS)
    if os.path.isfile(logo_src):
        os.makedirs(os.path.dirname(logo_dst), exist_ok=True)
        if os.path.exists(logo_dst):
            os.remove(logo_dst)  # Windows rename refuses to overwrite
        shutil.move(logo_src, logo_dst)
        _log("  logo: %s -> %s" % (LOGO_DROP_NAME, "/".join(LOGO_DEST_PARTS)))
    else:
        _log("  logo: no %s drop -- keeping template logo" % LOGO_DROP_NAME)


# ---------------------------------------------------------------------------
# Godot runner
# ---------------------------------------------------------------------------

def run_godot(godot_bin: str, godot_args, timeout_s: int):
    cmd = [godot_bin] + list(godot_args)
    _log("  $ %s" % " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        return {"exit": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or "",
                "timed_out": False}
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {"exit": None, "stdout": out, "stderr": err, "timed_out": True}
    except OSError as exc:
        return {"exit": None, "stdout": "", "stderr": "failed to launch godot: %s" % exc, "timed_out": False}


def _tail(text: str, lines: int = REPORT_TAIL_LINES) -> str:
    kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Step 3 -- gates (7 suites, REQUIRED before deploy)
# ---------------------------------------------------------------------------

def run_gates(godot_bin: str, staging: str, dry_run: bool):
    """Returns (ok, results). Results also written to gate_report.json by caller."""
    results = []

    if dry_run:
        for suite in GATE_SUITES:
            script = os.path.join(staging, "tests", suite + ".gd")
            results.append({"suite": suite, "status": "skipped-dry-run",
                            "present": os.path.isfile(script), "exit": None, "seconds": None,
                            "tail": ""})
        return True, results

    # First headless import of the fresh copy: generates .godot/ caches that
    # both the test scripts and the exporter need. Counts as a gate.
    t0 = datetime.datetime.now()
    imp = run_godot(godot_bin, ["--headless", "--path", staging, "--import"], IMPORT_TIMEOUT_S)
    secs = round((datetime.datetime.now() - t0).total_seconds(), 1)
    imp_ok = imp["exit"] == 0
    results.append({"suite": "--import", "status": "pass" if imp_ok else "fail",
                    "present": True, "exit": imp["exit"], "seconds": secs,
                    "tail": _tail(imp["stdout"] + imp["stderr"])})
    if not imp_ok:
        for suite in GATE_SUITES:
            results.append({"suite": suite, "status": "blocked (import failed)", "present": None,
                            "exit": None, "seconds": None, "tail": ""})
        return False, results

    ok = True
    for suite in GATE_SUITES:
        script = os.path.join(staging, "tests", suite + ".gd")
        t0 = datetime.datetime.now()
        if not os.path.isfile(script):
            results.append({"suite": suite, "status": "fail (suite script missing)",
                            "present": False, "exit": None, "seconds": 0.0,
                            "tail": "expected tests/%s.gd in staging -- incomplete template" % suite})
            ok = False
            continue
        res = run_godot(godot_bin, ["--headless", "--path", staging, "--script",
                                    "res://tests/%s.gd" % suite], GATE_TIMEOUT_S)
        secs = round((datetime.datetime.now() - t0).total_seconds(), 1)
        passed = res["exit"] == 0
        ok = ok and passed
        results.append({
            "suite": suite,
            "status": "pass" if passed else ("fail (timeout after %ds)" % GATE_TIMEOUT_S if res["timed_out"]
                                             else "fail (exit %s)" % res["exit"]),
            "present": True, "exit": res["exit"], "seconds": secs,
            "tail": _tail(res["stdout"] + res["stderr"]),
        })
    return ok, results


# ---------------------------------------------------------------------------
# Step 4 -- web export
# ---------------------------------------------------------------------------

def export_web(godot_bin: str, staging: str):
    """Runs the Web preset export inside staging. Returns the build/web dir."""
    res = run_godot(godot_bin, ["--headless", "--path", staging,
                                "--export-release", WEB_PRESET_NAME], EXPORT_TIMEOUT_S)
    if res["exit"] != 0:
        raise OrderError(
            "web export failed (exit %s%s)\n%s" % (
                res["exit"], ", timeout" if res["timed_out"] else "",
                _tail(res["stdout"] + res["stderr"], 30))
        )
    web_dir = os.path.join(staging, *WEB_EXPORT_SUBDIR)
    if not os.path.isfile(os.path.join(web_dir, "index.html")):
        raise OrderError(
            "export claimed success but %s is missing -- check the %r preset's export_path"
            % (os.path.join(*WEB_EXPORT_SUBDIR, "index.html"), WEB_PRESET_NAME)
        )
    return web_dir


# ---------------------------------------------------------------------------
# Step 5 -- hub bundle
# ---------------------------------------------------------------------------

def build_hub_bundle(outputs_dir: str, web_dir, order: dict, updated: str):
    bundle = os.path.join(outputs_dir, "hub_bundle")
    if os.path.isdir(bundle):
        shutil.rmtree(bundle)  # idempotent (only the bundle -- outputs dir keeps gate_report.json)
    os.makedirs(bundle)

    if web_dir is not None:
        shutil.copytree(web_dir, bundle, dirs_exist_ok=True)

    game = {
        "owner_id": order["owner_id"],
        "title": order["title"],
        "slug": order["slug"],
        "url": "games/%d-%s/" % (order["owner_id"], order["slug"]),
        "updated": updated,
    }
    with open(os.path.join(bundle, "game.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(game, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return bundle, game


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="P8 template instantiation: work-order -> branded game -> gated hub bundle.")
    parser.add_argument("--order", required=True, help="path to the NEW-GAME work-order JSON")
    parser.add_argument("--template-dir", required=True, help="path to the arcade template repo checkout")
    parser.add_argument("--out", required=True, help="staging dir (outputs land in the sibling <out>-out/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="everything EXCEPT godot import/gates/export (no godot binary needed)")
    args = parser.parse_args(argv)

    staging = os.path.normpath(os.path.abspath(args.out))
    outputs_dir = os.path.join(os.path.dirname(staging), os.path.basename(staging) + "-out")

    _log("== P8 instantiate_new_game%s ==" % (" (DRY RUN)" if args.dry_run else ""))
    _log("order:       %s" % os.path.abspath(args.order))
    _log("template:    %s" % os.path.abspath(args.template_dir))
    _log("staging:     %s" % staging)
    _log("outputs:     %s" % outputs_dir)

    try:
        order = load_order(args.order)
        check_template_dir(args.template_dir)
    except OrderError as exc:
        _log("VALIDATION ERROR: %s" % exc)
        return EXIT_USAGE
    _log("order ok:    player=%d template=%s (resolved: %s) title=%r slug=%s palette=%s"
         % (order["owner_id"], order["template_requested"], order["template_resolved"],
            order["title"], order["slug"], order["palette"] or "<template default>"))

    godot_bin = os.environ.get("GODOT_BIN") or GODOT_BIN_DEFAULT
    if not args.dry_run:
        if not os.path.isfile(godot_bin):
            _log("ENVIRONMENT ERROR: godot binary not found at %s (set GODOT_BIN)" % godot_bin)
            return EXIT_USAGE
        _log("godot:       %s" % godot_bin)
    else:
        _log("godot:       SKIPPED (dry run)")

    created = _now_iso()

    try:
        _log("\n[1/5] copy template -> staging (excluding %s)"
             % ", ".join(sorted(EXCLUDED_FROM_COPY)))
        copy_template(args.template_dir, staging)
        _log("      copied")

        _log("\n[2/5] rebrand staging")
        rebrand(staging, order, created)

        gate_ok, gate_results = run_gates(godot_bin, staging, args.dry_run)

        # Written immediately so it exists on the exit-2 path too.
        os.makedirs(outputs_dir, exist_ok=True)
        with open(os.path.join(outputs_dir, "gate_report.json"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"order": order, "dry_run": args.dry_run, "created": created,
                       "ok": gate_ok, "suites": gate_results}, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

        _log("\n[3/5] gates (7 suites, REQUIRED before deploy)")
        for res in gate_results:
            secs = "%.1fs" % res["seconds"] if res["seconds"] is not None else "-"
            _log("      %-12s %-28s %-6s %s" % (res["suite"], res["status"],
                                                "" if res["present"] in (True, None) else "ABSENT",
                                                secs))
        if not gate_ok:
            _log("\nGATE FAILURE -- aborting, exit 2. Staging LEFT IN PLACE for inspection: %s" % staging)
            _log("report: %s" % os.path.join(outputs_dir, "gate_report.json"))
            for res in gate_results:
                if res["status"].startswith("fail") or res["status"].startswith("blocked"):
                    _log("\n--- %s ---\n%s" % (res["suite"], res["tail"] or "(no output)"))
            return EXIT_GATES

        web_dir = None
        if args.dry_run:
            _log("\n[4/5] web export: SKIPPED (dry run)")
        else:
            _log("\n[4/5] web export (%s preset)" % WEB_PRESET_NAME)
            web_dir = export_web(godot_bin, staging)
            _log("      exported -> %s" % web_dir)

        _log("\n[5/5] hub bundle")
        bundle, game = build_hub_bundle(outputs_dir, web_dir, order, created)
        for entry in sorted(os.listdir(bundle)):
            _log("      %s" % entry)

        _log("\n%s" % ("DRY RUN COMPLETE (no godot steps executed)." if args.dry_run
                       else "INSTANTIATION COMPLETE."))
        _log("destination (caller commits this): games/%d-%s/" % (order["owner_id"], order["slug"]))
        _log("hub_bundle: %s" % bundle)
        return EXIT_OK
    except OrderError as exc:
        _log("INSTANTIATION ERROR: %s" % exc)
        _log("staging left in place: %s" % staging)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
