# SCULMM core — interface contract for the sibling fronts

F1 (core: schema/validate/compile) is built and green (15/15 gates).
If you are F2 (LLM world-generation) or F3 (browser player), this is
your contract. Files: `flux_check/cue_dev/src/sculmm_*.py`, test assets
in `flux_check/cue_dev/test/`.

## F2 — LLM world-generation front

**You produce:** `.sculmm.json` scripts (the v2 format). You consume a
text brief. You emit ONLY schema-valid JSON — `sculmm_json_schema()`
(draft 2020-12, `additionalProperties: false` everywhere) is the law;
run `python src/sculmm_validate.py <out.json> --clips src/clips.json
--book 1984` before claiming done. The repair loop feeds
`report["errors"]` (rule/path/error + nearest-suggestion) back to your
LLM — ≤3 retries, then flag to the coordinator.

Key shapes (see `test/scene.sculmm.json`, the living example):
- `world.rooms[]`: `name`, `backdrop` (existing asset path) OR
  `backdrop_prompt` (work order), `shell` (procedural 3D: floor
  `size_m` + `texture` slots, `walls[]`, `ceiling`), `props` (name
  refs), `floor_marks`, `depth_layers`
- `world.props[]`: `name`, `room`, `place` (closed anchor vocabulary:
  `center`, `against_wall:north|south|east|west`,
  `{"left_of": prop}`, `{"right_of": prop}`), and EXACTLY ONE gen slot:
  `image_prompt` (string ≤500) XOR `model_ref` (path)
- `world.actors[]`: `name` + `asset` = `tier` (A/B/C), asset path
  fields (`cutout_dir`/`mesh`/`rig`/`art`), `clips` (registry refs)
- `beats[]`: v1 time expressions (`scene:n`, `line:id`, float) +
  cues from the closed vocabulary — v1 cues plus `perform`
  (`actor`+`clip` registry ref) and `layer`
- `audio`: `clock: placeholder` + lines with `placeholder_dur_s`
  (vertical-slice mode)

**Stored-artifact law (amendment 1):** every generated thing records
its provenance IN the script (prompt + engine + seed in texture slots /
gen slots). Generation happens offline against those stored artifacts;
the compiler never calls you or any LLM. Content addressing (sha256)
and the asset-normalization contract (`units m, pivot floor, facing
+z, poly budget`) are stamped by the compiler into the manifest.

**Clip registry:** `src/clips.json` — `{ref: {path, location, dur_s,
src}}`. `location` tells the consumer which machine holds the asset
(`book` = relative to the book dir, `lappy` = the Lappy book root).
Adding a clip = one registry entry (the one-place law). Unknown refs
are rule-12 rejections + `missing_assets` entries.

## F3 — browser runtime / scrub player

**You consume:** `build/<name>.sculmmc.json` (compiled). Shape:

```
{ version: 2, book, scene, seed, mode: "sculmm2", clock,
  duration,                       # seconds
  beats: [ { at, at_seconds, t0, t1,
             cues: [ {...cue, t0, t1} ] } ],     # absolute seconds
  lines: [ {id, speaker, text, placeholder_dur_s} ],
  assets: { "<kind>:<ref>": { kind, ref, path, location,
                              norm, sha256?, dur_s? } },
  missing_assets: [ {kind, ref, hint} ],         # the work order
  hashes: { script, clips } }
```

- Scrub: `at_seconds..t1` windows are your timeline; `duration` the
  end. `lines[].placeholder_dur_s` is placeholder-clock truth (the
  readalong clock later overwrites via the same field shape).
- Render what exists, badge what's missing (missing_assets is your
  "asset pending" UI — a work order, not an error).
- Asset paths are machine-relative: `location: "book"` resolves under
  the book dir on whichever box serves files; `lappy` = fetch from the
  Lappy book root (`/mnt/seagate/audiobooks/<book>/`). `sha256` pins
  content — verify when you cache.

## Determinism + gates (both fronts)

Compile is byte-identical for identical inputs (proven: double-compile
byte-equal test). The battery `test/run_tests.py` (15 gates incl. 11
fault injections) is the regression harness — keep it green; add gates,
never weaken them. v1 (`src/cue_*.py` in the factory root and its
compiled cuec sheets) is LIVE read-only: import, never modify.
