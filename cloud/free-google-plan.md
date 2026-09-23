# Free Google Stuff — Pipeline Integration Plan

*Written 2026-09-23. What Google gives away for free, and how the Pipeline takes it.*

## The inventory

| Google free thing | Quota | Pipeline use |
|---|---|---|
| **Firebase Test Lab** | ~10 virtual-device tests/day free | Real-device farm: every shipped game gets robo-tested on real phones, screenshots feed llama-3.2 vision |
| **Gemini API (AI Studio)** | ~1500 req/day, 15 RPM, vision-capable | Free LLM rung in the critic ladder — fixes the NVIDIA 504/rate-limit weakness |
| **Cloud Run** | 2M requests/mo, 360k GB-seconds | Free home for the multiplayer signaling + lobby server (WS supported) |
| **Colab** | T4 GPU sessions | Already our GPU (TRELLIS, jevlike training) — formalize as the on-call GPU worker |
| **Firestore** | 1 GiB, 50k reads/day | OPTIONAL: realtime lobby/queue via onSnapshot — only if we outgrow the WS server |

## Phases

### G1 — Test Lab device farm (#92, agent building now)
1. `daily/testlab_gate.py` — gcloud roo-runner + screenshot harvest (haiku agent running)
2. Godot **Android APK export presets** for sonar/slime-line/octogram-arcade + `android-apk.yml` Actions workflow (signed with a Pipeline keystore secret)
3. **[USER]** one-time `gcloud auth login` (or service-account JSON) on Rog
4. Wire as post-deploy gate: deploy → testlab_gate → vision verdict → diary

### G2 — Gemini free LLM rung
1. `vision_critic.py` + `llm_critic.py`: add Gemini Flash as a rung (vision + text, urllib REST, no new deps) after llama-3.3/llama-3.2, before openrouter
2. **[USER]** free API key from aistudio.google.com → `~/.gemini_key` on Rog + retromonkey
3. Ladder becomes: glm-5.3 → kimi-k3 → **gemini-flash** → openrouter/free — four rungs, two free

### G3 — Cloud Run multiplayer (#88)
1. `cloud/mp/` — signaling + lobby + checkers server (ws + REST, Telegram-auth stub), Dockerfile, `--local` test mode
2. **[USER]** gcloud run deploy (same auth as G1) + Caddy route `wss://retromonkey.com.au/mp` → Cloud Run URL
3. Free forever at our traffic

### G4 — Colab GPU worker (formalize)
1. One-command dispatch notebook (TRELLIS mesh / Flux art / jevlike train) + runbook
2. Stays manual-trigger (Colab can't be headlessly started on free tier)

## Order
G2 (hardens the brain) → G1 items 1-2 (agents, now) → G3 (server skeleton) → user auth steps → first Test Lab burn.
