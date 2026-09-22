"""cloud_editor — the feedback→implement→deploy agent harness for Octogram Arcade.

v4: the two-tier LLM company — glm-5.3 is the BOSS (triage -> plan JSON ->
review; the reviewer owns the last word) and openrouter/free workers draft the
code/data subtasks. All v3 behavior is preserved (inline keyboards + callback
queries, the per-chat session FSM, deploy confirmation buttons, file uploads,
and all v2 behavior: commands, deep links, the gate wall, Actions mode, the
selftest cases). A worker failure degrades silently to boss-only.

Loop (run on Rog with no flags, or as a scheduled GitHub Actions job with --once):
  1. Poll Telegram getUpdates. Updates route by shape:
       message (text) — by intent, unchanged from v2:
         /start login (or a hub deep link)   -> greeting + hub link + main menu
         /newgame <arcade|rpg|eight> <title> -> new_game_work_order JSON in orders/
         /feedback <text> or plain text      -> the feedback -> diff -> gates pipeline
         /status                             -> queue/deploy counters
         /stop (and /resume)                 -> paused.flag idle gate
         anything else                       -> the menu (+ main menu buttons)
       callback_query — by callback-data prefix:
         menu:<games|newgame|fix|feedback|status|main> -> the menu screens
         tpl:<arcade|rpg|eight>           -> new-game session, awaiting the title
         vibe:<bright|cozy|dark|surprise> -> finish the session, write the order
         dep:yes|dep:diff|dep:no[:order]  -> answer a pending deploy confirmation
       message (photo/voice/audio/.zip)  -> uploads/<chat_id>/<md5>.<ext>; a photo
         inside a newgame session becomes the game logo, anything else attaches to
         the next feedback from that chat; voice also queues a transcription order.
  2. For feedback: glm-5.3 (NVIDIA free endpoint, OpenAI-compatible) triages the
     tier and emits a plan JSON ({reply, subtasks}); openrouter/free workers draft
     one diff per code/data subtask; the boss reviews and either APPROVES the
     merged drafts or outputs its own corrected diff — the diff that ships is
     always the boss's. No parseable plan, or the worker endpoint is dead (401/
     429/timeout)? The boss does it all itself (silent v3 single-shot fallback).
  3. Apply the patch on a per-order scratch branch (cloud_editor/scratch-<order>);
     run THE GATE WALL (Godot headless suites).
       T1/T2 diffs (art, data/numbers) still auto-deploy exactly like v2.
       T3 code diffs ASK FIRST: the player gets the short plan plus
       [🚀 Deploy] [👀 Show diff] [❌ Cancel]; the choice is parked in the chat's
       session (state confirm_feedback) until a button answers it —
       dep:yes runs the v2 export+commit+push path, dep:no discards the branch.
Every deploy is a git commit — always revertible. Gates are the safety net that
makes autonomy sane.

Multi-tenant: every message's from.id is recorded with the feedback/order it produced;
replies go to the SAME chat the message came from, and are mirrored to the owner's
review channel (config chat_id) with a "[player <id> <name>]" prefix.

Config: env TG_TOKEN / NVAPI_KEY / OPENROUTER_KEY / CHAT_ID win, else
        cloud_editor/config.json
        {bot_token, chat_id, nvapi_key, openrouter_key, repo_dir} (local default).
State:  cloud_editor/state.json {last_update_id, deployed_count, last_action}
Queue:  cloud_editor/feedback/*.json (audit trail of every request)
Orders: cloud_editor/orders/*.json  (new-game + voice transcription work orders)
Sessions: cloud_editor/sessions/<chat_id>.json {state, data}  (per-chat FSM)
Uploads:  cloud_editor/uploads/<chat_id>/<md5>.<ext>
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.environ.get("RUNTIME_DIR") or HERE
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(RUNTIME, "state.json")
FEEDBACK_DIR = os.path.join(RUNTIME, "feedback")
ORDERS_DIR = os.path.join(RUNTIME, "orders")
SESSIONS_DIR = os.path.join(RUNTIME, "sessions")
UPLOADS_DIR = os.path.join(HERE, "uploads")
PAUSED_PATH = os.path.join(HERE, "paused.flag")
HUB_INDEX = os.path.join(HERE, "..", "hub", "games", "index.json")
REPO_DEFAULT = r"C:\Users\aaron\octogram-arcade"
GODOT = os.environ.get("GODOT_BIN") or r"C:\Users\aaron\AppData\Local\Godot\Godot_v4.7.1-stable_win64.exe"
LLM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
LLM_MODEL = "z-ai/glm-5.3"          # BOSS tier: triage, review, last word
WORKER_URL = "https://openrouter.ai/api/v1/chat/completions"
WORKER_MODEL = "openrouter/free"    # WORKER tier: drafts code/data subtasks
PLAN_KINDS = ("code", "art", "data")  # subtask whitelist (art stays with the boss)
MAX_SUBTASKS = 3                    # a plan carries 0-3 subtasks
GATE_SUITES = ["run_tests", "run_rpg_tests", "smoke_battle", "smoke_menu",
               "run_e2e", "run8_tests", "run8_e2e"]
MAX_ATTEMPTS = 3
POLL_SECONDS = 20.0
HUB_URL = "https://slothitude.github.io/game-making-pipeline/"
TEMPLATES = ("arcade", "rpg", "eight")
TITLE_MIN = 3
TITLE_MAX = 40
SCRATCH_BASE = "cloud_editor/scratch"
DEPLOY_DIFF_SPLIT = 3900
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
NEWGAME_STATES = ("newgame_template", "newgame_title")
VIBES = {
    "bright": "bright, saturated, high-energy",
    "cozy": "warm, soft, low-contrast evening",
    "dark": "dark, moody, high-contrast",
    "surprise": "dealer's choice",
}
# Deploy tiers: T3 = code (asks first), T2 = data/numbers, T1 = art (both ship).
TIER_CODE_EXTS = (".gd", ".tscn", ".py", ".js", ".html", ".css", ".cs", ".godot")
TIER_DATA_EXTS = (".txt", ".json", ".csv", ".tres", ".res", ".cfg", ".import")
TIER_ART_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ogg",
                 ".wav", ".mp3", ".ttf", ".otf", ".glb", ".gltf")

SYSTEM_PROMPT = (
    "You are cloud_editor, the maintenance agent for Octogram Arcade — a Godot 4.7 "
    "GDScript word-game trilogy (Word Poker arena, an RPG campaign, and Eight Letters) "
    "in one app. A player sent feedback. Decide: (a) if it needs a code/content change, "
    "output a unified diff (git apply format, paths relative to the repo root) that "
    "implements it MINIMALLY and safely — match the codebase's GDScript 4 style, tabs, "
    "constants-not-magic-numbers; (b) if it is a question/opinion, reply conversationally "
    "with no diff. Your output must be EITHER a short reply message AND a fenced diff "
    "block, or just a short reply. Never output partial files, only diffs. "
    "The player may own their own generated game under games/<id>-<slug>/ in the hub "
    "repo — for feedback tagged with an owner game, restrict edits to that game's "
    "directory; for pipeline-owned games (octogram arcade family) edit as before."
)

# v4 two-tier crew: the boss triages before anyone writes a diff. Appended to
# SYSTEM_PROMPT for the triage pass (and kept for the whole boss thread).
TRIAGE_ADDENDUM = (
    "\n\nTWO-TIER CREW: you are the BOSS — openrouter/free workers draft code for "
    "you and you review it; you own the last word. First classify the tier: "
    "T0 = question/opinion, T1 = art, T2 = data/numbers, T3 = code. For T0 just "
    "reply conversationally — no JSON, no diff. For T1/T2/T3 do NOT write the "
    "diff yourself yet: output one short line naming the tier, then ONE fenced "
    "json block shaped exactly like "
    '{"reply": "<short message for the player>", "subtasks": [{"id": 1, '
    '"kind": "code", "instruction": "<exact, self-contained change spec>", '
    '"files_hint": ["scripts/game_manager.gd"]}]} '
    "with 0-3 subtasks (kind is code|art|data). code/data subtasks go to the "
    "workers; kind \"art\" subtasks stay yours — express them as diffs too "
    "(theme/.tres/.tscn edits), nobody here can paint pixels."
)

# v4: the worker tier drafts one subtask at a time. Plain OpenAI-compatible call.
WORKER_PROMPT = (
    "You are a worker coder on the cloud_editor crew for Octogram Arcade — a "
    "Godot 4.7 GDScript word-game trilogy (Word Poker arena, an RPG campaign, "
    "Eight Letters) in one app. Implement the assigned subtask MINIMALLY and "
    "safely as ONE unified diff (git apply format, paths relative to the repo "
    "root): match the codebase's GDScript 4 style, tabs, constants-not-magic-"
    "numbers. Never output partial files, only diffs. Output ONLY one fenced "
    "```diff block."
)

PLAN_NUDGE = (
    "That had no usable plan JSON. Either answer conversationally with no diff, "
    "or output your short reply plus ONE fenced ```diff block (git apply format) "
    "implementing the change."
)

REVIEW_NUDGE = (
    "Output APPROVED plus one fenced ```diff block, or your own corrected full "
    "fenced ```diff block. Nothing else."
)

MENU_TEXT = (
    "🤖 I'm cloud_editor — the Game Making Pipeline's factory agent.\n"
    f"Hub: {HUB_URL}\n\n"
    "/start — greeting + hub link\n"
    "/newgame <arcade|rpg|eight> <title> — spin up your own game\n"
    "/feedback <text> — ask for a change (plain text counts too)\n"
    "/status — what have I been up to\n"
    "/stop — pause me · /resume — wake me\n\n"
    "Or just tap a button: 🎮 my games · 🏭 new game · 🛠 fix · 💬 feedback · 📊 status\n"
    "Code changes ask before shipping; art/number tweaks just ship."
)

NO_GAMES_TEXT = "🎮 no games yet — make one!"

MAIN_MENU = [
    [{"text": "🎮 My Games", "callback_data": "menu:games"},
     {"text": "🏭 New Game", "callback_data": "menu:newgame"}],
    [{"text": "🛠 Fix a Game", "callback_data": "menu:fix"},
     {"text": "💬 Feedback", "callback_data": "menu:feedback"}],
    [{"text": "📊 Status", "callback_data": "menu:status"}],
]

VIBE_BUTTONS = [
    [{"text": "🌈 Bright", "callback_data": "vibe:bright"},
     {"text": "🌙 Cozy", "callback_data": "vibe:cozy"}],
    [{"text": "🖤 Dark", "callback_data": "vibe:dark"},
     {"text": "🎲 Surprise", "callback_data": "vibe:surprise"}],
]


def load_config():
    """Env vars first (Actions secrets), config.json as the local default."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    cfg["bot_token"] = os.environ.get("TG_TOKEN") or cfg.get("bot_token", "")
    cfg["nvapi_key"] = os.environ.get("NVAPI_KEY") or cfg.get("nvapi_key", "")
    cfg["chat_id"] = os.environ.get("CHAT_ID") or cfg.get("chat_id", "")
    # v4 worker tier. config.json.example note: add
    #   "openrouter_key": "<your OpenRouter key>"
    # (env OPENROUTER_KEY wins). No key -> boss-only: every subtask is drafted by
    # glm-5.3 itself, silently.
    cfg["openrouter_key"] = (os.environ.get("OPENROUTER_KEY")
                             or cfg.get("openrouter_key", ""))
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"last_update_id": 0}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------- Telegram ---
# (api_post is the only network door; everything above it is a thin wrapper.)

