# PlayerOne — A Full Study
*How it plays, how it finds issues, where jevlike fits, and the road to a critic that actually improves games. Written 2026-09-23 after a day of live wiring.*

## 1. What PlayerOne IS (the honest stack)

Three tiers with three different jobs:

| Tier | What | Speed | Where | Job |
|---|---|---|---|---|
| **Plays** | jevlike tiny-scorer (64-dim byte-level attention, ~161KB numpy) | milliseconds | server | choose actions from (context, options) text |
| **Sees** | vision (llama-3.2-11b API) | seconds | NVIDIA API | screenshot → 5-dim scores + verdict |
| **Judges** | LLM ladder (nemotron-550b → deepseek → gemini → openrouter) | seconds | APIs | evidence+scores → loves/issues/directives |

Plus the mouths: **critic chat** (investigates, plans, files actions) and **coach chat** (spec-reading helper). Plus the **memory**: reviews corpus, diary, evidence dir.

## 2. HOW IT PLAYS — the three lanes and their truth

### Lane A: pixel-measured canvas play (the PROVEN one)
`scripts/sonar_playthrough.py` etc.: a Playwright canvas session takes a
screenshot → `view_of()` measures pixels (air bar brightness, hull blob
centroid, cyan reveals) → builds a TEXT context ("depth 40m | air 12s |
jelly near E") → jevlike scores the 10-action vocabulary → chosen action
executes as tap/drag. **This is how every jevlike model was trained and how
self-play must work.** Per-game code, pixel-true, deterministic-ish.

### Lane B: evidence spectator (what runs today on the server)
`collect_evidence.py`: loads the game, taps past the menu (today's fix),
screenshots on a cadence. It films; it does not decide. Its output feeds the
SEE and JUDGE tiers — which is why critiques say true things about what's on
screen but nothing about how the game FEELS to play.

### Lane C: emulator play (parked)
The ATD + APK lane: real Android, adb input, screencap. Blocked on: APK
workflow (red), box RAM (emulator parked), and no browser on ATD for the
HTML5 route.

**The gap:** Lane A exists as per-game modules but only ran on Rog during
training. The server has never PLAYED a game with intent. `selfplay.py`
(building) is exactly Lane A on the server: the missing link between "the
factory made games" and "the factory plays its own games."

## 3. HOW IT FINDS ISSUES — the chain and its failure history

```
play (lane A/B) → evidence (screenshots + events + rows)
  → SEE: vision scores the frames
  → JUDGE: ladder reads evidence+scores → issues[]
  → CONVERT: issues → directives → queue jobs (tune/art/milestone)
  → ACT: executors land changes → Actions wall gates → deploy
  → REPEAT: new evidence, new critique — the loop
```

Failure ledger (all hit today, all named):
1. **Judgment died on transport** — ladder 504s killed critiques (fixed:
   patient driver, 3× retry, honest degrade).
2. **Conversion guessed** — regex-extracted const names from prose →
   `tunable: None` orders (fixed: the critic now emits structured
   `tunable_directive`/`art_directive` itself; converter prefers them).
3. **Evidence was menus** — the spectator never pressed START (fixed: Enter +
   2 taps; 22 shots vs 6).
4. **No play intent** — Lane B can't feel difficulty; only Lane A rows carry
   "what a player would actually do."

## 4. JEVIKE — what it is, what it ISN'T, and how to make it matter

**Is:** a one-pass option scorer over byte-level context. Tiny, fast,
numpy-portable, retrainable in ~40s on the server's CPU. Top-1 on human-style
play: 0.20 (vs 0.08 scripted-trained) — and the 57% exploration-noise ceiling
is a FEATURE: it models the unpredictable human mix.

**Isn't:** a general game player. It only knows the (context, options)
vocabulary it was trained on. It can't read a new game's state — that's
`view_of`'s job (per-game pixel law) or a vision model's.

**The three ways it matters:**
1. **Self-play data engine** — plays with its current brain (temp 0.35),
   records rows, retrains weekly: the distribution evolves toward real play.
   Humans' words (reviews/help chats) bias WHICH sessions get recorded and
   weighted (die-to-boulder chats → more boulder-band sessions).
2. **Difficulty instrument** — where the brain's choices die (action → death
   correlation across sessions) is a measurable difficulty signal no vision
   model can give: "players will die here" before any human does.
3. **Regression tripwire** — after a tune lands, self-play N sessions: if
   survival/actions-taken shift beyond band, the wall gets a red flag the
   suites can't see.

**The road:** per-game brains (exists: sonar/slime/arcade + human-model);
generic fallback = fixed action vocabulary + vision-described context (slower,
game-agnostic — the frontier after selfplay.py proves Lane A on the server).

## 5. THE PLAN (miles in order)
1. **MILE 1 (walking):** one critique lands one tune in one repo through the
   wall. Everything fixed above converges here.
2. **MILE 2:** one pi milestone goes 2× green (learner + memory-injected
   prompts).
3. **MILE 3:** portal live to real players → human words flow.
4. **Then:** selfplay weekly (data engine) → evolve weekly → difficulty
   instrument → regression tripwire. At that point PlayerOne stops being a
   critic that visits and becomes a player that lives there.
