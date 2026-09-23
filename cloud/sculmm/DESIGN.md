# SCULMM — Script Creation Unity for LLMs
## Engine Design Law (informed by the pilots + godot-escoria)

*Written 2026-09-23. Sources: the session's pilot history (v2 furnished world, v3 camera round) + [godot-escoria](https://github.com/godot-escoria)'s architecture.*

### What SCULMM is
A 3D game creation unit that LLMs drive end-to-end: the Pipeline's agents write the
world (manifests, props, clips, camera laws), the Colab lane manufactures the assets
(Hunyuan3D-2 textured meshes, animation), and the engine assembles it into a playable
build — gates included.

### Escoria principles adopted (why)
From [escoria-core](https://github.com/godot-escoria/escoria-core) and its ecosystem:

1. **Core as a plugin, not a fork** — engine logic overlays Godot as a plugin
   (escoria-core's law). SCULMM's F1 core stays a Godot addon; games never fork it.
2. **Swappable subsystems behind the core** — Escoria ships interchangeable UI
   modules (simple mouse, 9-verb) and pluggable dialog managers. SCULMM's fronts
   (input scheme, camera rig, render pass) are the same: registered implementations
   behind one interface, swapped by manifest — an LLM changes a string, not code.
3. **Template + demo pairing** — escoria-game-template (blank) beside
   escoria-demo-game (complete reference). SCULMM keeps the pilot v2 world as the
   reference project; every new game instantiates the template and imitates the demo.
4. **Content separated from engine** — game content is data (rooms, actors, events in
   Escoria's case; manifests, props, clips in ours), never engine code. The LLM
   authors data; the engine interprets it.
5. **Declarative authoring surface** — Escoria's ESC scripts read like stage
   directions. SCULMM's equivalent: the prop manifest + clips registry + camera
   config (the v3 named config precedent — a camera is a JSON, not a scene edit).

### The pilots' proven laws (session history)
- **F1 engine core / F2 world-gen / F3 player / F4 asset+render** — the four fronts,
  each with its own battery (15/15 green at pilot v2).
- **Prop manifest law**: generated meshes resolve as `assets/props/<name>/` +
  `manifest.json` entries (`colab-hunyuan3d-2-textured`, `colab-anim` for animated);
  the world-gen reads the manifest, never hardcoded props.
- **Bind law (v3)**: RIGID nearest-segment bind — ARMATURE_AUTO weights were inert
  (the actor-stood-at-origin disease). Animation lanes carry `bind:
  "rigid-nearest-segment"` in their result contracts.
- **Camera law (v3)**: SPRING ARM damped follow (k=5.0, arm offset, Track To) +
  DOF on the spring target — a NAMED CONFIG, swappable per the subsystem principle.
- **Amber-placeholder doctrine (v2)**: no feature waits on art — placeholders ship,
  the mesh lane replaces them by manifest, gates stay green throughout.

### The lane map (all Colab CLI — Lappy is out, owner directive)
```
queue gpu.mesh  → Colab T4 → Hunyuan3D-2 shape → del+cache → paint (TEXTURED)
                → normalize (m/floor/+z, ~50k) → GLB → prop_order resolve → manifest
queue gpu.anim  → Colab Blender → GLB + rigid bind + BVH retarget → animated GLB
queue gpu.render→ Colab Blender → frames
pi (openrouter/free) writes engine code + manifests → Forgejo → Actions gates
```

### The laws (numbered, like the Pipeline's)
1. Engine = plugin; games = data. Nothing forks.
2. Subsystems are swappable strings in a manifest.
3. Template + reference demo always both exist and both stay green.
4. Every asset carries provenance in the manifest (source, faces, textured, bind).
5. Placeholders first, meshes replace — gates never wait on art.
6. All GPU on the Colab CLI; teardown ALWAYS.
7. The LLM authors declarations; the engine interprets; the gates judge both.
