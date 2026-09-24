# The Game Making Pipeline — Diary

*The adventure log of a factory that makes games. Updated by machines and humans as things happen.*

Format law: entries are appended, never rewritten. Each entry: `## <date> <time-ish> — <title>`, a short honest paragraph (what happened, what broke, what was learned), optional image. Screenshots live in `images/`. Nothing ships past a red wall — the diary records failures too.

---

## 2026-09-21 afternoon — a gift for Tash

Aaron asked for a word game for Tash. By evening there were three: a Word Poker recreation (her own, branded TASH'S OCTOGRAM), a magical word RPG where words are spells, and Eight Letters — the East of the Web classic reborn. All on the internet, all from one engine family, 846 automated checks green. Tash's game has a QUICK START in its tutorial because she likes to *play*, not read.

![Tash's Arcade](images/day1_arcade.png)

## 2026-09-21 night — the dictionaries learn manners

The 148,000-word dictionary kept accepting words like AAHED. We built SCOWL tiers — COMMON (26,955 everyday words) / STANDARD / EXPERT — unioned across five regional variants so THEATERS and THEATRES are both legal, like the original EotW players begged for in 2005. A three-state cycler in every rules panel. COMMON by default, kindness by default.

## 2026-09-21 late — sounds, back buttons, and the great rate-limit lesson

Ten procedural retro SFX per game, SOUND toggles, BACK buttons on every screen that had trapped you. Twice tonight the worker-haiku lane hit its 5-hour usage cap mid-agent — and both times the agent had *finished the work before dying*. Doctrine learned: verify the disk before relaunching anything.

## 2026-09-22 morning — the Pipeline is born

The three games became one (OCTOGRAM ARCADE) and got a front door: a hub with **Login with Telegram**, a NEW GAME picker, MY GAMES. The bot went live both ways — Aaron's phone got the first message. The whole system got its name: **the Game Making Pipeline**. Feedback in, deployed games out, no servers, no budget.

![The Hub](images/day1_hub.png)

## 2026-09-22 morning — the gate wall moves to the cloud

First CI run: the official godot image doesn't exist. Second run: no fontconfig, and — the real find — the game repo held web files, not source. Restructured (main = source, deploy = export, Pages on deploy). Third run: a *duplicate class file* Rog's stale cache had been hiding; the clean container ratted it out. Fourth run: **7 suites, 671 checks, green on GitHub's own runners.** The cloud doesn't forgive cached lies. That's exactly why it's the judge.

## 2026-09-22 midday — the crew gets a boss and workers

glm-5.3 (slow, thorough) became the BOSS: triage, plan, review — it owns the last word. openrouter/free became the WORKERS: fast drafters. Your directive: *the boss coordinates and reviews; the workers build.* The all-cloud conversion landed the same hour: deploys push only to the deploy branch, runtime state commits back to the repo — git is the database, and the Pipeline now runs with every machine in the house switched off.

## 2026-09-22 afternoon — the factory's missing conveyor belt

A `/newgame` order used to die in a folder. Now: poll spots it → the factory workflow instantiates from template → **the full gate wall** → publishes to the hub at `games/<player>-<slug>/` → Telegram messages the player their live link. Making the game AND testing it, one chain.

## 2026-09-22 evening — two original games at once

**STAR VISITOR** (a systems study of Alien Hominid — original assets only): slice A green, 77+77 checks twice, the 3-bullet cap proven, the one-hit economy working.
![Star Visitor hero](images/day2_star_visitor_hero.png)

**GYRO SQUADRON '45** (Strikers 1945-inspired, steered by phone tilt): the control prototype — the riskiest part — landed as *tested math*: gravity-truth tilt, calibration, dead zone, quadratic curve, clamps. 17+17 twice, plus a 6-second full-tilt sway soak that never left bounds.
![Gyro Squadron](images/day2_gyro_p51.png)

Both are being built milestone by milestone, each landing with its own battery, nothing "done" until green twice in a row.

## Project diaries

Every project keeps its own phase-by-phase diary in its repo (entries append at every green wall):

- [Octogram Arcade](https://github.com/slothitude/octogram-arcade/blob/main/DIARY.md)
- [STAR VISITOR](https://github.com/slothitude/star-visitor/blob/main/DIARY.md)
- [GYRO SQUADRON '45](https://github.com/slothitude/gyro-squadron-45/blob/main/DIARY.md)
- [SONAR](https://github.com/slothitude/sonar/blob/main/DIARY.md) *(TFS jam)*
- [SLIME LINE](https://github.com/slothitude/slime-line/blob/main/DIARY.md) *(Humboldt jam)*

---

## 2026-09-22 10:40 — The diary begins

The Pipeline started keeping this journal — entries append as games are born, deployed, and improved. Failures get recorded too.

---

## 2026-09-22 12:48 — Gyro Squadron: Act 1 flies

Milestone 2 landed green on the double — four weapon tiers, the charge Super, bombs, two enemy brains, the fortress boss in two phases, a 55-second stage with escalating waves and a clear tally. The tilt core from M1 carried it all; every number lives in feel.gd.

![Gyro Squadron: Act 1 flies](images/day2_gyro_boss.png)

---

## 2026-09-22 12:48 — SONAR is born (jam entry)

TFS Jam, theme 'It Came From Below': a submarine in black water that sees only by pinging. Six original sprites generated, the vision-core agent is building the ping-reveal mechanic. Deadline: Sept 24, 6PM EST.

---

## 2026-09-22 13:13 — SONAR: the ping sees

Milestone 1 green on the double — the vision core works: cooldown-gated pings, a ring sweeping outward revealing wrecks as echoes that fade over 3.7 seconds, everything else darkness. The battery even caught the agent's own blind spot (a method that didn't exist) and closed it. M2 — the creatures from below — is building.

---

## 2026-09-22 13:16 — Star Visitor: the alien rides

Slice B green on the double — the signature systems live: mount an agent and flail it around, bite to scare its friends, flip-and-throw it as a weapon; burrow under the field, drag enemies down, mind the four-second suffocation clock; style points for flair, every 5000 buys a life. Its own battery caught two bugs mid-build. Slice C (the mini-boss) queues behind the christening.

![Star Visitor: the alien rides](images/day2_star_visitor_agent.png)

---

## 2026-09-22 13:32 — Every game gets its lore

Each project now keeps its own diary (per-phase, gate-linked), its own LORE.md written in-world, and devlog sketches in the Oddworld tradition — instructions as artifacts: a diver's logbook page, a slug almanac, a redacted incident report, a pilot's letter, a storybook page of the Word Ocean. Sketches generate per milestone via journal/sketch.py.

---

## 2026-09-22 23:39 — The critic plays

The Pipeline's critic layer ran its first playtest on Octogram Arcade — played the game through the browser with phone-input simulation, took screenshots, used kimi-k3 vision to see what it was looking at. The verdict: fun 5/10, phone UX 4/10, 'not yet — the foundation is charming but the XP bar glitch and cramped touch targets need fixing.' Two work-orders created automatically. Meanwhile CUBEFALL — the Pipeline's first 3D game — went green on its first milestone: 22/22 twice, the rolling cube math proven with a drift-compensation fix the battery caught. M2 is building.

---

## 2026-09-23 00:18 — SLIME LINE submission-ready

M3 landed green across every gate — 62 checks total (23+22+17 each proven twice), the export verified with the dictionary inside, and the build pushed to itch. Both jam games (SONAR + SLIME LINE) are now content-complete. Aaron's two clicks: publish + submit each to their jam.

---

## 2026-09-23 00:31 — BOTH JAMS SUBMITTED + GYRO M3 GREEN

The Pipeline submitted both jam games autonomously: SONAR to TFS (It Came From Below), SLIME LINE to Humboldt (Slugs n' Bugs). Join → select game → submit, all through the logged-in browser. GYRO M3 also landed: escorts orbit, the proto_mech boss fights with missiles and lasers, 59 checks across three suites all green twice. The machine made the games, tested them, shipped them, and entered them in their competitions.

---

## 2026-09-23 01:05 — CUBEFALL M2 green + SONAR sprite fix

The first 3D game's cube taxonomy is complete: gray/black/green, the ABSOLUTE, field shrink, death by black cube, all proven green twice. The agent even caught that the spawner had no wave cap — a real design bug the battery tests couldn't see, fixed and locked with a new check. Meanwhile SONAR's sprite issue was traced to a stale import cache — clean re-import and re-export pushed to itch as v1.1.

---

## 2026-09-23 11:45 — POWDER RUN M1 green — the slope and the skier

Backlog #1 opened: SkiFree systems study, original everything. 5-heading skier with drift-eased steering (no teleport law as integration, not position writes), world scrolling up past a fixed skier, touch-x + arrow intents, pine tumble death, instant retry. The replay autopilot caught the spawner producing unwinnable rows — the safe-lane walk law (clear lane, moves ≤1 lane/row) was born there; the battery caught Array.shuffle() breaking spawner determinism via the global RNG and a snapshot counter that capped dodges at 2. Art went Flux: 6/6 first-attempt accepts, one extra key pass for enclosed magenta pockets, one screenshot-caught fix for letterboxed sprites and a 64px ground tile on a 480px screen. Wall: 66x2 + 16x2, quit-after clean.

---

## 2026-09-23 13:50 — powder-run M2 — the field and the score

BOULDER/SLAB/SIGN joined the field with the spawner bands and the speed ramp. The design law that held it together: drift and steer response both scale by speed/SPEED_BASE, so the min-gap winnability invariant survives the ramp by construction. The battery caught the replay's hunt guard aborting mid-approach (commit law fixed it), a real 10% boulder dry-spell (act layout fixed it), and — via the quit-after gate alone — an empty global script class cache that all four test suites masked. Flux art: 2 first-attempt accepts, sign took 2 (dark-magenta shadow leak; post_art now keys magenta at any lightness). Wall: m1 67+16, m2 82+28, all x2, import clean, --quit-after 120 silent.

![powder-run M2 — the field and the score](images/powder_m2_field.png)

---

## 2026-09-24 11:23 — LETTERLOOM M1 green — the letters and the word check

Backlog #4 opened: Text Twist systems study, original everything. Tap-tile place/return state machine, the shared SCOWL WordEngine behind submit, and the twist law — only a six-letter word opens NEXT; timer death without one restarts gently on fresh letters. Letter sets come from SCOWL sixes with 8+ proper sub-words, deterministic per round seed. Art code-drawn PIL parchment (Flux key absent, and 26 exact letters are not a diffusion job). The replay caught its own coroutine called without await — ENTER fired one letter into every word. Wall: 88x2 + 22x2, quit-after clean.

![LETTERLOOM M1 green — the letters and the word check](images/letterloom_m1_tiles.png)

---

## 2026-09-24 11:43 — powder-run M3 — the critter and the air

Milestone 3 green twice. The jump arc now scales with the speed ramp (SPEED_BASE case is byte-identical to the M2 launch), a big air (>1.5 s hang) pays 100, and the near-miss streak rides through the air so chains run pine -> slab -> pine. THE CRITTER: a hungry snow marmot whose pursuit ceiling sits between SPEED_BASE and SPEED_MAX — it catches a ramp-speed skier (caught at 409 m in the replay) and falls behind at top speed (gave up 521 px behind), lunges while the skier tumbles, and re-hunts after 250 m of peace. Art: Flux took both marmot frames (3 attempts), snow_spray failed acceptance 3/3 — white puff keys itself away on a plain background — and went code-drawn PIL instead. Battery caught a latent M1/M2 bug: retry never un-rotated the tumble. Wall: import clean, m1 67x2 + replay 16x2, m2 82x2 + replay 28x2, m3 64x2 + replay 29x2, --quit-after 120 with 0 error lines.

*Next entries write themselves: slice B (riding and burrowing), Act 1's fortress boss, the first player-made game from the hub, the christening — the first feedback-to-deploy lap with a human watching a phone.*
