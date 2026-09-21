# feedback_work_order

One player feedback request, from tap to confirmation. This is the unit of work the
whole Pipeline exists to process: Telegram intake -> glm-5.3 triage -> gate wall ->
web deploy -> Telegram reply. Every feedback message that might mean a change gets
one of these; they are also the audit trail (cloud_editor writes one per request).

## Fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique work-order id. Convention: `wo-fb-<YYYYMMDD>-<seq>` — sortable, greppable in logs and in git history. |
| `time` | string | ISO-8601 UTC when the message arrived (when the work-order was created, not when it finished). |
| `from_telegram_id` | integer | Telegram user id of the requester. Multi-tenant routing key: the reply goes back to this id, and (v2) the patch is scoped to that player's game directory. |
| `game` | string | Slug of the target game repo/directory, e.g. `octogram-arcade`. For v1 single-tenant this is always the flagship; v2 fills it from routing. |
| `text` | string | The player's message verbatim. Never rewritten — the original words are evidence when triage misfires. |
| `tier` | string | Autonomy tier glm assigned at triage: `T0` conversational (nothing edited) / `T1` tunables JSON / `T2` art work-order / `T3` GDScript diff. Drives which gates beyond the wall apply (T2 adds pixel acceptance, T3 adds green-battery x3). |
| `status` | string | `new` (not yet processed) -> `done` (gates green, deployed, player notified) or `failed` (wall red after the attempt budget; human notified) or `human` (escalated: /stop, tier budget exhausted, or owner override). |

## Invariants

- `status` moves forward only: `new -> done | failed | human`. No reopening — file a
  fresh work-order instead.
- A `T3` work-order that exhausts its green-battery attempts must end `human`, never
  `failed` silently.
- Work-orders are written once at intake and updated in place as status changes;
  they are never deleted (git is the database).
