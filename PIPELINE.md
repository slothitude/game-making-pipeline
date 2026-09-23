# THE GAME MAKING PIPELINE (GMP) — Complete Form

*The canonical definition. Written 2026-09-23. Supersedes scattered plan notes.*

One sentence: **an autonomous game factory where AI agents design, build, test, ship, and critique phone-first games — with humans steering from a chat window.**

---

## 1. The Machines (the law)

| Machine | Role | Runs |
|---|---|---|
| **ROG** (Windows desktop) | 🧠 **The brain ONLY** | Claude Code + `/gmp` skill, PlayerOne training, Flux art gen, haiku subagent fleet |
| **GITHUB ACTIONS** | ⚙️ **The builder** | Godot headless builds, gate walls, CI deploys, daily remake cron |
| **RETROMONKEY** (Oracle Cloud, retromonkey.com.au) | 🖥 **The body** | Caddy site + games, chat/signal, agent daemon (systemd `gmp-agent`), PostgreSQL, Android emulator + PlayerOne inference |
| **LAPPY** (RTX 3060) | 🎨 **The GPU, on call** | TRELLIS.2 3D, Blender renders, ComfyUI, heavyweight inference |
| **TELEGRAM** | 📱 **The front door** | Auth + dispatch + human feedback |

**Nothing heavy ever runs on Rog.** Rog thinks; Actions builds; retromonkey serves.

## 2. The Loop (the complete form)

```
        ┌──────────────── INPUTS ────────────────┐
        │ Telegram · site chat · itch comments · │
        │ daily cron · PlayerOne critiques       │
        └───────────────────┬────────────────────┘
                            ▼
                   ┌───────────────┐
                   │  THE BRAIN    │  Rog /gmp or the daemon on retromonkey
                   │  (3-deep LLM  │  glm-5.3 → kimi-k3 → openrouter/free
                   │   chain)      │  plans the work, writes work-orders
                   └───────┬───────┘
                            ▼
                   ┌───────────────┐
                   │  THE BUILDER  │  GitHub Actions / daemon
                   │               │  milestone builds (Godot 4.7.1),
                   └───────┬───────┘  art work-orders (Flux + PIL keying),
                            ▼         tunable work-orders (T1 lane)
                   ┌───────────────┐
                   │  THE GATES    │  2× CLEAR LAW: every tests/*.gd suite
                   │               │  green twice via pinned Godot headless.
                   └───────┬───────┘  Red = auto-revert, nothing ships.
                            ▼
                   ┌───────────────┐
                   │  THE DEPLOY   │  retromonkey /games/<slug>/ (live on
                   │               │  scp) + itch.io via butler/Playwright
                   └───────┬───────┘  + cover art on every page
                            ▼
                   ┌───────────────┐
                   │  THE CRITIC   │  PlayerOne: jevlike model PLAYS the game
                   │               │  (100% top-1) → measured evidence →
                   │               │  llama-3.2-11b VISION judges screenshots
                   │               │  (seconds) → LLM ladder writes the
                   └───────┬───────┘  nuanced critique
                            ▼
                   ┌───────────────┐
                   │  THE LEDGER   │  Every green milestone → DIARY.md +
                   │               │  LORE.md (in-world artifacts) + Flux
                   │               │  pencil sketches. The site shows it all.
                   └───────┬───────┘
                            │
                            └──── work-orders filed ────▶ back to THE BRAIN
```

**The loop closes**: critiques and player feedback become work-orders; work-orders become builds; builds ship; new critiques arrive. The factory runs itself; humans steer.

## 3. The Fronts (how humans touch it)

- **https://retromonkey.com.au/** — the Slothitude Games site: games, live pipeline dashboard, schedule, action queue, todo list, diaries, chat. Guests play games; logged-in users (Telegram auth) get chat + multiplayer; Mr Slothitude is admin and can talk to any agent.
- **Site chat → work-orders**: "make the timer longer" files a T1 tunable order; "change the tiles to pink" files a T2 art order. PlayerOne keyword-matcher infers the game, clamps to range, the handler rewrites the const, runs the gate wall, auto-reverts on red, deploys on green.
- **itch.io** — published games + cover art + asset pages (no create-API; Playwright with the Cloudflare-defeating Chrome bridge).
- **Telegram** — the bot is the dispatch layer; the chat IS the protocol between all agents.

## 4. The Critic (PlayerOne, complete)

Three tiers, one verdict:

1. **Plays** — a jevlike one-pass option scorer, retrained per game (v1, sonar, arcade at 100% top-1; slime at 80%), on the retromonkey emulator (real Android, adb-driven) or Playwright (phone viewport, HTML5 fast lane).
2. **Sees** — `vision_critic.py`: llama-3.2-11b-vision-instruct scores screenshots on fun / polish / readability / phone UX / ADHD-friendly, in seconds. Handles JSON and markdown output, rescales 0–1 decimals.
3. **Judges** — `llm_critic.py`: the 3-deep ladder turns measured evidence + visual scores into loves/issues/work-orders.

## 5. The Fleet (the games)

**Shipped**: OCTOGRAM ARCADE (3-in-1 for Tash: Word Poker + RPG + eight-letters), SONAR (TFS jam), SLIME LINE (Humboldt jam).
**In the mill**: STAR VISITOR (slice C), GYRO SQUADRON '45 (M3 done), CUBEFALL (3D Kurushi study, M1 queued).
**Backlog**: 24-game remake queue, one per day via the daily remake cron.

## 6. The Stack (the parts list)

Godot 4.7.1 (pinned) · SCOWL dictionaries (common/standard/expert tiers, shared across all games) · Flux.1-dev + PIL magic-wand keying · jevlike tiny-nets · llama-3.2 vision · PostgreSQL (gmp/pipeline) · Caddy + Docker + systemd · butler + Playwright · Android emulator w/ KVM · Telegram Bot API · WebSocket signaling (multiplayer, v2).

## 7. The Laws

1. Rog is brain only. Actions is the builder. retromonkey is the body.
2. Nothing ships without 2× clear gates.
3. Every green milestone is written into the diary.
4. The chat is the protocol — every agent is a user in the room.
5. Phone-first, ADHD-friendly, Tash is the north star.
6. Red gates auto-revert. The factory never ships a broken build.
7. The loop must close: feedback → work-order → build → deploy → critique.
8. **The brains are the big NVIDIA models** (glm-5.3 → kimi-k3 today; the ladder refresh promotes the strongest the catalog offers). **Workers are the openrouter/free endpoint** — the boss delegates every build/draft subtask to them. Free rungs (Gemini) only catch failures. The ladder (`daily/ladder.json`) refreshes on retromonkey every 7 days.
9. **Everything runs on the server through THE QUEUE.** Postgres-backed jobs table; one job at a time locally (serialization is how the little box survives); heavy work offloads to free Google tiers — device tests → Firebase Test Lab, GPU jobs → the official Colab CLI, scale-out → Cloud Run. Every front (Telegram, chat, cron, PlayerOne) enqueues; nothing bypasses the queue.
