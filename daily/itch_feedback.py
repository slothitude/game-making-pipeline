"""itch_feedback — pull itch.io comments/devlog replies into the Pipeline.

itch has no comments API. The logged-in Playwright session (itch_web/itch.py)
scrapes each game's community tab: new comments become feedback work-orders in
runtime/feedback/ (tagged [itch]) for cloud_editor's crew to implement.

Env: TG_TOKEN, CHAT_ID (announce new comments). Run inside pipeline-poll.
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GMP = os.path.dirname(HERE)
ITCH_PY = os.path.join(GMP, "itch_web", "itch.py")
RUNTIME_FB = os.path.join(GMP, "runtime", "feedback")
SEEN = os.path.join(GMP, "runtime", "itch_seen_comments.json")
GAMES = ["octogram-arcade", "sonar", "slime-line", "star-visitor", "gyro-squadron-45"]


def load_seen():
    if os.path.exists(SEEN):
        return json.load(open(SEEN, encoding="utf-8"))
    return {}


def save_seen(data):
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    json.dump(data, open(SEEN, "w", encoding="utf-8"), indent=1)


def scrape(game):
    """Drive the logged-in browser (itch.py session) to read the community tab."""
    url = f"https://slothitude.itch.io/{game}/comments"
    script = f"""
(async () => {{
    const {{ chromium }} = require('playwright');
    const browser = await chromium.launchPersistentContext(
        (process.env.ITCH_PROFILE || "{os.path.join(GMP, 'itch_web', 'session', 'chrome_profile')}"),
        {{ headless: false, channel: 'chrome' }});
    const page = browser.pages()[0] || await browser.newPage();
    await page.goto("{url}", {{ waitUntil: 'domcontentloaded', timeout: 60000 }});
    await page.waitForTimeout(4000);
    const comments = await page.evaluate(() => {{
        const nodes = document.querySelectorAll('.community_topic, .post_row, .comment, [class*="topic"]');
        return Array.from(nodes).slice(0, 30).map(n => {{
            const author = n.querySelector('.post_author a, .user_link, [class*="author"] a');
            const body = n.querySelector('.post_body, .comment_body, [class*="body"]');
            const date = n.querySelector('abbr, time, [class*="date"]');
            return {{
                author: author ? author.textContent.trim() : '?',
                text: body ? body.textContent.trim().slice(0, 800) : '',
                date: date ? date.textContent.trim() : ''
            }};
        }}).filter(c => c.text);
    }});
    await browser.close();
    return JSON.stringify(comments);
}})()
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       timeout=180, cwd=os.path.join(GMP, "itch_web"))
    if r.returncode != 0:
        print(f"scrape {game} failed: {r.stderr[-200:]}", flush=True)
        return []
    try:
        return json.loads(r.stdout.strip().split("\n")[-1])
    except Exception:
        return []


def tg(text):
    token = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("CHAT_ID", "")
    if not token or not chat:
        return
    import urllib.request
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text[:3900]}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def main():
    seen = load_seen()
    new_count = 0
    for game in GAMES:
        key_base = f"{game}"
        prev = seen.get(key_base, [])
        comments = scrape(game)
        fresh = [c for c in comments if c["text"] not in prev]
        for c in fresh[:10]:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            entry = {
                "time": stamp,
                "source": "itch",
                "game": game,
                "text": f"[itch comment on {game} by {c['author']}] {c['text']}",
                "meta": {"from": {"id": "itch", "name": c["author"]}, "game": game},
            }
            os.makedirs(RUNTIME_FB, exist_ok=True)
            open(os.path.join(RUNTIME_FB, f"itch-{stamp}.json"), "w", encoding="utf-8").write(
                json.dumps(entry, indent=1))
            new_count += 1
            tg(f"💬 itch feedback on {game} from {c['author']}: {c['text'][:200]}")
        if comments:
            seen[key_base] = [c["text"] for c in comments[:30]]
        time.sleep(3)
    save_seen(seen)
    print(f"itch feedback: {new_count} new work-orders" if new_count else "itch feedback: none new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
