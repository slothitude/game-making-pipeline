# new_game_workflow_notes.md — wrapping `instantiate_new_game.py` in GitHub Actions (P8)

`instantiate_new_game.py` does everything up to (not including) the hub commit:
order validation → template copy → rebrand → 7-suite gate wall → web export →
`hub_bundle/`. **This workflow owns the last mile**: commit the bundle into the
hub Pages repo, merge `games/index.json`, push, and reply to the player via the
bot.

```
/newgame arcade <Title>            (player -> bot)
        |
        v
bot creates work-order JSON        <-- QUOTAS ENFORCED HERE (see bottom)
        |
        v
workflow_dispatch (order JSON as input)
        |
        v
instantiate/instantiate_new_game.py   in godot container   <-- THIS SCRIPT
        |
        v
hub_bundle/ -> hub repo games/<id>-<slug>/ + games/index.json (jq)
        |
        v
Pages deploy -> bot replies with https://<hub>/games/<id>-<slug>/
```

## File origins (who owns what)

| Path | Producer |
|---|---|
| `order.json` | the bot handler (or hub NEW GAME deep link) |
| `template/` | arcade repo, checked out with `GAMES_TOKEN` |
| `staging/`, `staging-out/` | this script |
| `hub_bundle/` (inside `staging-out/`) | this script — web files + `game.json` |
| `games/<id>-<slug>/`, `games/index.json` | the workflow steps below |

## Workflow (`.github/workflows/new_game.yml` in the GMP repo)

```yaml
name: new-game
on:
  workflow_dispatch:
    inputs:
      order_json:
        description: "NEW-GAME work-order JSON {from_telegram_id, template, title, palette?, slug?}"
        required: true

permissions:
  contents: write        # commit into the hub repo

jobs:
  instantiate:
    runs-on: ubuntu-latest
    container: docker://godotengine/godot:latest   # provides `godot`; GODOT_BIN=godot
    env:
      GODOT_BIN: godot
    steps:
      - name: Checkout Game Making Pipeline (this script)
        uses: actions/checkout@v4
        with:
          path: gmp

      - name: Checkout arcade template (private repo -> GAMES_TOKEN)
        uses: actions/checkout@v4
        with:
          repository: ${{ vars.ARCADE_REPO }}      # e.g. slothitude/octogram-arcade
          ref: main
          token: ${{ secrets.GAMES_TOKEN }}        # PAT with repo read on the template
          path: template

      - name: Write work-order
        run: echo "${{ github.event.inputs.order_json }}" > order.json

      - name: Instantiate + gate + export
        working-directory: gmp
        run: |
          python3 instantiate/instantiate_new_game.py \
            --order ../order.json \
            --template-dir ../template \
            --out ../staging

      - name: Upload staging on failure (gate forensics)
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: staging-${{ github.run_id }}
          path: |
            staging/
            staging-out/gate_report.json
          retention-days: 7

      - name: Deploy hub_bundle into hub repo
        working-directory: gmp
        env:
          HUB_TOKEN: ${{ secrets.GAMES_TOKEN }}    # same PAT, write access to hub repo
        run: |
          git config --global --add safe.directory "$GITHUB_WORKSPACE/hub"
          git clone "https://x-access-token:${HUB_TOKEN}@github.com/${{ vars.HUB_REPO }}.git" hub
          OWNER_ID=$(jq -r '.owner_id' staging-out/hub_bundle/game.json)
          SLUG=$(jq -r '.slug' staging-out/hub_bundle/game.json)
          DEST="games/${OWNER_ID}-${SLUG}"
          mkdir -p "$DEST"
          cp -r staging-out/hub_bundle/. "$DEST/"

          # merge game.json into games/index.json (git is the database)
          GAME=$(cat "$DEST/game.json")
          if [ -f games/index.json ]; then
            tmp=$(mktemp)
            jq --argjson g "$GAME" \
               '.games = ((.games // []) | map(select(.owner_id != $g.owner_id or .slug != $g.slug)) + [$g])' \
               games/index.json > "$tmp" && mv "$tmp" games/index.json
          else
            jq -n --argjson g "$GAME" '{games: [$g]}' > games/index.json
          fi

          cd hub
          git config user.name  "gmp-newgame-bot"
          git config user.email "actions@users.noreply.github.com"
          git add -A
          git commit -m "new game: ${OWNER_ID}-${SLUG} ($(date -u +%FT%TZ))" \
                     -m "title: $(jq -r '.title' ../staging-out/hub_bundle/game.json)"
          git push

      - name: Reply to the player via the bot
        if: success()
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
        run: |
          OWNER_ID=$(jq -r '.owner_id' staging-out/hub_bundle/game.json)
          TITLE=$(jq -r '.title' staging-out/hub_bundle/game.json)
          curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d chat_id="${OWNER_ID}" \
            --data-urlencode text="🎮 ${TITLE} is live: ${{ vars.HUB_URL }}games/${OWNER_ID}-$(jq -r '.slug' staging-out/hub_bundle/game.json)"

      - name: Report failure to the player
        if: failure()
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
        run: |
          curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d chat_id="${{ fromJson(github.event.inputs.order_json).from_telegram_id }}" \
            --data-urlencode text="Your game build failed the gate wall — a human is on it. Nothing was deployed."
```