def api_post(url, payload, token=None, timeout=400):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def tg_send(cfg, text, chat_id=None):
    api_post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
             {"chat_id": chat_id or cfg["chat_id"], "text": text[:3900]})


def tg_send_kb(cfg, chat_id, text, buttons=None):
    """sendMessage with an inline keyboard: buttons = [[{text, callback_data}|{text, url}], ...]."""
    payload = {"chat_id": chat_id, "text": text[:3900]}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return api_post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage", payload)


def tg_edit_text(cfg, chat_id, message_id, text, buttons=None):
    """Rewrite an existing menu message in place instead of stacking new ones.
    Returns the API result, or None if there is nothing/no way to edit."""
    if message_id is None:
        return None
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:3900]}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        return api_post(f"https://api.telegram.org/bot{cfg['bot_token']}/editMessageText",
                        payload)
    except Exception:  # noqa: BLE001 — a stale/unchanged message falls back to send
        return None


def tg_edit_or_send(cfg, chat_id, message_id, text, buttons=None):
    """Prefer editing the menu message; fall back to a fresh send if the edit fails."""
    if chat_id is None:
        return
    if message_id is not None and tg_edit_text(cfg, chat_id, message_id, text, buttons):
        return
    tg_send_kb(cfg, chat_id, text, buttons)


def tg_answer(cfg, callback_id, text=None):
    """answerCallbackQuery — best effort; a stale callback id must never raise."""
    if not callback_id:
        return
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:190]
    try:
        api_post(f"https://api.telegram.org/bot{cfg['bot_token']}/answerCallbackQuery",
                 payload)
    except Exception:  # noqa: BLE001
        pass


def tg_get_file(cfg, file_id):
    data = api_post(f"https://api.telegram.org/bot{cfg['bot_token']}/getFile",
                    {"file_id": file_id})
    return (data or {}).get("result") or {}


def tg_download(cfg, file_path):
    """Raw bytes of a Telegram file; the caller enforces the size guard."""
    url = f"https://api.telegram.org/file/bot{cfg['bot_token']}/{file_path}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read(MAX_UPLOAD_BYTES + 1)


def sender_tag(sender):
    return f"[player {sender.get('id', '?')} {sender.get('name', '?')}]"


def tg_reply(cfg, chat_id, text, sender=None, buttons=None):
    """Two sends: the reply goes to the SAME chat the message came from, and is
    mirrored to the owner review channel (config chat_id) tagged with the player.
    Inline buttons go to the player chat only — the owner mirror stays plain."""
    if buttons:
        tg_send_kb(cfg, chat_id, text, buttons)
    else:
        tg_send(cfg, text, chat_id=chat_id)
    if chat_id is not None and str(chat_id) != str(cfg.get("chat_id")):
        who = sender_tag(sender) if sender else "[player ?]"
        tg_send(cfg, f"{who} {text}")


def tg_poll(cfg, offset):
    url = (f"https://api.telegram.org/bot{cfg['bot_token']}/getUpdates"
           f"?timeout=0&offset={offset + 1}")
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def sender_of(msg):
    frm = msg.get("from") or {}
    return {"id": frm.get("id", "?"),
            "name": frm.get("first_name") or frm.get("username") or "player"}


# ------------------------------------------------------------- text intents ---

def parse_intent(text):
    """Pure text -> intent dict. Deep links arrive as "/start <payload>" with the
    payload URL-decoded by Telegram ("newgame-arcade-My%20Title" becomes the text
    "/start newgame-arcade-My Title"). Kinds: login, newgame, feedback, status,
    stop, resume, unknown, error (with a human message)."""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return {"kind": "feedback", "text": raw}
    tokens = raw[1:].split(None, 1)
    cmd = (tokens[0] if tokens else "").lower()
    payload = tokens[1].strip() if len(tokens) > 1 else ""

    if cmd == "start":
        if "%" in payload:
            payload = urllib.parse.unquote(payload)
        if payload.startswith("newgame-"):
            return _newgame_intent(payload[len("newgame-"):])
        return {"kind": "login"}
    if cmd == "newgame":
        if not payload:
            return {"kind": "error",
                    "message": "Usage: /newgame <arcade|rpg|eight> <title>\n"
                               "e.g. /newgame arcade Daily Word Duel"}
        parts = payload.split(None, 1)
        title = parts[1].strip() if len(parts) > 1 else ""
        return _newgame_intent(f"{parts[0]}-{title}")
    if cmd == "feedback":
        if not payload:
            return {"kind": "error",
                    "message": "Usage: /feedback <text> — or just send plain text."}
        return {"kind": "feedback", "text": payload}
    if cmd == "stop":
        return {"kind": "stop"}
    if cmd == "resume":
        return {"kind": "resume"}
    if cmd == "status":
        return {"kind": "status"}
    return {"kind": "unknown", "command": cmd}


def _newgame_intent(rest):
    template, _, title = rest.partition("-")
    template = template.strip().lower()
    title = title.strip()
    if template not in TEMPLATES:
        return {"kind": "error",
                "message": f"Unknown template {template or '(none)'!r} — "
                           f"pick one of: {', '.join(TEMPLATES)}."}
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        return {"kind": "error",
                "message": f"Title must be {TITLE_MIN}-{TITLE_MAX} characters "
                           f"(got {len(title)})."}
    return {"kind": "newgame", "template": template, "title": title}


def parse_callback(data):
    """callback_query data -> intent dict, dispatched by prefix:
    dep:yes|dep:diff|dep:no (optional :<order_id>), menu:<item>, tpl:<template>,
    vibe:<vibe>. Unknown data -> kind "unknown"."""
    raw = (data or "").strip()
    head, _, rest = raw.partition(":")
    head = head.lower()
    if head == "dep":
        action = rest.split(":", 1)[0].lower()
        if action in ("yes", "diff", "no"):
            order = rest.split(":", 1)[1] if ":" in rest else None
            return {"kind": "deploy", "action": action, "order": order or None}
    if head == "menu":
        return {"kind": "menu", "item": (rest or "main").lower()}
    if head == "tpl":
        return {"kind": "template", "template": rest.lower()}
    if head == "vibe":
        return {"kind": "vibe", "vibe": rest.lower()}
    return {"kind": "unknown", "data": raw}


# ---------------------------------------------------------- per-chat session ---

def idle_session():
    return {"state": "idle", "data": {}}


def session_path(chat_id, sessions_dir=SESSIONS_DIR):
    return os.path.join(sessions_dir, f"{chat_id}.json")


def load_session(chat_id, sessions_dir=SESSIONS_DIR):
    try:
        with open(session_path(chat_id, sessions_dir)) as f:
            sess = json.load(f)
    except (OSError, ValueError):
        return idle_session()
    if not isinstance(sess, dict) or "state" not in sess:
        return idle_session()
    if not isinstance(sess.get("data"), dict):
        sess["data"] = {}
    return sess


def save_session(chat_id, sess, sessions_dir=SESSIONS_DIR):
    os.makedirs(sessions_dir, exist_ok=True)
    with open(session_path(chat_id, sessions_dir), "w") as f:
        json.dump(sess, f, indent=2)


def session_reset(sess=None):
    """Any /start or menu button lands back here: state idle, data empty."""
    return idle_session()


def session_hard_reset(repo, chat_id, sessions_dir=SESSIONS_DIR):
    """Menu/greeting reset: drop the chat's session; a pending deploy proposal
    loses its scratch branch with it."""
    sess = load_session(chat_id, sessions_dir)
    if sess.get("state") == "confirm_feedback":
        discard_scratch(repo, (sess.get("data") or {}).get("branch"))
    save_session(chat_id, idle_session(), sessions_dir)


def session_pick_template(sess, template):
    """tpl:<name> -> (session, ok): state newgame_template, awaiting the title."""
    if template not in TEMPLATES:
        return sess, False
    return {"state": "newgame_template", "data": {"template": template}}, True


def valid_title(title):
    return bool(title) and TITLE_MIN <= len(title) <= TITLE_MAX


