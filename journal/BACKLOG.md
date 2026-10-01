# The Remake Backlog — one per day (owner directive 2026-09-22)

Systems studies only: original titles, original art, the mechanics reborn. Lanes: word=WordDatabase stack · arcade=fleet-proven 2D · puzzle=grid/physics · psx=3D lane. Each entry gets: spec → art → 3-4 gated milestones → itch.

## The queue (build order)

| # | Study of | Era | Lane | Why it fits the Pipeline |
|---|---|---|---|---|
| 1 | SkiFree | '91 | arcade | one-button dodger, the yeti gag is folklore; tilt/drag steering ready-made |
| 2 | JezzBall | '92 | puzzle | grid capture math, pure battery material, phone portrait perfect |
| 3 | Destruct-O-Match (Neopets) | '00 | puzzle | block-match chain law; the arcade's sibling vibe |
| 4 | Text Twist | '99 | word | WordEngine reuse day-one; the 2-letter-min twist is one const |
| 5 | Line Rider | '06 | physics | line-draw → sled physics; Godot 2D physics handles it; massive visual juice lane |
| 6 | Sutek's Tomb (Neopets) | '00 | puzzle | match-3 swap with the collapse law; tile pool from our stack |
| 7 | Rodent's Revenge | '91 | puzzle | push-block onto cats; grid state machine, battery-first |
| 8 | Chat Noir | '07 | puzzle | the escaping-cat dot-blocker; tiny scope, pure elegance |
| 9 | Boomshine | '07 | arcade | chain-reaction bubbles; one scene, one law, endless polish room |
| 10 | Whack-A-Kass (Neopets) | '01 | arcade | bat-and-projectile timing; the gnome's revenge; tilt-ready |
| 11 | Qix / Volfied | '89/'91 | arcade | area-capture perimeter game; 2D draw law, juicy reveals |
| 12 | Pipe Mania | '89 | puzzle | tile-laying under pressure; the classic systems study |
| 13 | Pang / Buster Bros | '89 | arcade | popping-burst ballistics ladder; run-gun tech reuse |
| 14 | Winterbells | '07 | arcade | the bouncing-bunny holiday dream; one input, pure charm |
| 15 | Helicopter Game | '04 | arcade | hold-to-rise tunnel; the original one-thumb genre |
| 16 | Chip's Challenge | '89 | puzzle | tile-item logic levels; level-pack driven = manifest-per-level (sculmm pattern!) |
| 17 | Faerie Bubbles (Neopets) | '01 | puzzle | shooter-bubble rows; aim physics + match law |
| 18 | Canabalt | '09 | arcade | one-button runner, the mood piece; procedural rooftops |
| 19 | N (ninja) | '04 | arcade | momentum platforming physics; the physics IS the game |
| 20 | The Impossible Game | '09 | arcade | rhythm-runner cubes; the CUBEFALL roll math cousins |
| 21 | Filler (flash) | '07 | puzzle | grow-balls-without-pop; the hold-release law |
| 22 | Swarm (Neopets) | '00 | arcade | space-shooter-lite; GYRO's weapon stack reuse |
| 23 | Magic Pen | '08 | physics | crayon-draw physics objects; Line Rider's cousin, bigger law |
| 24 | Snow Bros | '90 | arcade | bubble-trap-and-kick platformer; STAR VISITOR systems cousin |

## Rules
- IP law unchanged: original titles/art; studies of systems, never assets. (Nothing from the Lost Media Jam's banned list enters jams.)
- One per day: the daily job picks the next entry, writes the spec, generates art, fires M1. Finished games ship itch + asset pack + devlog automatically.
- Insertion: user can promote any entry to tomorrow by saying so.

## Built
- #1 SkiFree study -> POWDER RUN (M1-M3 green, live on retromonkey + shipped) AND SLOPE HOUND (M1-M3 green 345x2, itch 2026-09-25, https://slothitude.itch.io/slope-hound) — two takes, one day each
- #2 JezzBall -> built (M4 green, live on retromonkey)
- #3 Destruct-O-Match -> built (server, games-src)
- NEXT: #4 Text Twist (word lane, WordEngine reuse)

- #4 Text Twist -> TWISTED SIX (M1-M3 green 465x2, itch 2026-09-26, https://slothitude.itch.io/twisted-six)
- NEXT: #5 Line Rider (physics lane)

- #5 Line Rider -> SCARVE (M1 GREEN x2 2026-09-28 — 81/81 + 33/33 replay, quit-after clean; probe-fed freeze law tree-paused; physics-race bug killed via atomic _integrate_forces hand-off; M2 building)
- NEXT: #6 Suteks Tomb (puzzle lane, match-3)

- #6 Suteks Tomb -> SARCOFALL — BUILT & SHIPPED (2026-09-28, one-day full cycle: spec -> art 10/10 Flux first-attempts Rog-local -> M1 56/56+13 x2 -> M2 93/93+19 x2 -> M3 113/113 x2, wall 262 checks + replays x2, Web export clean -> itch https://slothitude.itch.io/sarcofall, embed verified playing, asset pack sarcofall-art-pack.zip)
- SCARVE (#5 Line Rider) — BUILT & SHIPPED (2026-09-28 catch-up: M1 81/81+33 x2 resumed from probe-death with the tree-paused freeze law, M2 90/90+34 x2, M3 104/104 x2 — full 342-check wall + replays x2, Web export clean, itch https://slothitude.itch.io/scarve, embed verified, asset pack scarve-art-pack.zip). TWO daily games shipped this day: SARCOFALL + SCARVE

- #7 Rodents Revenge -> CHEESEMAKER — BUILT & SHIPPED (2026-09-29, full one-day cycle: spec -> art [ART LAW: the word "mouse" is Flux-blacklisted, "rodent mascot" passes] -> M1 1318+21 x2 -> M2 182+39 x2 -> M3 132 x2, full wall 1632 checks + replays x2 + third identical pass -> itch https://slothitude.itch.io/cheesemaker, embed verified, pack cheesemaker-art-pack.zip). FIVE dailies shipped total. NEXT: #8 Chat Noir (puzzle lane)
- NEXT: #7 Rodents Revenge (puzzle lane, push-block)
- #8 Chat Noir -> PAWSHUT — BUILT & SHIPPED (2026-10-01, full one-day cycle ALL ON LAPPY, Rog as terminal only: spec -> art 7/7 first-attempt Flux (Lappy home lane, key at ~/.nvapi) -> keying ON LAPPY (cv2 --user install) -> M1 30/30+replay x2 -> M2 14/14 x2 via the FIRST NATIVE claude_code milestone (Lappy's own Claude Code on shared Rog credentials) -> M3 17/17 x2 + Web export -> itch https://slothitude.itch.io/pawshut EMBED VERIFIED PLAYING (tap-verified, not just rendered), asset pack pawshut-art-pack.zip, live at retromonkey.com.au/games/pawshut/). THE DAY THE FLEET'S DIRTY SECRET DIED: every web game ever shipped hung minutes on a 39.5MB uncompressed wasm — fixed fleet-wide by Caddy gzip (39.5MB -> 10.6MB, boot 30s) + COOP/COEP headers; the dailies ALSO had real mouse_filter input-swallow bugs (overlay Controls defaulting STOP) — hotfixed across all six games with input-regression checks grown into every wall. PlayerOne critique lane revived end-to-end same day (chromium re-homed, collector SwiftShader args, five dailies critiqued). SIX dailies shipped total.
- NEXT: #9 Boomshine (arcade lane, chain-reaction bubbles)

