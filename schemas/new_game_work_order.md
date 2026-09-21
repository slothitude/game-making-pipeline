# new_game_work_order

A NEW-GAME request from the hub front door (P8 template-instantiation front). The
hub sends this to the bot; the Actions instantiation job clones the matching
template, applies the player's branding, runs the gate wall on the result, deploys
to the hub Pages tree at `games/<player-id>-<slug>/`, and the bot replies with the
live link. One of these == one new player-owned game.

## Fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique id. Convention: `wo-new-<YYYYMMDD>-<seq>`. |
| `from_telegram_id` | integer | Telegram user id of the player. This becomes the owner: the deployed game lives under their id, and their feedback routes only into their copy (multi-tenant law). |
| `template` | string | Which factory template to instantiate: `arcade` (Word Poker + Campaign + Eight Letters merged), `rpg` (standalone RPG), `eight` (standalone Eight Letters). Future templates land here as new choices. |
| `title` | string | Player-facing game title, applied to the window title, menu and logo work-order. Free text from the player — the instantiator sanitizes it for filesystem-safe use but keeps this field verbatim. |
| `palette` | object | Named color slots applied as tunables (law 2: named constants, LLM-verb-modulatable). Slots are template-defined; instantiators map unknown player colors to nearest slot rather than inventing new keys. v1 minimum: `bg`, `ink`, `accent`, `board`. |
| `slug` | string | URL-safe directory name for the deploy: lowercase, `[a-z0-9-]` only, no leading/trailing dash. Final deploy path is `games/<from_telegram_id>-<slug>/`. |
| `status` | string | `new` (received) -> `done` (gates green, deployed, link sent) or `failed` (gates red after budget / quota refused / abuse guard) or `human` (escalated to the owner). |

## Invariants

- One work-order, one game directory: re-running instantiation for the same
  `id` must overwrite that id's directory, never touch another player's.
- Quotas and the invite-mode allowlist (see `docs/OPERATIONS.md`) are checked at
  intake, before any template is cloned.
- Every instantiated game must clear the full gate wall before its link is sent —
  a fresh game ships green or does not ship.
