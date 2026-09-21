# The Game Making Pipeline

*Feedback in, deployed game out — no servers, no budget, gates as the safety net.*

```
 PLAYER (Tash, on her phone)                    YOU (Aaron, on Telegram)
        │ taps FEEDBACK in-game                         │
        ▼                                               │
 ┌─────────────────┐                          ┌──────────────────┐
 │ Octogram Arcade │  Telegram Bot API ─────► │  review channel  │
 │ (GitHub Pages)  │                          │  (every request  │
 └─────────────────┘                          │   lands visible) │
        ▲                                     └────────┬─────────┘
        │ 1 min after push                             │
 ┌──────┴────────┐   push    ┌──────────────┐  polls   │
 │  GitHub Pages │◄──────────│ web_deploy   │◄─────┐   │
 └───────────────┘           └──────────────┘      │   │
                                                   ▼   ▼
                                    ┌───────────────────────────┐
                                    │ cloud_editor (this folder)│
                                    │  glm-5.3 (NVIDIA, free)   │
                                    │  reviewer + implementer   │
                                    └───────────┬───────────────┘
                                                │ git apply → GATE WALL
                                    (846+ checks: unit, rpg, battle, menu,
                                     e2e, eight-letters suites)
                                     green → deploy · red → retry ×3 → report
```

## The pieces
- **Games**: Octogram Arcade (Word Poker + Campaign + Eight Letters behind one menu), plus the three standalone links — all Godot 4.7, all Pages.
- **Feedback channel**: in-game HTML panel → Telegram bot (token embedded client-side — send-only scope by design).
- **cloud_editor**: `cloud_editor.py` on Rog (GitHub Actions job later — same script). Polls Telegram, asks glm-5.3 for a plan+diff, applies on a scratch branch, runs the gate wall, deploys on green, reports back in Telegram either way. `feedback/` = audit trail; every deploy is a git commit.
- **Secrets**: `config.json` (bot token, NVIDIA key) — gitignored, never enters a repo.

## Runbook
- Start the loop: `python C:\Users\aaron\octogram-arcade\cloud_editor\cloud_editor.py`
- Manual override: the loop only reads Telegram messages; edit code directly and push as usual — both paths coexist.
- Revert anything: `git revert` on the arcade repo, push web_deploy.
- Latency law: free-tier glm-5.3 ≈ 3-4 min per call → feedback to live change ≈ 5-15 min.

## Roadmap
1. ✅ Arcade merge + cloud_editor harness + config
2. ⏳ In-game feedback panel (FEEDBACK button → Telegram POST)
3. ⏳ GitHub Actions variant (scheduled job, secrets in GH, runner does the same loop)
4. Future: cloud_editor learns from accepted/rejected history (feedback/*.json is the training log)
