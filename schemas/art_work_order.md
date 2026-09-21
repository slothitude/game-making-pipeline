# art_work_order

A T2 art request in work-order form: glm does not touch image bytes — it emits this
JSON, the Flux.1-dev front renders it, and PIL post-processing runs the acceptance
checks before the asset is allowed into `assets/generated/` (and then through the
gate wall with everything else). This is architecture law 1: work-orders over code,
and law 4: the content-filter reword law lives in the rendering front, not here.

## Fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique id. Convention: `wo-art-<YYYYMMDD>-<seq>`. |
| `game` | string | Slug of the game the asset belongs to (`octogram-arcade` in v1). |
| `asset_id` | string | Stable asset key the game resolves at runtime, e.g. `logo`, `tile_word`, `hero_idle`. The generator writes exactly this filename; the game never sees the prompt. |
| `prompt` | string | The generation prompt as handed to Flux.1-dev. If the content filter trips, the front rewords it (law 4) — this field records the final accepted wording. |
| `style` | string | Style tag consumed by the front's post-processing chain, e.g. `flat-vector`, `pixel`, `clay`. Keeps PIL magic-wand passes consistent across regenerations. |
| `acceptance` | object | The pixel gate. All three keys must pass or the asset is rejected and regenerated (fallback chain: Colab -> local NVIDIA -> queued). |
| `acceptance.opaque_min` | number | Minimum acceptable fraction of opaque pixels (0.0-1.0). Catches transparent/black-frame failures. |
| `acceptance.opaque_max` | number | Maximum acceptable fraction of opaque pixels (0.0-1.0). Catches "filled the whole canvas" failures on sprites that need alpha. |
| `acceptance.mean_color_hint` | string | Expected mean color as `#rrggbb`. The check compares the rendered mean against this within the front's tolerance; it is a hint, not an exact-match law. |

## Invariants

- The acceptance block is authored with the work-order, not retro-fitted after a
  render — the gate must exist before the pixels do.
- An asset that passes acceptance still goes through the game's gate wall; pixel
  acceptance is necessary, never sufficient, to ship.