Notes on the exact steps:

- **Container**: `docker://godotengine/godot:latest` matches PLAN.md's
  "godot:latest container". Inside it the binary is `godot`, hence
  `GODOT_BIN=godot` (the script honours the env; on Rog it falls back to the
  const `Godot_v4.7.1-stable_win64.exe` path). Pin a version (e.g.
  `godotengine/godot:4.7.1`) once the template pins `config/features`.
- **Export templates**: the godot image ships the editor but *not* export
  templates. Add before the instantiate step (version-matched to the image):
  ```yaml
  - name: Install export templates
    run: |
      VER="4.7.1"
      curl -sLo /tmp/tpz "https://github.com/godotengine/godot/releases/download/${VER}-stable/Godot_v${VER}-stable_export_templates.tpz"
      mkdir -p ~/.local/share/godot/export_templates
      unzip -o /tmp/tpz -d /tmp/x
      mv "/tmp/x/templates/web_${VER}.noexport" ~/.local/share/godot/export_templates/${VER}.stable/web_${VER}.noexport
      cp -r /tmp/x/templates/. ~/.local/share/godot/export_templates/${VER}.stable/
  ```
  (Only the `web_*` template is strictly needed for this front.)
- **Secrets**: `GAMES_TOKEN` (PAT: read arcade template, read/write hub repo),
  `BOT_TOKEN` (Telegram). Vars: `ARCADE_REPO`, `HUB_REPO`, `HUB_URL`.
- **Gate wall**: nothing reaches the deploy step unless the script exits 0.
  Exit 2 = gate failure → the `if: failure()` steps fire, staging + gate report
  are uploaded as an artifact, nothing is committed to the hub.
- **Idempotence**: the script wipes `staging/` and `staging-out/` at the start
  of every run, and the jq merge replaces any existing entry with the same
  `owner_id` + `slug` — re-running the same order is safe.

## Quota rule (enforced where the ORDER is created — bot handler / hub intake)

Not enforced in this script (it is a pure order-executor); the gate that keeps
one player from flooding the hub lives at order creation:

- **max 3 games per player**: count entries with `owner_id == from_telegram_id`
  in `games/index.json`; at 3, refuse with the games they already own.
- **min 10 minutes between creations**: reject if the player's newest
  `created`/`updated` stamp is less than 10 minutes old.
- v1 invite mode (PLAN P8): additionally only accept orders from the allowlist.

Reference implementation for the bot handler:

```python
MAX_GAMES_PER_PLAYER = 3
MIN_SECONDS_BETWEEN_CREATIONS = 600  # 10 min

def quota_check(index: dict, owner_id: int, now_iso: str) -> str | None:
    """Returns a refusal string, or None if the order may proceed."""
    mine = [g for g in index.get("games", []) if g["owner_id"] == owner_id]
    if len(mine) >= MAX_GAMES_PER_PLAYER:
        return f"You already have {len(mine)} games (max {MAX_GAMES_PER_PLAYER}). " \
               + ", ".join(g["title"] for g in mine)
    latest = max((g.get("updated", "") for g in mine), default="")
    if latest and now_iso <= latest:  # ISO strings sort lexicographically
        return "Your last game is still building — try again in a few minutes."
    return None
```

(The 10-minute rule also needs the *previous* order's timestamp; keep a
`last_order_at` per player in the bot's own store if index stamps alone are not
granular enough.)

## Local test (no hub, no network)

```bash
python instantiate/instantiate_new_game.py \
  --order instantiate/example_order.json \
  --template-dir C:/Users/aaron/octogram-rpg \
  --out "$TMPDIR/gmp-staging" \
  --dry-run
```

Drop the run's `staging-out/hub_bundle/` contents into the hub repo's
`games/<id>-<slug>/` by hand when testing the Pages side.