def session_on_text(sess, text):
    """Natural text inside a newgame session is the title. Returns
    (session, action): "ask_vibe" | "bad_title" | "none". Text in either
    newgame state (re)sets the title — typing while awaiting the vibe is a rename."""
    if sess.get("state") in NEWGAME_STATES:
        title = (text or "").strip()
        if not valid_title(title):
            return sess, "bad_title"
        data = dict(sess.get("data") or {})
        data["title"] = title
        return {"state": "newgame_title", "data": data}, "ask_vibe"
    return sess, "none"


def session_pick_vibe(sess, vibe):
    """vibe:<name> -> (session_after, order_fields|None). The order fields carry
    template + title + palette hint (+ logo_upload when a photo was sent)."""
    data = sess.get("data") or {}
    if sess.get("state") != "newgame_title" or vibe not in VIBES:
        return sess, None
    if not data.get("template") or not valid_title(data.get("title")):
        return sess, None  # never ship an order without a template and a title
    fields = {"template": data.get("template"),
              "title": data.get("title"),
              "palette": VIBES[vibe],
              "logo_upload": data.get("logo")}
    return idle_session(), fields


# ------------------------------------------------------------------ orders ---

def write_order(template, title, sender, chat_id, extra=None):
    """Persist a new_game_work_order for the template-instantiation front (P8)."""
    os.makedirs(ORDERS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-") or "untitled"
    order = {
        "time": ts,
        "type": "new_game_work_order",
        "template": template,
        "title": title,
        "slug": slug,
        "player": {"id": sender.get("id", "?"), "name": sender.get("name", "?")},
        "chat_id": chat_id,
        "status": "pending",
    }
    if extra:
        order.update(extra)
    path = os.path.join(ORDERS_DIR, f"{ts}-{slug}.json")
    with open(path, "w") as f:
        json.dump(order, f, indent=2)
    return path


def write_voice_order(path, chat_id, orders_dir=ORDERS_DIR):
    """Transcription work-order for a saved voice note."""
    os.makedirs(orders_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    order = {"time": ts, "type": "voice_transcription",
             "file": path, "chat_id": chat_id, "status": "pending"}
    out = os.path.join(orders_dir, f"voice_{ts}.json")
    with open(out, "w") as f:
        json.dump(order, f, indent=2)
    return out


def dir_count(path, suffix=".json"):
    try:
        return len([n for n in os.listdir(path) if n.endswith(suffix)])
    except OSError:
        return 0


def status_report(state):
    paused = "yes" if os.path.exists(PAUSED_PATH) else "no"
    return ("📊 cloud_editor status\n"
            "brain: glm-5.3 boss + openrouter/free workers\n"
            f"feedback in queue: {dir_count(FEEDBACK_DIR)}\n"
            f"game orders in queue: {dir_count(ORDERS_DIR)}\n"
            f"deploys shipped: {state.get('deployed_count', 0)}\n"
            f"last action: {state.get('last_action', 'none')}\n"
            f"paused: {paused}")


# ------------------------------------------------------------ menu screens ---

def main_menu_screen():
    return "🤖 cloud_editor — what shall we do?", MAIN_MENU


def newgame_screen():
    rows = [[{"text": "🕹 arcade", "callback_data": "tpl:arcade"},
             {"text": "⚔ rpg", "callback_data": "tpl:rpg"},
             {"text": "🔤 eight", "callback_data": "tpl:eight"}],
            [{"text": "⬅️ Menu", "callback_data": "menu:main"}]]
    return ("🏭 Pick a template — arcade, RPG, or Eight Letters. "
            "I'll ask for a name and a vibe next:", rows)


def fix_screen():
    rows = [[{"text": "💬 Tell me what's broken", "callback_data": "menu:feedback"}],
            [{"text": "⬅️ Menu", "callback_data": "menu:main"}]]
    return ("🛠 Fix a Game — send plain text describing what's wrong (or tap 💬). "
            "Small art/number tweaks ship automatically; code changes ask you first.", rows)


def feedback_screen():
    rows = [[{"text": "⬅️ Menu", "callback_data": "menu:main"}]]
    return ("💬 Just type your feedback as a normal message — I'll triage it, write "
            "the patch, run the gate wall and ship it (code changes ask you first). "
            "Photos/voice you send get attached to it automatically.", rows)


def factory_screen(title):
    rows = [[{"text": "🎮 My Games", "callback_data": "menu:games"},
             {"text": "🏭 Another one", "callback_data": "menu:newgame"}],
            [{"text": "⬅️ Menu", "callback_data": "menu:main"}]]
    return ("🏭 Game factory spinning up — " + (title or "your game") +
            " is queued. You'll get a link here when it's live.", rows)


def games_for_player(index, player_id):
    """Pure: hub/games/index.json -> [{title, url}] owned by player_id. Liberal
    about the index shape ({"games": [...]}, a bare list, or an id-keyed map)."""
    games = index
    if isinstance(index, dict):
        games = index.get("games")
        if games is None:
            games = [v for v in index.values() if isinstance(v, dict)]
    if not isinstance(games, list):
        return []
    out = []
    for g in games:
        if not isinstance(g, dict):
            continue
        owner = g.get("player_id", g.get("player", g.get("owner", g.get("owner_id"))))
        if isinstance(owner, dict):
            owner = owner.get("id")
        if owner is None or str(owner) != str(player_id):
            continue
        url = g.get("url") or g.get("link") or g.get("play_url")
        title = g.get("title") or g.get("name") or g.get("slug") or "game"
        if url:
            out.append({"title": str(title), "url": str(url)})
    return out


def my_games(player_id):
    """(callback answer, message text, buttons) for 🎮 My Games: one url-type
    button per owned game (inline URLs need url buttons), the link text as the
    callback answer, or the no-games nudge."""
    try:
        with open(HUB_INDEX) as f:
            index = json.load(f)
    except (OSError, ValueError):
        index = None
    games = games_for_player(index, player_id)
    if not games:
        return ("no games yet — make one!", NO_GAMES_TEXT,
                [[{"text": "🏭 New Game", "callback_data": "menu:newgame"}],
                 [{"text": "⬅️ Menu", "callback_data": "menu:main"}]])
    rows = [[{"text": f"🎮 {g['title']}", "url": g["url"]}] for g in games]
    rows.append([{"text": "⬅️ Menu", "callback_data": "menu:main"}])
    answer = " · ".join(f"{g['title']}: {g['url']}" for g in games)[:190]
    return (answer, "🎮 Your games — tap one to play:", rows)


def deploy_buttons(order_id):
    return [[{"text": "🚀 Deploy", "callback_data": f"dep:yes:{order_id}"},
             {"text": "👀 Show diff", "callback_data": f"dep:diff:{order_id}"},
             {"text": "❌ Cancel", "callback_data": f"dep:no:{order_id}"}]]


# -------------------------------------------------------------- feedback LLM ---

def llm_boss(cfg, messages):
    """BOSS tier — glm-5.3 on the NVIDIA free tier: a reasoning model that can
    take minutes and may return content=null with only reasoning_content. Big
    budget, long timeout, and a final-answer nudge when the content comes back
    empty. Triages, reviews the worker drafts, owns the last word."""
    for nudge in range(2):
        data = api_post(LLM_URL, {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 0.3,
            "top_p": 0.9,
            "max_tokens": 4096,
            "chat_template_kwargs": {"thinking": False},
        }, token=cfg["nvapi_key"])
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
        if content.strip():
            return content
        reasoning = choice["message"].get("reasoning_content") or ""
        messages = messages + [
            {"role": "assistant", "content": reasoning[-1500:] or "(reasoning)"},
            {"role": "user", "content":
             "Output ONLY your final answer now (short reply and/or one fenced "
             "```diff block). No further reasoning."},
        ]
    return ""


# ------------------------------------------------------------- two-tier crew ---
# BOSS (llm_boss, glm-5.3) triages and reviews; WORKERS (llm_worker,
# openrouter/free) draft the code/data subtasks; the boss's review output is the
# diff that ships. A worker failure (401/429/timeout/no key) degrades silently:
# the boss drafts the subtask itself, and only the feedback entry meta records it.

def llm_worker(cfg, messages):
    """WORKER tier — openrouter/free via the OpenAI-compatible endpoint. Plain
    call: no chat_template_kwargs, no reasoning nudge. Returns "" on ANY failure
    (missing key, 401/429, timeout, malformed body); the caller treats that as
    'the boss drafts this subtask itself'."""
    if not cfg.get("openrouter_key"):
        return ""
    try:
        data = api_post(WORKER_URL, {
            "model": WORKER_MODEL,
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 4096,
        }, token=cfg["openrouter_key"], timeout=240)
        choice = (data.get("choices") or [{}])[0]
        return (choice.get("message") or {}).get("content") or ""
    except Exception as exc:  # noqa: BLE001 — any worker failure degrades to boss
        print("worker error:", exc, flush=True)
        return ""


def json_block_end(text, start):
    """Index just past the '}' matching the '{' at text[start], respecting
    strings and escapes; None when unbalanced (tolerant extractor plumbing)."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def extract_plan(reply):
    """First {...} JSON object in the boss's triage reply -> dict, or None. A
    fenced ```json block wins; else the first brace-balanced {...} anywhere in
    the text (so nested braces and braces inside strings survive)."""
    text = reply or ""
    candidates = []
    fence = re.search(r"```(?:json)?\s*(\{.*)", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start = text.find("{")
    if start != -1:
        end = json_block_end(text, start)
        if end is not None:
            candidates.append(text[start:end])
    for cand in candidates:
        # Trim to the balanced block: a fenced grab may carry trailing prose.
        end = json_block_end(cand, 0)
        if end is None:
            continue
        try:
            plan = json.loads(cand[:end])
        except ValueError:
            continue
        if isinstance(plan, dict):
            return plan
    return None


def validate_plan(plan):
    """Triage dict -> (reply, subtasks), or None when unusable. Subtasks are
    dropped unless kind is code|art|data with a non-empty instruction, capped
    at MAX_SUBTASKS, re-id'd 1..n; files_hint is coerced to a list of str."""
    if not isinstance(plan, dict):
        return None
    reply = plan.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None
    raw = plan.get("subtasks")
    if not isinstance(raw, list):
        raw = []
    subtasks = []
    for st in raw[:MAX_SUBTASKS]:
        if not isinstance(st, dict):
            continue
        kind = str(st.get("kind") or "").strip().lower()
        instruction = st.get("instruction")
        if kind not in PLAN_KINDS or not isinstance(instruction, str) \
                or not instruction.strip():
            continue
        hint = st.get("files_hint")
        if not isinstance(hint, list):
            hint = []
        subtasks.append({"id": len(subtasks) + 1, "kind": kind,
                         "instruction": instruction.strip(),
                         "files_hint": [str(f) for f in hint if f]})
    return reply.strip(), subtasks


def plan_tier(subtasks):
    """Pure: subtasks -> the tier the plan implies (T0 = none, any code file
    makes it T3, else data -> T2, else art -> T1)."""
    kinds = {st.get("kind") for st in subtasks}
    if not kinds:
        return "T0"
    if "code" in kinds:
        return "T3"
    if "data" in kinds:
        return "T2"
    return "T1"


def needs_boss_draft(draft):
    """The cost-guard decision: a blank/error worker reply (llm_worker returns ""
    on 401/429/timeout/no key) means the boss does the subtask itself. Silent to
    the player; the feedback entry meta carries the bookkeeping."""
    return not (draft or "").strip()


def merge_drafts(drafts):
    """Pure: worker drafts -> one combined patch for the boss to review."""
    return "\n".join(d.strip() for d in drafts if d and d.strip()).strip()


def review_decision(review_reply, merged_drafts):
    """Pure: boss review output + merged worker drafts -> the diff that proceeds.
    The reviewer owns the last word: its own diff wins; APPROVED without a diff
    ships the drafts as-is; anything else -> None (retry, then boss-only)."""
    diff = extract_diff(review_reply or "")
    if diff is not None and diff.strip():
        return diff.strip()  # the fence capture keeps a trailing newline
    if "APPROVED" in (review_reply or ""):
        return (merged_drafts or "").strip() or None
    return None


def worker_draft(cfg, subtask, facts):
    """One worker pass: a fenced diff for this subtask, or None when the worker
    flaked (then the boss drafts it — the silent single-tier fallback)."""
    hint = ", ".join(subtask.get("files_hint") or []) or "(no paths suggested)"
    reply = llm_worker(cfg, [
        {"role": "system", "content": WORKER_PROMPT},
        {"role": "user", "content":
            f"{facts}\n\nSubtask {subtask.get('id')}: {subtask.get('instruction')}\n"
            f"Likely files: {hint}\n"
            "Output ONE fenced ```diff block (git apply format, paths relative to "
            "the repo root) and nothing else."},
    ])
    if needs_boss_draft(reply):
        return None
    return extract_diff(reply)


def review_brief(subtasks, drafts, plan_reply):
    """The boss review prompt: every subtask with its worker draft (or a 'do it
    yourself' for art / flaked workers), and what APPROVE vs revise means."""
    lines = [f"Your plan told the player: {plan_reply!r}", "Worker drafts:"]
    for st, draft in zip(subtasks, drafts):
        if draft:
            lines.append(f"--- subtask {st['id']} ({st['kind']}) ---\n"
                         f"```diff\n{draft}\n```")
        else:
            lines.append(f"--- subtask {st['id']} ({st['kind']}) --- no worker "
                         "draft; implement it yourself.")
    lines.append("Review as the boss: if the drafts are correct and complete, "
                 "output APPROVED plus (optionally) one merged/cleaned ```diff "
                 "block; if not, output your own corrected FULL ```diff block "
                 "covering every subtask. Your diff is what ships.")
    return "\n".join(lines)


def run_gates(repo):
    fails = []
    for suite in GATE_SUITES:
        r = subprocess.run(
            [GODOT, "--headless", "--path", repo, "--script", f"res://tests/{suite}.gd"],
            capture_output=True, text=True, timeout=600)
        out = r.stdout + r.stderr
        m = re.search(r"Ran \d+ [^\n]*checks: (\d+) passed, (\d+) failed", out)
        if not m or int(m.group(2)) > 0 or r.returncode != 0:
            tail = "\n".join((out.splitlines() or ["<no output>"])[-12:])
            fails.append(f"{suite}:\n{tail}")
    return fails


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def extract_diff(reply):
    m = re.search(r"```diff\n(.*?)```", reply, re.DOTALL)
    return m.group(1) if m else None


def extract_message(reply):
    no_diff = re.sub(r"```diff\n.*?```", "", reply, flags=re.DOTALL).strip()
    return no_diff or "(patch attached)"


def diff_tier(diff):
    """T3 = code (asks first), T2 = data/numbers, T1 = art. Any code file in the
    diff makes the whole proposal T3; unknown/no paths default to T3 (safe)."""
    tier = "T1"
    seen = False
    for path in re.findall(r"^(?:---|\+\+\+) (\S+)", diff or "", re.MULTILINE):
        if path == "/dev/null":
            continue
        seen = True
        path = re.sub(r"^[ab]/", "", path)
        ext = os.path.splitext(path)[1].lower()
        if ext in TIER_CODE_EXTS:
            return "T3"
        if ext in TIER_DATA_EXTS:
            tier = "T2"
        elif ext not in TIER_ART_EXTS:
            return "T3"  # unknown extension -> treat as code, ask first
    return "T3" if not seen else tier


def deploy_web(repo):
    """Export Web and push ONLY the web files to the game's deploy branch
    (Pages serves it; main holds source — never clobber source with assets)."""
    godot = os.environ.get("GODOT_BIN") or GODOT
    r = subprocess.run([godot, "--headless", "--path", repo, "--export-release", "Web"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or "ERROR" in (r.stdout + r.stderr):
        return f"export failed: {(r.stdout + r.stderr)[-500:]}"
    src = os.path.join(repo, "build", "web")
    keep = ["index.html", "index.js", "index.pck", "index.wasm",
            "index.service.worker.js", "index.manifest.json",
            "index.png", "index.icon.png", "index.apple-touch-icon.png",
            "index.144x144.png", "index.180x180.png", "index.512x512.png",
            "index.audio.worklet.js", "index.audio.position.worklet.js"]
    scratch = "cloud_editor/deploy-staging"
    cleanup = [["checkout", "-q", "main"], ["branch", "-q", "-D", scratch]]
    git(repo, "checkout", "-q", "--orphan", scratch)
    git(repo, "rm", "-rf", "-q", "--cached", ".")
    for name in keep:
        s = os.path.join(src, name)
        if os.path.exists(s):
            with open(s, "rb") as a, open(os.path.join(repo, name), "wb") as b:
                b.write(a.read())
            git(repo, "add", "-f", name)
    rr = git(repo, "commit", "-q", "-m", "cloud_editor web deploy")
    rr = git(repo, "push", "-q", "-f", "origin", f"{scratch}:deploy")
    for args in cleanup:
        git(repo, *args)
    if rr.returncode != 0:
        return f"push deploy failed: {rr.stderr[-300:]}"
    return None


def discard_scratch(repo, branch):
    """dep:no / reset: drop the scratch branch, back to main. Best effort."""
    if not branch:
        return
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", branch)


def send_diff(cfg, chat_id, diff):
    """The full patch, split at 3900 chars per message."""
    for i in range(0, len(diff), DEPLOY_DIFF_SPLIT):
        head = "👀 the diff:\n" if i == 0 else "(cont)\n"
        tg_send(cfg, head + diff[i:i + DEPLOY_DIFF_SPLIT], chat_id=chat_id)


def handle_feedback(cfg, repo, text, meta, state=None, reply_chat=None, sender=None):
    """v4 two-tier loop. glm-5.3 (BOSS) triages the feedback into a tier + plan
    JSON; openrouter/free WORKERS draft one diff per code/data subtask; the boss
    reviews and either APPROVES the merged drafts or outputs its own corrected
    diff — the reviewer owns the last word, so the diff that ships is always the
    boss's. From there the v3 flow is unchanged: scratch branch -> gate wall ->
    T3 confirm buttons / T1-T2 auto-deploy. An unparseable plan or any worker
    failure (401/429/timeout/no key) degrades silently to the boss doing it all
    itself (v3 single-shot); only the feedback entry meta records the fallback."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    order_id = ts
    branch = f"{SCRATCH_BASE}-{order_id}"
    entry = {"time": ts, "text": text, "meta": meta}
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    entry_path = os.path.join(FEEDBACK_DIR, f"{ts}.json")
    with open(entry_path, "w") as f:
        json.dump(entry, f, indent=2)

    def entry_note(extra):
        """Audit-trail addenda (tier, crew/fallback bookkeeping) — never shown."""
        entry.update(extra)
        with open(entry_path, "w") as f:
            json.dump(entry, f, indent=2)

    # The owner review channel always sees the raw player request, tagged.
    if reply_chat is not None and str(reply_chat) != str(cfg.get("chat_id")):
        tg_send(cfg, f"{sender_tag(sender or {})} {text}")

    def note(action):
        if state is not None:
            state["last_action"] = action
            save_state(state)

    def player(msg):
        if reply_chat is not None:
            tg_reply(cfg, reply_chat, msg, sender)
        else:
            tg_send(cfg, msg)

    def run_loop(messages, reply=None, diff=None, proposal=None):
        return diff_loop(cfg, repo, state, reply_chat, player, note,
                         order_id, branch, ts, messages,
                         reply=reply, diff=diff, proposal=proposal)

    facts = (
        "Repo facts: Godot 4.7 GDScript; scripts/ (logic RefCounted classes: GameManager "
        "arena, GameEight + Rules8 + LetterSource, BattleManager + Progression + RpgConfig, "
        "WordDatabase with SCOWL tiers via set_tier common/standard/expert); scenes/; "
        "data/scowl_*.txt dictionaries; tests/ suites are the gate wall."
    )
    context = f"{facts} Player feedback: {text!r} (context: {json.dumps(meta)})"

    git(repo, "checkout", "-q", "-B", branch)

    # --- Tier 1: the BOSS triages -> tier + plan JSON ({"reply", "subtasks"}).
    triage_messages = [{"role": "system", "content": SYSTEM_PROMPT + TRIAGE_ADDENDUM},
                       {"role": "user", "content": context}]
    triage_reply = llm_boss(cfg, triage_messages)
    plan = validate_plan(extract_plan(triage_reply))
    if plan is None:
        # Tolerant fallback: the v3 single-shot path, boss only, seeded with the
        # wobbly triage output so the next ask nudges the format back on track.
        entry_note({"brain": "boss-only (plan JSON unparseable)"})
        return run_loop([{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": context},
                         {"role": "assistant", "content": triage_reply},
                         {"role": "user", "content": PLAN_NUDGE}])
    plan_reply, subtasks = plan
    entry_note({"brain": "boss+workers" if subtasks else "boss-only (T0)",
                "plan_tier": plan_tier(subtasks), "subtasks": len(subtasks)})
    if not subtasks:  # T0: a question/opinion — conversational reply, no diff
        player(f"📬 Feedback received — no code change needed.\n\n{plan_reply}")
        note(f"conversational reply ({ts})")
        git(repo, "checkout", "-q", "main")
        git(repo, "branch", "-q", "-D", branch)
        return

    # --- Tier 2: WORKERS draft the code/data subtasks; art stays with the boss.
    drafts = []
    flaked = 0
    for st in subtasks:
        if st["kind"] in ("code", "data"):
            draft = worker_draft(cfg, st, facts)  # None on 401/429/timeout/no key
            if draft is None:
                flaked += 1  # silent single-tier fallback for this subtask
        else:
            draft = None  # kind "art": the boss drafts it in review, by design
        drafts.append(draft)
    if flaked:
        entry_note({"worker_fallback":
                    f"{flaked}/{len(subtasks)} subtasks -> boss drafted itself"})
    merged = merge_drafts([d for d in drafts if d])

    # --- Tier 3: the BOSS reviews; its output is the diff that ships.
    review_messages = triage_messages + [
        {"role": "assistant", "content": triage_reply},
        {"role": "user", "content": review_brief(subtasks, drafts, plan_reply)},
    ]
    review_reply = llm_boss(cfg, review_messages)
    final_diff = review_decision(review_reply, merged)
    if final_diff is None:
        review_messages += [{"role": "assistant", "content": review_reply},
                            {"role": "user", "content": REVIEW_NUDGE}]
        review_reply = llm_boss(cfg, review_messages)
        final_diff = review_decision(review_reply, merged)
    if final_diff is None:
        # The reviewer never produced a usable diff — single-tier fallback: the
        # boss implements the whole thing itself, v3 style.
        entry_note({"brain": "boss-only (review never produced a diff)"})
        return run_loop(review_messages + [
            {"role": "assistant", "content": review_reply},
            {"role": "user", "content": PLAN_NUDGE}])
    return run_loop(review_messages, reply=review_reply, diff=final_diff,
                    proposal=plan_reply)


def diff_loop(cfg, repo, state, reply_chat, player, note, order_id, branch, ts,
              messages, reply=None, diff=None, proposal=None):
    """The v3 apply -> gates -> deploy retry loop, on a boss thread. Attempt 1
    uses the seeded reply/diff (v4: the boss review output); later attempts
    re-ask the boss with the failure pasted in. A reply with no diff means
    conversational — same exit as v3."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if diff is None or attempt > 1:
            reply = llm_boss(cfg, messages) or ""
            diff = extract_diff(reply)
        if diff is None:
            player(f"📬 Feedback received — no code change needed.\n\n{extract_message(reply or '')}")
            note(f"conversational reply ({ts})")
            git(repo, "checkout", "-q", "main")
            git(repo, "branch", "-q", "-D", branch)
            return
        git(repo, "checkout", "-q", "main")
        git(repo, "checkout", "-q", "-B", branch)
        applied = subprocess.run(["git", "-C", repo, "apply", "-"], input=diff,
                                 capture_output=True, text=True)
        if applied.returncode != 0:
            messages.append({"role": "assistant", "content": reply or ""})
            messages.append({"role": "user", "content":
                             f"git apply failed:\n{applied.stderr}\nRegenerate the diff cleanly."})
            continue
        fails = run_gates(repo)
        if not fails:
            tier = diff_tier(diff)
            proposal = proposal or extract_message(reply or "")
            if tier == "T3" and reply_chat is not None:
                # T3 code changes ask first — park the proposal in the chat's
                # session until a deploy button answers it. The scratch branch
                # name embeds the order id and persists until then.
                save_session(reply_chat, {
                    "state": "confirm_feedback",
                    "data": {"order_id": order_id, "branch": branch, "diff": diff,
                             "proposal": proposal, "chat_id": reply_chat,
                             "attempt": attempt},
                })
                tg_send_kb(cfg, reply_chat,
                           f"🛠 Proposal {order_id} (attempt {attempt}, gates green, "
                           f"T3 code change):\n{proposal}\n\nShip it?",
                           deploy_buttons(order_id))
                note(f"awaiting deploy confirm ({order_id})")
                return
            err = deploy_web(repo)
            if err is None:
                git(repo, "checkout", "-q", "main")
                git(repo, "merge", "-q", "--ff-only", branch)
                git(repo, "push", "-q", "origin", "main")
                player(f"✅ Deployed! {proposal}\n"
                       f"(attempt {attempt}; gates green; live in ~1 min)")
                if state is not None:
                    state["deployed_count"] = state.get("deployed_count", 0) + 1
                note(f"deployed from feedback ({ts})")
            else:
                player(f"⚠️ Gates green but deploy failed: {err}")
                note(f"deploy failed ({ts})")
            git(repo, "checkout", "-q", "main")
            git(repo, "branch", "-q", "-D", branch)
            return
        messages.append({"role": "assistant", "content": reply or ""})
        messages.append({"role": "user", "content":
                         "Gates failed:\n" + "\n\n".join(fails)[:2000] + "\nFix and regenerate the full diff."})

    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", branch)
    player("🛑 Couldn't implement this one automatically after "
           f"{MAX_ATTEMPTS} attempts — saved to the queue for human review.")
    note(f"gave up after {MAX_ATTEMPTS} attempts ({ts})")


# -------------------------------------------------------------- deployments ---

def deploy_callback(cfg, repo, state, intent, chat_id, sess):
    """dep:yes / dep:diff / dep:no against the pending confirm_feedback proposal."""
    action = intent["action"]
    order = intent.get("order")
    data = sess.get("data") or {}
    if sess.get("state") != "confirm_feedback" or not data.get("diff"):
        return ("Nothing is waiting on me right now.", *main_menu_screen())
    if order and data.get("order_id") and order != data["order_id"]:
        return ("That button belongs to an older proposal — use the buttons on "
                "the latest one.", None, None)
    order_id = data.get("order_id") or "?"
    branch = data.get("branch") or f"{SCRATCH_BASE}-{order_id}"

    if action == "diff":
        send_diff(cfg, chat_id, data["diff"])
        return ("Diff below 👇 (nothing shipped yet)", None, None)

    if action == "no":
        discard_scratch(repo, branch)
        save_session(chat_id, idle_session())
        if state is not None:
            state["last_action"] = f"deploy cancelled ({order_id})"
            save_state(state)
        return ("Cancelled — nothing shipped.",
                "❌ Cancelled — nothing shipped, scratch branch discarded.",
                MAIN_MENU)

    # action == "yes": re-apply the stored diff from scratch (the working tree
    # may have moved on), then the exact v2 export + merge + push path.
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-B", branch)
    applied = subprocess.run(["git", "-C", repo, "apply", "-"], input=data["diff"],
                             capture_output=True, text=True)
    if applied.returncode != 0:
        discard_scratch(repo, branch)
        save_session(chat_id, idle_session())
        return ("Patch no longer applies.",
                "⚠️ The stored patch no longer applies — proposal dropped. "
                "Send the feedback again?", MAIN_MENU)
    err = deploy_web(repo)
    if err is not None:
        discard_scratch(repo, branch)
        save_session(chat_id, idle_session())
        if state is not None:
            state["last_action"] = f"deploy failed ({order_id})"
            save_state(state)
        return ("Deploy failed.",
                f"⚠️ Gates were green but the deploy failed: {err}", MAIN_MENU)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", branch)
    git(repo, "push", "-q", "origin", "main")
    git(repo, "branch", "-q", "-D", branch)
    if state is not None:
        state["deployed_count"] = state.get("deployed_count", 0) + 1
        state["last_action"] = f"deployed via confirm ({order_id})"
        save_state(state)
    save_session(chat_id, idle_session())
    return ("Deployed!",
            "✅ deployed — live in ~1 min", MAIN_MENU)


# ---------------------------------------------------------------- callbacks ---

def menu_callback(cfg, repo, state, item, chat_id, sender):
    """Any menu button resets the chat's session, then rewrites the menu."""
    session_hard_reset(repo, chat_id)
    if item == "games":
        answer, text, buttons = my_games(sender.get("id"))
        return (answer, text, buttons)
    if item == "newgame":
        return ("Pick a template:", *newgame_screen())
    if item == "fix":
        return ("Tell me what's broken — I'll fix it.", *fix_screen())
    if item == "feedback":
        return ("Type it out — I'm listening.", *feedback_screen())
    if item == "status":
        return (None, status_report(state), MAIN_MENU)
    return (None, *main_menu_screen())


def dispatch_callback(cfg, repo, state, intent, chat_id, message_id, sender):
    """Returns (callback answer, message text, buttons); text None = don't rewrite
    the menu message (the handler already said what it had to say)."""
    kind = intent["kind"]
    if kind == "menu":
        return menu_callback(cfg, repo, state, intent.get("item"), chat_id, sender)

    sess = load_session(chat_id)
    if kind == "template":
        sess, ok = session_pick_template(sess, intent.get("template"))
        if not ok:
            return ("Unknown template — pick arcade, rpg or eight.", *main_menu_screen())
        save_session(chat_id, sess)
        return ("Template locked in.", "🏭 What should the game be called? "
                                       f"({TITLE_MIN}-{TITLE_MAX} characters — just type it)",
                [[{"text": "⬅️ Menu", "callback_data": "menu:main"}]])
    if kind == "vibe":
        sess, fields = session_pick_vibe(sess, intent.get("vibe"))
        if fields is None:
            return ("No game in progress — tap 🏭 New Game to start one.",
                    *main_menu_screen())
        extra = {"palette": fields["palette"]}
        if fields["logo_upload"]:
            extra["logo_upload"] = fields["logo_upload"]
        path = write_order(fields["template"], fields["title"], sender, chat_id,
                           extra=extra)
        save_session(chat_id, sess)
        if state is not None:
            state["last_action"] = (f"newgame order: {fields['template']}/"
                                    f"{fields['title']}")
            save_state(state)
        return ("Order queued!", *factory_screen(fields["title"]))
    if kind == "deploy":
        return deploy_callback(cfg, repo, state, intent, chat_id, sess)
    return ("Didn't catch that — here's the menu.", *main_menu_screen())


def handle_callback(cfg, repo, state, cq):
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    sender = sender_of(cq)
    intent = parse_callback(data)
    if chat_id is None:
        tg_answer(cfg, cq.get("id"), "I can only do that inside a chat.")
        return
    answer, text, buttons = dispatch_callback(cfg, repo, state, intent,
                                              chat_id, message_id, sender)
    tg_answer(cfg, cq.get("id"), answer)
    if text is not None:
        tg_edit_or_send(cfg, chat_id, message_id, text, buttons)


# ------------------------------------------------------------------ uploads ---

def classify_upload(msg):
    """message -> (kind, file_id, size, ext) or None when it carries no tracked
    file. kind: photo | voice | audio | zip | doc_other. Photos pick the largest
    size; documents must be .zip to be accepted (doc_other = politely refused)."""
    photo = msg.get("photo")
    if photo:
        best = max(photo, key=lambda p: ((p.get("width") or 0) * (p.get("height") or 0),
                                         p.get("file_size") or 0))
        return ("photo", best.get("file_id"), best.get("file_size") or 0, "jpg")
    if msg.get("voice"):
        v = msg["voice"]
        return ("voice", v.get("file_id"), v.get("file_size") or 0, "ogg")
    if msg.get("audio"):
        a = msg["audio"]
        ext = {"audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/wav": "wav",
               "audio/x-wav": "wav", "audio/mp4": "m4a"}.get(a.get("mime_type") or "",
                                                             "mp3")
        return ("audio", a.get("file_id"), a.get("file_size") or 0, ext)
    doc = msg.get("document")
    if doc:
        name = doc.get("file_name") or ""
        if doc.get("mime_type") == "application/zip" or name.lower().endswith(".zip"):
            return ("zip", doc.get("file_id"), doc.get("file_size") or 0, "zip")
        return ("doc_other", doc.get("file_id"), doc.get("file_size") or 0,
                os.path.splitext(name)[1].lstrip(".").lower() or "bin")
    return None


def upload_dest(chat_id, content, ext, uploads_dir=UPLOADS_DIR):
    """uploads/<chat_id>/<md5>.<ext> for the raw bytes."""
    digest = hashlib.md5(content).hexdigest()
    return os.path.join(uploads_dir, str(chat_id), f"{digest}.{ext}")


def route_upload(kind, session_state):
    """Context-aware routing: a photo inside a newgame session becomes the game
    logo; everything else is saved and attached to the next feedback message."""
    if kind == "photo" and session_state in NEWGAME_STATES:
        return "logo"
    return "attach"


def handle_upload(cfg, repo, state, msg, chat_id, sender):
    classified = classify_upload(msg)
    if not classified or chat_id is None:
        return
    if os.path.exists(PAUSED_PATH):
        tg_reply(cfg, chat_id, "⏸ Paused — nothing is being processed right now. "
                              "Send /resume to wake me.", sender)
        return
    kind, file_id, size, ext = classified
    if kind == "doc_other":
        tg_reply(cfg, chat_id, "🚫 I can only take photos, voice, audio and .zip "
                              "files — that one isn't.", sender)
        return
    if size > MAX_UPLOAD_BYTES:
        tg_reply(cfg, chat_id, "🚫 That file is over 10 MB — send a smaller one.", sender)
        return
    try:
        info = tg_get_file(cfg, file_id)
        content = tg_download(cfg, info.get("file_path") or "")
    except Exception as exc:  # noqa: BLE001 — Telegram/file errors go back to the chat
        tg_reply(cfg, chat_id, f"⚠️ Couldn't fetch that file: {exc}", sender)
        return
    if len(content) > MAX_UPLOAD_BYTES:
        tg_reply(cfg, chat_id, "🚫 That file is over 10 MB — send a smaller one.", sender)
        return
    dest = upload_dest(chat_id, content, ext)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)

    sess = load_session(chat_id)
    if route_upload(kind, sess.get("state")) == "logo":
        sess["data"]["logo"] = dest
        save_session(chat_id, sess)
        where = ("now pick the vibe" if sess.get("state") == "newgame_title"
                 else "now tell me the game's title")
        tg_reply(cfg, chat_id, f"🖼 Saved as your game logo — {where}.", sender)
        if state is not None:
            state["last_action"] = f"logo upload ({dest})"
            save_state(state)
        return

    sess["data"].setdefault("pending_attachments", []).append(dest)
    sess["data"]["last_upload"] = dest
    save_session(chat_id, sess)
    if kind == "voice":
        write_voice_order(dest, chat_id)
        tg_reply(cfg, chat_id, "🎙 Voice saved — transcription queued", sender)
    else:
        tg_reply(cfg, chat_id, "📦 Got your file — tell me what it's for in a reply",
                 sender)
    if state is not None:
        state["last_action"] = f"upload saved ({dest})"
        save_state(state)


# ------------------------------------------------------------------ routing ---

def handle_message(cfg, repo, state, msg):
    text = (msg.get("text") or "").strip()
    chat_id = msg.get("chat", {}).get("id")
    sender = sender_of(msg)
    if not text:
        handle_upload(cfg, repo, state, msg, chat_id, sender)
        return
    intent = parse_intent(text)
    kind = intent["kind"]

    # Paused: acknowledge everything, act on nothing except /resume.
    if os.path.exists(PAUSED_PATH) and kind != "resume":
        tg_reply(cfg, chat_id, "⏸ Paused — nothing is being processed right now. "
                              "Send /resume to wake me.", sender)
        return

    if kind == "feedback":
        sess = load_session(chat_id)
        if sess.get("state") in NEWGAME_STATES:
            # Natural text inside a newgame session is the title, not feedback.
            sess, action = session_on_text(sess, text)
            if action == "ask_vibe":
                save_session(chat_id, sess)
                tg_reply(cfg, chat_id, f"📌 '{sess['data']['title']}' — lovely. "
                                      "Now pick a vibe:", sender, buttons=VIBE_BUTTONS)
            else:
                tg_reply(cfg, chat_id, f"Titles are {TITLE_MIN}-{TITLE_MAX} "
                                      "characters — try another "
                                      "(or tap ⬅️ Menu to bail).", sender)
            return
        if sess.get("state") == "confirm_feedback":
            tg_reply(cfg, chat_id, "⚠️ A deploy is still waiting on you — tap "
                                  "🚀 Deploy or ❌ Cancel on the proposal first.", sender)
            return
        meta = {"from": sender}
        atts = (sess.get("data") or {}).get("pending_attachments") or []
        if atts:
            meta["attachments"] = atts
            save_session(chat_id, idle_session())
        tg_reply(cfg, chat_id, "👀 cloud_editor is on it…", sender)
        handle_feedback(cfg, repo, intent["text"], meta,
                        state=state, reply_chat=chat_id, sender=sender)
    elif kind == "login":
        session_hard_reset(repo, chat_id)
        tg_reply(cfg, chat_id,
                 f"👋 Welcome, {sender.get('name', 'player')} — you're talking to the "
                 f"Game Making Pipeline.\nHub: {HUB_URL}\n"
                 "Make your own game with /newgame <arcade|rpg|eight> <title>, "
                 "or just send me feedback — or tap a button below.", sender,
                 buttons=MAIN_MENU)
    elif kind == "newgame":
        sess = load_session(chat_id)
        if sess.get("state") in NEWGAME_STATES:
            save_session(chat_id, idle_session())
        write_order(intent["template"], intent["title"], sender, chat_id)
        state["last_action"] = f"newgame order: {intent['template']}/{intent['title']}"
        save_state(state)
        tg_reply(cfg, chat_id,
                 "🏭 Game factory spinning up — you'll get a link here when it's live",
                 sender)
    elif kind == "error":
        tg_reply(cfg, chat_id, intent["message"], sender)
    elif kind == "stop":
        with open(PAUSED_PATH, "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        state["last_action"] = "paused by /stop"
        save_state(state)
        tg_reply(cfg, chat_id, "⏸ Paused — I'll idle until you send /resume.", sender)
    elif kind == "resume":
        if os.path.exists(PAUSED_PATH):
            os.remove(PAUSED_PATH)
        state["last_action"] = "resumed"
        save_state(state)
        tg_reply(cfg, chat_id, "▶️ Resumed — send me feedback or /newgame.", sender)
    elif kind == "status":
        tg_reply(cfg, chat_id, status_report(state), sender)
    else:
        session_hard_reset(repo, chat_id)
        tg_reply(cfg, chat_id, MENU_TEXT, sender, buttons=MAIN_MENU)


def poll_once(cfg, repo, state):
    """One getUpdates batch. Returns True if any updates came back."""
    data = tg_poll(cfg, state["last_update_id"])
    updates = data.get("result", [])
    for upd in updates:
        state["last_update_id"] = upd["update_id"]
        save_state(state)
        cq = upd.get("callback_query")
        if cq:
            try:
                handle_callback(cfg, repo, state, cq)
            except Exception as exc:  # noqa: BLE001 — one bad callback must not kill the batch
                print("callback error:", exc, flush=True)
            continue
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue
        try:
            handle_message(cfg, repo, state, msg)
        except Exception as exc:  # noqa: BLE001 — one bad message must not kill the batch
            print("message error:", exc, flush=True)
    return bool(updates)


def run_once(cfg, repo, state):
    """Actions mode: drain every pending update, then exit 0."""
    if os.path.exists(PAUSED_PATH):
        print("paused.flag present — exiting without processing", flush=True)
        return
    for _ in range(20):  # bounded: Telegram pages at most 100 updates each
        if not poll_once(cfg, repo, state):
            break


# ----------------------------------------------------------------- selftest ---

def selftest():
    """Offline: intent + callback parsing, the session FSM, upload routing and
    classification, deploy tiering, hub-index filtering, session round-trip.
    No network calls."""
    failures = []

    def check(label, ok, detail=""):
        print(f"  {label}: {'OK' if ok else 'FAIL ' + detail}")
        if not ok:
            failures.append(f"{label}: {detail}")

    cases = [
        ("/start login", {"kind": "login"}),
        ("/start", {"kind": "login"}),
        ("/start newgame-arcade-My%20Title",
         {"kind": "newgame", "template": "arcade", "title": "My Title"}),
        ("/start newgame-eight-Star%20Vault",
         {"kind": "newgame", "template": "eight", "title": "Star Vault"}),
        ("/newgame rpg Dungeon of Doom",
         {"kind": "newgame", "template": "rpg", "title": "Dungeon of Doom"}),
        ("/newgame arcade Ab", {"kind": "error"}),
        ("/newgame eight " + "Z" * 41, {"kind": "error"}),
        ("/newgame nope Somewhere", {"kind": "error"}),
        ("/feedback the dice feel rigged",
         {"kind": "feedback", "text": "the dice feel rigged"}),
        ("the dice feel rigged", {"kind": "feedback", "text": "the dice feel rigged"}),
        ("/stop", {"kind": "stop"}),
        ("/resume", {"kind": "resume"}),
        ("/status", {"kind": "status"}),
        ("/bogus stuff", {"kind": "unknown"}),
    ]
    for text, want in cases:
        got = parse_intent(text)
        for key, val in want.items():
            if got.get(key) != val:
                failures.append(f"{text!r}: want {key}={val!r}, got {got.get(key)!r}")
        print(f"  {text!r:52} -> {got}")

    os.environ["CHAT_ID"] = "env-chat-id"
    cfg = load_config()
    env_ok = cfg["chat_id"] == "env-chat-id"
    del os.environ["CHAT_ID"]
    print(f"  config env precedence (CHAT_ID): {'OK' if env_ok else 'FAIL'}")
    if not env_ok:
        failures.append("env CHAT_ID did not override config.json")

    # v4: worker-tier key plumbing (env OPENROUTER_KEY wins)
    os.environ["OPENROUTER_KEY"] = "env-or-key"
    cfg = load_config()
    or_ok = cfg["openrouter_key"] == "env-or-key"
    del os.environ["OPENROUTER_KEY"]
    print(f"  config env precedence (OPENROUTER_KEY): {'OK' if or_ok else 'FAIL'}")
    if not or_ok:
        failures.append("env OPENROUTER_KEY did not land in cfg")

    # v3: callback-data parsing
    cb_cases = [
        ("dep:yes", {"kind": "deploy", "action": "yes", "order": None}),
        ("dep:diff", {"kind": "deploy", "action": "diff", "order": None}),
        ("dep:no", {"kind": "deploy", "action": "no", "order": None}),
        ("dep:yes:20260921-101010",
         {"kind": "deploy", "action": "yes", "order": "20260921-101010"}),
        ("dep:diff:abc-123", {"kind": "deploy", "action": "diff", "order": "abc-123"}),
        ("dep:no:o9", {"kind": "deploy", "action": "no", "order": "o9"}),
        ("menu:games", {"kind": "menu", "item": "games"}),
        ("menu:newgame", {"kind": "menu", "item": "newgame"}),
        ("menu:fix", {"kind": "menu", "item": "fix"}),
        ("menu:feedback", {"kind": "menu", "item": "feedback"}),
        ("menu:status", {"kind": "menu", "item": "status"}),
        ("menu", {"kind": "menu", "item": "main"}),
        ("tpl:arcade", {"kind": "template", "template": "arcade"}),
        ("tpl:rpg", {"kind": "template", "template": "rpg"}),
        ("tpl:eight", {"kind": "template", "template": "eight"}),
        ("vibe:bright", {"kind": "vibe", "vibe": "bright"}),
        ("vibe:cozy", {"kind": "vibe", "vibe": "cozy"}),
        ("vibe:dark", {"kind": "vibe", "vibe": "dark"}),
        ("vibe:surprise", {"kind": "vibe", "vibe": "surprise"}),
        ("garbage", {"kind": "unknown"}),
        ("dep:maybe", {"kind": "unknown"}),
        ("tpl:roguelike", {"kind": "template", "template": "roguelike"}),
    ]
    for data, want in cb_cases:
        got = parse_callback(data)
        for key, val in want.items():
            if got.get(key) != val:
                failures.append(f"cb {data!r}: want {key}={val!r}, got {got.get(key)!r}")
        print(f"  cb {data!r:34} -> {got}")

    # v3: the newgame session FSM (happy path, rejections, reset)
    tmp = tempfile.mkdtemp(prefix="cloud_editor_selftest_")
    try:
        sess = load_session("42", tmp)
        check("session default idle", sess == {"state": "idle", "data": {}}, repr(sess))
        sess, ok = session_pick_template(sess, "rpg")
        check("tpl pick -> newgame_template",
              ok and sess["state"] == "newgame_template"
              and sess["data"]["template"] == "rpg", repr(sess))
        sess, bad = session_pick_template(sess, "roguelike")
        check("bad template rejected", not bad and sess["state"] == "newgame_template",
              repr((bad, sess)))
        sess, act = session_on_text(sess, "ab")
        check("short title rejected", act == "bad_title"
              and sess["state"] == "newgame_template", repr((act, sess)))
        sess, act = session_on_text(sess, "Z" * 41)
        check("long title rejected", act == "bad_title"
              and sess["state"] == "newgame_template", repr((act, sess)))
        sess, act = session_on_text(sess, "Star Vault")
        check("title accepted -> newgame_title (awaiting vibe)",
              act == "ask_vibe" and sess["state"] == "newgame_title"
              and sess["data"]["title"] == "Star Vault", repr((act, sess)))
        sess, act = session_on_text(sess, "Renamed Star")
        check("late text re-sets the title", act == "ask_vibe"
              and sess["data"]["title"] == "Renamed Star", repr((act, sess)))
        sess2, fields = session_pick_vibe(sess, "cozy")
        check("vibe -> order fields + session idle",
              sess2["state"] == "idle"
              and fields == {"template": "rpg", "title": "Renamed Star",
                             "palette": VIBES["cozy"], "logo_upload": None},
              repr((sess2, fields)))
        sess3, fields3 = session_pick_vibe({"state": "newgame_title", "data": {}}, "dark")
        check("vibe without template/title rejected", fields3 is None, repr(fields3))
        sess4, fields4 = session_pick_vibe(sess, "nope")
        check("unknown vibe rejected", fields4 is None, repr(fields4))
        sess["state"] = "newgame_title"
        sess = session_reset(sess)
        check("reset -> idle with empty data", sess == {"state": "idle", "data": {}},
              repr(sess))
        sess["data"] = {"title": "x"}
        sess = session_reset(sess)
        check("reset drops data", sess["data"] == {}, repr(sess))

        # v3: session file round-trip in a temp dir
        pending = {"state": "confirm_feedback",
                   "data": {"order_id": "20260921-101010",
                            "branch": "cloud_editor/scratch-20260921-101010",
                            "diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
                            "proposal": "(patch attached)"}}
        save_session("42", pending, tmp)
        back = load_session("42", tmp)
        check("session round-trip (confirm_feedback)", back == pending, repr(back))
        check("session file lives in the sessions dir",
              os.path.exists(os.path.join(tmp, "42.json")))
        with open(os.path.join(tmp, "nope.json"), "w") as f:
            f.write("{not json")
        check("corrupt session file -> idle", load_session("nope", tmp) ==
              {"state": "idle", "data": {}}, repr(load_session("nope", tmp)))

        # v3: upload routing + classification
        check("photo in newgame_title -> logo",
              route_upload("photo", "newgame_title") == "logo")
        check("photo in newgame_template -> logo",
              route_upload("photo", "newgame_template") == "logo")
        check("photo idle -> attach", route_upload("photo", "idle") == "attach")
        check("photo in confirm_feedback -> attach",
              route_upload("photo", "confirm_feedback") == "attach")
        check("voice never becomes a logo",
              route_upload("voice", "newgame_title") == "attach")
        kind, fid, size, ext = classify_upload(
            {"photo": [{"file_id": "small", "width": 100, "height": 100},
                       {"file_id": "big", "width": 1280, "height": 960,
                        "file_size": 200000}]})
        check("photo picks the largest size",
              (kind, fid, ext) == ("photo", "big", "jpg") and size == 200000,
              repr((kind, fid, size, ext)))
        k2 = classify_upload({"document": {"file_id": "z", "file_name": "pack.zip",
                                           "mime_type": "application/zip",
                                           "file_size": 5}})
        check("zip document accepted", k2[0] == "zip" and k2[3] == "zip", repr(k2))
        k3 = classify_upload({"document": {"file_id": "p", "file_name": "notes.pdf"}})
        check("pdf document refused (doc_other)", k3[0] == "doc_other", repr(k3))
        check("voice classified ogg",
              classify_upload({"voice": {"file_id": "v", "file_size": 9}})
              == ("voice", "v", 9, "ogg"),
              repr(classify_upload({"voice": {"file_id": "v", "file_size": 9}})))
        check("sticker ignored", classify_upload({"sticker": {"file_id": "s"}}) is None)
        dest = upload_dest("42", b"hello", "jpg", tmp)
        check("upload path is <md5>.<ext> under uploads/<chat_id>",
              os.path.basename(dest) == hashlib.md5(b"hello").hexdigest() + ".jpg"
              and os.sep + "42" + os.sep in dest, repr(dest))

        # v3: deploy tiering (T3 asks first; T1/T2 auto-ship)
        code_diff = ("--- a/scripts/game_manager.gd\n+++ b/scripts/game_manager.gd\n"
                     "@@ -1,2 +1,3 @@\n x\n+y")
        check(".gd diff -> T3 (asks first)", diff_tier(code_diff) == "T3",
              diff_tier(code_diff))
        scene_diff = "--- a/scenes/menu.tscn\n+++ b/scenes/menu.tscn"
        check(".tscn diff -> T3", diff_tier(scene_diff) == "T3", diff_tier(scene_diff))
        data_diff = ("--- a/data/scowl_standard.txt\n+++ b/data/scowl_standard.txt\n"
                     "@@ -1 +1,2 @@\n a\n+b")
        check("data diff -> T2 (auto-ships)", diff_tier(data_diff) == "T2",
              diff_tier(data_diff))
        art_diff = "--- a/assets/ball.png\n+++ b/assets/ball.png"
        check("art diff -> T1 (auto-ships)", diff_tier(art_diff) == "T1",
              diff_tier(art_diff))
        mixed_diff = code_diff + "\n" + art_diff
        check("any code file makes the whole diff T3",
              diff_tier(mixed_diff) == "T3", diff_tier(mixed_diff))
        check("pathless diff defaults to T3 (safe)", diff_tier("not a diff") == "T3")
        check("deploy buttons carry the order id",
              deploy_buttons("o1") == [[
                  {"text": "🚀 Deploy", "callback_data": "dep:yes:o1"},
                  {"text": "👀 Show diff", "callback_data": "dep:diff:o1"},
                  {"text": "❌ Cancel", "callback_data": "dep:no:o1"}]],
              repr(deploy_buttons("o1")))

        # v3: hub index filtering (My Games)
        index = {"games": [
            {"title": "Star Vault", "url": "https://x/sv", "player_id": 7},
            {"title": "Not Mine", "url": "https://x/nm", "player_id": 8},
            {"title": "Also Mine", "url": "https://x/am", "player": "7"},
        ]}
        mine = games_for_player(index, 7)
        check("hub index filters by player id",
              [g["title"] for g in mine] == ["Star Vault", "Also Mine"], repr(mine))
        check("empty index -> no games", games_for_player({}, 7) == [])
        check("garbage index -> no games", games_for_player(None, 7) == [])
        games = games_for_player(index, 7)
        check("my-games buttons are url-type",
              all("url" in g for g in games) and games[0]["url"] == "https://x/sv",
              repr(games))

        # v4: plan-JSON extraction (tolerant: fenced, nested braces, prose around)
        plan_json = ('{"reply": "on it", "subtasks": [{"id": 1, "kind": "code", '
                     '"instruction": "nerf the dice", "files_hint": '
                     '["scripts/game_manager.gd"]}]}')
        plan = extract_plan(f"T3 code.\n```json\n{plan_json}\n```\nnothing else")
        check("plan json inside a fence parses",
              isinstance(plan, dict) and plan["reply"] == "on it", repr(plan))
        braced = ('{"reply": "a {braced} \\"thing\\"", "subtasks": '
                  '[{"id": 1, "kind": "data", "instruction": "x{}y", '
                  '"files_hint": []}]}')
        plan = extract_plan("Sure — tier T2. " + braced + " and prose after } end")
        check("plan json with nested braces + surrounding prose parses",
              plan and plan["subtasks"][0]["instruction"] == "x{}y", repr(plan))
        check("no plan json -> None", extract_plan("just talk, no braces") is None)
        check("unbalanced braces -> None", extract_plan('{"reply": "oops"') is None)
        check("empty object parses but is unusable",
              extract_plan("{}") == {} and validate_plan({}) is None)

        # v4: subtask validation (kind whitelist, cap 3, junk dropped)
        reply, subs = validate_plan({"reply": "ok", "subtasks": [
            {"id": 1, "kind": "code", "instruction": "a", "files_hint": ["s/x.gd"]},
            {"id": 2, "kind": "paint", "instruction": "b"},   # bad kind
            {"id": 3, "kind": "DATA", "instruction": "c"},    # case-insensitive
            {"id": 4, "kind": "art", "instruction": "   "},   # blank instruction
            "junk",                                           # not a dict
        ]})
        check("subtasks filtered to code/art/data (case-insensitive)",
              reply == "ok" and [s["kind"] for s in subs] == ["code", "data"],
              repr((reply, subs)))
        check("files_hint coerced to a list of str",
              subs[0]["files_hint"] == ["s/x.gd"], repr(subs[0]))
        many = [{"id": n, "kind": "code", "instruction": f"task {n}"}
                for n in range(1, 6)]
        _, capped = validate_plan({"reply": "go", "subtasks": many})
        check("subtasks capped at 3 and re-id'd",
              [s["id"] for s in capped] == [1, 2, 3], repr(capped))
        r2, s2 = validate_plan({"reply": "hi", "subtasks": "junk"})
        check("junk subtasks field -> empty list, reply kept",
              r2 == "hi" and s2 == [], repr((r2, s2)))
        check("missing reply -> plan invalid", validate_plan({"subtasks": []}) is None)
        check("non-dict plan invalid", validate_plan([1, 2]) is None)
        check("tier implied by subtask kinds",
              plan_tier([]) == "T0"
              and plan_tier([{"kind": "data"}]) == "T2"
              and plan_tier([{"kind": "art"}]) == "T1"
              and plan_tier([{"kind": "data"}, {"kind": "code"}]) == "T3")

        # v4: cost-guard fallback (worker error -> boss drafts, silently)
        check("blank worker reply -> boss drafts", needs_boss_draft("") is True)
        check("whitespace reply -> boss drafts", needs_boss_draft("  \n ") is True)
        check("real worker reply -> no fallback",
              needs_boss_draft("```diff\n--- a/x\n+++ b/x\n```") is False)
        check("worker without a key -> None draft (boss takes over)",
              worker_draft({}, {"id": 1, "kind": "code", "instruction": "x",
                                "files_hint": []}, "facts") is None)
        check("llm_worker without a key -> '' (no network, no raise)",
              llm_worker({}, []) == "")
        check("merge_drafts joins and skips empties",
              merge_drafts(["--- a/x\n", None, "  ", "+++ b/x"]) == "--- a/x\n+++ b/x")
        check("merge_drafts all-empty -> ''", merge_drafts([None, ""]) == "")
        gd = "--- a/scripts/x.gd\n+++ b/scripts/x.gd\n@@ -1 +1 @@\n-a\n+b"
        check("review: the boss's own diff wins",
              review_decision(f"revised:\n```diff\n{gd}\n```", "worker patch") == gd)
        check("review: APPROVED ships the drafts as-is",
              review_decision("APPROVED — clean.", "worker patch") == "worker patch")
        check("review: APPROVED + own diff -> own diff",
              review_decision(f"APPROVED after cleanup\n```diff\n{gd}\n```", "w") == gd)
        check("review: waffle -> None (boss redoes it)",
              review_decision("hmm, let me think", "worker patch") is None)
        check("review: APPROVED with nothing to ship -> None",
              review_decision("APPROVED", "") is None)
        check("status line names the two-tier brain",
              "brain: glm-5.3 boss + openrouter/free workers"
              in status_report({"deployed_count": 2}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("SELFTEST FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"SELFTEST PASS ({len(cases)} intent cases, {len(cb_cases)} callback cases, "
          "FSM + uploads + tiers + hub index + session round-trip + crew/plan/review)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="cloud_editor — feedback->implement->deploy harness")
    ap.add_argument("--once", action="store_true",
                    help="process all pending updates then exit 0 (Actions mode)")
    ap.add_argument("--selftest", action="store_true",
                    help="exercise parsing, the session FSM and upload routing "
                         "offline (no network) and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    cfg = load_config()
    repo = cfg.get("repo_dir", REPO_DEFAULT)
    state = load_state()
    mode = "once" if args.once else "polling"
    print(f"cloud_editor running — {mode} Telegram")
    if not cfg.get("bot_token"):
        print("no bot token (set TG_TOKEN or config.json) — cannot poll", flush=True)
        return 1
    while True:
        try:
            if args.once:
                run_once(cfg, repo, state)
                return 0
            poll_once(cfg, repo, state)
        except Exception as exc:  # noqa: BLE001 — a poll loop must never die
            print("loop error:", exc, flush=True)
            if args.once:
                return 0
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
