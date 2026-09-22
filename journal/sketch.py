"""Sketch generator for project diaries — devlog-style concept sketches.

  python journal/sketch.py <repo_path> "<scene description>" "<sketch name>"

Generates a rough pencil/ink concept sketch of the described scene via Flux,
saves it under <repo>/diary_images/, returns the relative path to embed.
Style law: rough pencil sketch on warm paper, loose lines, storybook devlog feel.
"""
import base64
import datetime
import io
import json
import os
import sys
import urllib.request

API = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"
STYLE = ("rough pencil sketch on warm cream paper, loose confident lines, "
         "light shading, game devlog concept art, no text")


def sketch(repo: str, scene: str, name: str = "") -> str:
    key = os.environ.get("NVAPI_KEY")
    if not key:
        raise SystemExit("NVAPI_KEY not set")
    out_dir = os.path.join(repo, "diary_images")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%H%M%S")
    fname = f"sketch_{name or stamp}.png".replace(" ", "_")
    body = json.dumps({"prompt": scene + ", " + STYLE, "cfg_scale": 3.5,
                       "steps": 20, "seed": hash(scene) % 9999}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": f"Bearer {key}", "Accept": "application/json",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode())
    raw = base64.b64decode(data["artifacts"][0]["base64"])
    with open(os.path.join(out_dir, fname), "wb") as f:
        f.write(raw)
    print(f"diary_images/{fname}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    sketch(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
