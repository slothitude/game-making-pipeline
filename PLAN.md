# The Game Making Pipeline — full plan
*The side project: feedback in, shipped game changes out — autonomous, zero budget, gates as the safety net.*

Mission: Tash (or anyone) taps FEEDBACK in a live game. The request is reviewed by an AI, implemented, tested against the full gate wall, deployed to the web, and confirmed back — typically inside 15 minutes, with a human Telegram thread watching everything.

## Stack map (all free, all named by the owner)
| Piece | Role |
|---|---|
| Telegram bot | Intake (from games + humans), review thread, notifications |
| glm-5.3 (NVIDIA free endpoint) | The brain: triage, plan, implement (JSON/art/code tiers) |
| Flux.1-dev (NVIDIA genai) | Art generation: logos, textures, characters (PIL magic-wand post) |
| Google Colab CLI | Heavy lifting: batch art, mesh generation (sculmm's hy3glen-on-T4 pattern), long renders |
| GitHub Actions | The cloud runner: `godot:latest` container → 7-suite gate wall → web export → Pages push; later, blender jobs |
| GitHub Pages | Delivery — every deploy is a git commit, instantly live |
| godot:latest (container) | Reproducible headless gates + exports, no Rog required |
| Blender | 3D front (phase 5): intro scenes, animated bits — work-order driven |
| Rog / Lappy | Rog = orchestrator (cloud_editor loop, Claude sessions); Lappy = optional local GPU |

## Architecture law (inherited from the sculmm 3D pipeline)
1. **Work-orders over code**: LLM output targets JSON manifests + asset requests wherever possible, not raw diffs. (sculmm's llm_world.json / work-orders)
2. **NAMED LAW constants**: every tunable is a named constant/JSON key — "LLM-verb-modulatable" by design.
3. **Factory fronts**: each pipeline stage (F1 engine, F2 content, F3 art, F4 deploy…) is a self-contained front with its own green battery.
4. **Fallback chains**: Colab → local NVIDIA → queued, never blocked; content-filter reword law for art prompts.
5. **Gates always**: nothing ships without the wall; every deploy revertible.

## The games (the Pipeline's first customers)
- **Octogram Arcade** (merged: Word Poker + Campaign + Eight Letters, one menu) — flagship intake
- Standalones remain live: octogram · octogram-rpg · eight-letters
- Gate wall today: 7 suites, 654 checks (arcade)

## Autonomy tiers (safety model)
| Tier | LLM may edit | Review |
|---|---|---|
| T0 conversational | nothing | — |
| T1 tunables JSON | per-game data/tunables.json (timers, scores, colors, taunts) | gates only |
| T2 art work-orders | art spec JSON → Flux/PIL → assets/generated/ | gates + pixel acceptance checks |
| T3 code diffs | GDScript on scratch branch | gates + green-battery ×3 attempts, else human |
Human override via Telegram any time: /stop /approve /revert.

## PLATFORM (v2 scope, 2026-09-22): the Pipeline becomes a factory for anyone
**The hub** (a GitHub Pages site, `game-making-pipeline/hub/`): the front door.
- **Login with Telegram**: official Telegram Login Widget (works statically) greets the player; every REAL action travels through the bot (`t.me/<bot>?start=…` deep links) — identity is Telegram-authenticated by construction, no server, no secrets in the client.
- **MY GAMES**: each logged-in player sees their games (from `games/index.json` — git is the database).
- **NEW GAME**: template picker (Word Arcade · RPG · Eight Letters · future templates) + a short brief (game title, who it's for, vibe) → sent to the bot as a NEW-GAME work-order.
- **Their own games**: each generated game = a full web export at `<hub>/games/<player-id>-<slug>/`, owned by that Telegram identity; their feedback edits flow only into their game (cloud_editor scopes patches to their directory).

**Multi-tenant law**: one hub repo, one bot, one glm key; per-player isolation by directory + Telegram id routing; the gate wall runs per game before any deploy; quotas polite (N games/player, minutes between requests).

## Phases (v2)
- **P0 ✅** Arcade merged (654 checks), cloud_editor harness, Telegram channel, feedback panel (building), runbook.
- **P1** GMP repo + Actions CI (`godot:latest` gates + Web export + Pages push).
- **P2** cloud_editor as scheduled Action; Rog loop stays for development.
- **P3** Art work-orders (Flux + PIL acceptance). **P4** Tunables front. **P5** Blender front. **P6** Learning loop.
- **P7 — Hub front door** (new): landing page with Telegram login, MY GAMES, NEW GAME template picker, feedback portal; `games/index.json` manifest; deep-link intents into the bot.
- **P8 — Template instantiation front** (new): Actions job that takes a NEW-GAME work-order → clones the arcade template with player branding (title, palette, Flux logo) → gates → deploys to hub Pages `/games/<id>-<slug>/` → bot replies with the live link. Quotas + abuse guards (allowlist in v1 invite mode).

## Immediate next steps (this session)
1. Feedback panel green → Arcade to its own Pages repo → flagship link.
2. **Hub landing page built and deployed** (P7) with Telegram login + NEW GAME deep links.
3. First end-to-end burn (feedback → deploy). Template instantiation (P8) right behind it.
