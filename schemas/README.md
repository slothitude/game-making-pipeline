# schemas/ — work-order contracts

Plain-text contracts between the fronts. The LLM emits/targets these JSON shapes,
never raw diffs, wherever a structured answer can carry the change (architecture
law 1: work-orders over code). Every tunable inside them is a named key so the
brain can modulate it (law 2).

| Work-order | Carries | Front that consumes it |
|---|---|---|
| [`feedback_work_order`](feedback_work_order.md) — [example](feedback_work_order.json) | One player feedback request, from intake to `done`/`failed`/`human` | cloud_editor (triage -> gate wall -> deploy -> Telegram reply) |
| [`art_work_order`](art_work_order.md) — [example](art_work_order.json) | A T2 art request + its pixel acceptance gate | Flux.1-dev / PIL art front |
| [`new_game_work_order`](new_game_work_order.md) — [example](new_game_work_order.json) | A hub NEW-GAME request (P8 template instantiation) | Actions instantiation job |

The `.json` files here are worked examples (placeholder ids and Telegram ids) —
copy one to seed a real work-order, never edit these in place with live values.
