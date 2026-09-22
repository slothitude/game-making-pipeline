"""critic — the Pipeline's playtester. Plays the game via Playwright MCP
(orchestrated by the parent), receives the screenshots + observations, and
produces a structured critique: scores, issues, loves, and the itch devlog quote.

Usage: python daily/critic.py --game <slug> --repo <path> --shots <dir>
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud_editor"))
from cloud_editor import llm_boss, load_config  # noqa: E402


def build_critique_prompt(game, repo, observations, spec_text):
    return (
        f"You are the Game Making Pipeline's CRITIC. You just playtested {game} "
        "(a game built entirely by the Pipeline). Here are the visual observations "
        "from the playthrough and the game's spec. Write a structured critique as "
        "JSON with these exact keys:\n"
        '{\n'
        '  "scores": {"fun": 1-10, "polish": 1-10, "theme_fit": 1-10, '
        '"readability": 1-10, "phone_ux": 1-10, "adhd_friendly": 1-10},\n'
        '  "verdict": "one sentence: should this ship?",\n'
        '  "loves": ["what made you smile — be specific"],\n'
        '  "issues": [{"severity": "blocker|annoyance|polish", '
        '"description": "what you saw", "suggestion": "the fix", '
        '"files_hint": ["where to look"]}],\n'
        '  "itch_quote": "a 2-sentence devlog post about what the critic found",\n'
        "  'itch_comment': 'a friendly public comment for the game page'\n"
        '}\n\n'
        f"SPEC:\n{spec_text[:2000]}\n\n"
        f"PLAYTHROUGH OBSERVATIONS:\n{observations}\n\n"
        "Be honest — a critic that loves everything is useless. If something "
        "is confusing, say it's confusing. If the fun isn't there yet, say so. "
        "Output ONLY the JSON."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--shots", help="path to screenshot dir")
    ap.add_argument("--observations", required=True, help="raw playthrough notes")
    ap.add_argument("--input-mode", default="drag", choices=["drag", "tap", "keyboard", "gyro"],
                    help="how the critic steered: drag (tilt fallback), tap, keyboard, gyro-emulated")
    args = ap.parse_args()

    spec_path = os.path.join(args.repo, "spec", "study_spec.json")
    if not os.path.exists(spec_path):
        spec_path = os.path.join(args.repo, "spec", "jam_spec.json")
    spec_text = open(spec_path, encoding="utf-8").read() if os.path.exists(spec_path) else "(no spec)"

    cfg = load_config()
    prompt = build_critique_prompt(args.game, args.repo,
                                   f"[INPUT MODE: {args.input_mode}] {args.observations}", spec_text)
    reply = llm_boss(cfg, [{"role": "user", "content": prompt}])

    # extract JSON
    import re
    m = re.search(r"\{[\s\S]*\}", reply)
    if not m:
        print("CRITIC FAILED — no JSON in reply")
        print(reply[:500])
        return 1

    try:
        critique = json.loads(m.group(0))
    except json.JSONDecodeError:
        print("CRITIC FAILED — unparseable JSON")
        return 1

    # save
    out = os.path.join(args.repo, "critique.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(critique, f, indent=2)

    # print summary
    print("=" * 60)
    print(f"CRITIQUE: {args.game}")
    print("=" * 60)
    scores = critique.get("scores", {})
    for k, v in scores.items():
        bar = "█" * int(v) + "░" * (10 - int(v))
        print(f"  {k:16s} {bar} {v}/10")
    avg = sum(scores.values()) / max(1, len(scores))
    print(f"  {'OVERALL':16s} {'█' * int(avg)}{'░' * (10 - int(avg))} {avg:.1f}/10")
    print(f"\n  VERDICT: {critique.get('verdict', '?')}")
    print(f"\n  LOVES:")
    for love in critique.get("loves", []):
        print(f"    💙 {love}")
    print(f"\n  ISSUES:")
    for issue in critique.get("issues", []):
        sev = issue.get("severity", "?")
        icon = "🔴" if sev == "blocker" else "🟡" if sev == "annoyance" else "🔵"
        print(f"    {icon} [{sev}] {issue.get('description', '?')}")
        print(f"       → {issue.get('suggestion', '?')}")
    print(f"\n  ITCH QUOTE: \"{critique.get('itch_quote', '')}\"")
    print(f"\n  ITCH COMMENT: \"{critique.get('itch_comment', '')}\"")
    print("=" * 60)

    # convert issues to feedback work-orders
    fb_dir = os.path.join(os.path.dirname(__file__), "..", "runtime", "feedback")
    os.makedirs(fb_dir, exist_ok=True)
    for i, issue in enumerate(critique.get("issues", [])):
        if issue.get("severity") == "polish":
            continue  # polish issues stay in the critique, don't auto-fix
        stamp = datetime.datetime.now().strftime(f"%Y%m%d-%H%M%S-critic{i}")
        entry = {
            "time": stamp,
            "source": "critic",
            "game": args.game,
            "text": f"[critic {issue['severity']}] {issue['description']} — fix: {issue['suggestion']}",
            "meta": {"from": {"id": "critic", "name": "The Critic"}, "game": args.game,
                     "files_hint": issue.get("files_hint", [])}
        }
        with open(os.path.join(fb_dir, f"{stamp}.json"), "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=1)

    print(f"\n  → {len([i for i in critique.get('issues', []) if i['severity'] != 'polish'])} feedback work-orders created")
    print(f"  → critique saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
