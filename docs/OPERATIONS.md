# OPERATIONS — the runbook for when the factory misbehaves

Three procedures: rotate a leaked bot token, manage who can talk to the bot
(quotas / invite mode), and revert a bad auto-deploy. For the "what is this"
answer, read the top-level README first.

---

## 1. Rotate a leaked bot token

Symptom: the token appeared in a paste, a screenshot, a log, a commit, or a public
repo. A leaked token means anyone can read the bot's messages and send feedback as
the bot — treat it as compromised, not as "maybe".

1. **Revoke immediately** — in Telegram, talk to `@BotFather`:
   - `/mybots` -> pick the bot -> **API Token** -> **Revoke current token**
     (or send `/revoke` and pick the bot). You get a fresh token instantly; the old
     one stops working within moments.
2. **Stop the loops using the old token** — on Rog, kill any running
   `cloud_editor.py` session (it polls `getUpdates` with the old token and will
   error out anyway once revoked, but kill it so it does not half-process).
3. **Update every place the token lives:**
   - GitHub secrets: `TG_TOKEN` in this repo (Settings -> Secrets and variables ->
     Actions), and in any other repo that holds a copy.
   - Rog local config: `cloud_editor/config.json` (`bot_token` key).
   - Anywhere else it leaked into history — see step 5.
4. **Restart and verify** — restart the Rog loop, then confirm identity with:
   `curl "https://api.telegram.org/bot<NEW_TOKEN>/getMe"` (expect the bot's name
   and id in the JSON). Send one test feedback from a game to prove end-to-end.
5. **If the token was committed**: rotating makes it dead, which is the important
   part. Optionally scrub history afterwards (`git filter-repo` over the affected
   repo, force-push) — never rely on history edits instead of rotation.

Never paste a token into a work-order, a schema example, or a log line. In
logs/patches, mask it as `TG_TOKEN=<set>`.

---

## 2. Quotas and invite mode (allowlist)

v1 runs in **invite mode**: the bot only serves Telegram ids it has been told to
serve. The gate is the file `cloud_editor/allowlist.txt`:

- **One Telegram id per line** — digits only, e.g. `123456789`.
- Anyone NOT in the file is politely refused (no game changes, no quota burn, a
  short notice reply). Nothing else about their message is processed.
- To invite someone: append their id on its own line, commit (git is the
  database), done — the next poll picks it up. To kick someone: delete the line.
  No restart needed.

Quota law (v2, multi-tenant): per-player polite limits — a cap on games per player
and a minimum number of minutes between requests. The intent: one player cannot
burn the free LLM endpoints or flood the Actions minutes. The exact numbers are
named constants in the v2 cloud_editor config (they will land with the v2 sync;
v1 relies on the allowlist alone).

Escalation paths humans always keep (override commands in Telegram): `/stop`
(halt the loop), `/approve` (force-continue), `/revert` (roll back the last
deploy). The allowlist never limits the owner's overrides.

---

## 3. Revert a bad auto-deploy

Every deploy is a git commit on the game repo's `web_deploy` branch (that is the
whole safety model — deploys are revertible by construction). To roll one back:

1. **Stop the loop first** so the agent does not immediately redeploy on top of
   you: send `/stop` in the Telegram thread.
2. **Fetch and inspect the branch** (game repo = the one that shipped the bad
   change, e.g. `slothitude/octogram-arcade`):
   ```
   git clone git@github.com:slothitude/octogram-arcade.git
   cd octogram-arcade
   git fetch origin web_deploy
   git checkout web_deploy
   git log --oneline -10        # the bad commit says "CI deploy from GMP" or similar
   ```
3. **Revert (never force-push history)**:
   ```
   git revert --no-edit <bad-commit-sha>    # or `git revert --no-edit HEAD` for the latest
   git push origin web_deploy
   ```
   If several bad commits stacked up, revert them oldest-first so the reverts apply
   cleanly: `git revert --no-edit <sha-1> <sha-2> ...`
4. **Confirm**: GitHub Pages rebuilds within a minute or two — hard-refresh the
   live URL (bypass cache) and check the changed behavior is gone. The bad commit
   stays visible in `git log` as the revert's parent, which is the audit trail.
5. **Feed the lesson back**: resume with `/approve` (or restart the Rog loop), and
   file the failure as a work-order note — if the gate wall let a bad change ship,
   the wall is missing a gate; that note is how the wall grows.

---

## Where the levers live

| Lever | Location |
|---|---|
| `TG_TOKEN` / `NVAPI_KEY` / `CHAT_ID` | GitHub repo secrets + Rog `cloud_editor/config.json` |
| Invite list | `cloud_editor/allowlist.txt` (one Telegram id per line) |
| Loop on/off | Rog process (`cloud_editor.py`, no flag) + Actions `Pipeline poll` schedule (disable in the Actions tab) |
| Deploys | Game repo `web_deploy` branch — revert to undo |
