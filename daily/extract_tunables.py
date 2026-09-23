"""extract_tunables — turn a game's feel constants into crew-editable JSON.

  python daily/extract_tunables.py                 # all games
  python daily/extract_tunables.py --game sonar    # one game
  python daily/extract_tunables.py --game sonar --root C:/some/sonar

Reads <game>/scripts/feel.gd (the spec law "constants_not_magic" file), parses
every `const NAME := value` line, and writes <game>/data/tunables.json with the
value, a human-readable description (from the code comments, falling back to a
prettified name) and a sane edit range. update_tunables.py consumes that JSON
and writes values back into the same const lines, so this extractor and that
handler must agree on the format: one entry per const, keyed by the crew-facing
tunable name, with "const" recording the real GDScript const when they differ.

Env: GAME_ROOTS (optional JSON mapping slug -> repo dir).
"""
import datetime
import json
import os
import re
import sys

# crew messages carry emoji; a cp1252 Windows console must not kill the lane
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
GMP = os.path.dirname(HERE)

# Where the game repos live on Rog. Override with GAME_ROOTS='{"sonar": "..."}'.
GAME_ROOTS = {
    "sonar": "C:/Users/aaron/sonar",
    "slime-line": "C:/Users/aaron/slime-line",
    "octogram-arcade": "C:/Users/aaron/octogram-arcade",
}

# sonar has no single feel file? It does. octogram-arcade predates the feel.gd
# law — its tunables file is rpg_config.gd ("Every tunable RPG number... pure
# data"). Candidates are tried in order; a missing file falls through.
FEEL_FILES = {
    "sonar": ["scripts/feel.gd"],
    "slime-line": ["scripts/feel.gd"],
    "octogram-arcade": ["scripts/rpg_config.gd", "scripts/feel.gd"],
}

# Curated entries: the crew-facing names, descriptions and ranges authored with
# the work-order system (not guessed). "const" is the real GDScript const when
# the crew name differs (players say MAX_DEPTH_BASE; the const is
# PRESSURE_MAX_DEPTH). Every other const gets an auto description + range.
CURATED = {
    ("sonar", "PING_COOLDOWN"): {"description": "Seconds between sonar pings", "range": [1.0, 10.0]},
    ("sonar", "REVEAL_TIME"): {"description": "How long entities stay fully visible", "range": [0.5, 5.0]},
    ("sonar", "ECHO_FADE"): {"description": "Seconds for echoes to fade out", "range": [1.0, 10.0]},
    ("sonar", "AIR_SECONDS"): {"description": "Starting air supply", "range": [30, 180]},
    ("sonar", "MAX_DEPTH_BASE"): {"description": "Base crush depth", "range": [400, 2000],
                                  "const": "PRESSURE_MAX_DEPTH"},
    ("sonar", "LURKER_MAX_SPEED"): {"description": "How fast lurkers chase", "range": [100, 400]},
}


def curated_for(game, const_name):
    """-> (crew_name, entry) for a curated hit, else (const_name, None)."""
    for (g, crew_name), entry in CURATED.items():
        if g == game and entry.get("const", crew_name) == const_name:
            return crew_name, entry
    return const_name, None

CONST_RE = re.compile(
    r"^const\s+([A-Za-z_][A-Za-z0-9_]*)"      # 1: const name
    r"(?:\s*:\s*([^=\n]+?))?"                  # 2: optional type annotation
    r"\s*(?::=|=)\s*",                         # := or =
)


def prettify(name):
    """AIR_SECONDS -> Air seconds."""
    return name.replace("_", " ").capitalize()


def scan_statement(text, start):
    """Walk from `start` to the end of the const's value expression.

    Returns (value_text, inline_comment). Tracks bracket depth and string
    literals so multi-line arrays/dicts and '#' inside strings don't end the
    statement early; a depth-0 '#' starts the trailing comment.
    """
    out, comment = [], ""
    depth = 0
    i, n = start, len(text)
    in_str = ""
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == in_str:
                in_str = ""
            i += 1
            continue
        if ch in "\"'":
            in_str = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            if depth == 0:
                end = text.find("\n", i)
                comment = text[i + 1:end if end != -1 else n].strip()
                break
        elif ch in "([{":
            depth += 1
            out.append(ch)
        elif ch in ")]}":
            depth -= 1
            out.append(ch)
            if depth == 0:
                # statement may end here; peek past spaces/comments for safety
                i += 1
                break
        elif ch == "\n" and depth == 0:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip(), comment


def parse_consts(source):
    """-> ordered list of (name, type_annotation, value_text, description).

    Description = trailing comment, else the preceding ##/# doc block, else "".
    """
    consts, doc = [], []
    offset = 0
    for line in source.splitlines(True):  # keepends so offsets track source
        stripped = line.rstrip("\n")
        lead = len(stripped) - len(stripped.lstrip())
        m = CONST_RE.match(stripped, lead) if not stripped.lstrip().startswith("#") else None
        if m:
            value, inline = scan_statement(source, offset + m.end())
            desc = inline or " ".join(doc).strip()
            doc = []
            consts.append((m.group(1), (m.group(2) or "").strip(), value, desc))
        elif stripped.strip().startswith("#"):
            doc.append(stripped.strip().lstrip("#").strip())
        elif stripped.strip():
            doc = []
        offset += len(line)
    return consts


NUM_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?$")


def _num(tok):
    tok = tok.strip()
    if NUM_RE.match(tok):
        return int(tok)
    if FLOAT_RE.match(tok):
        return float(tok)
    return None


def classify(text):
    """-> (type_name, value, editable). Non-literals become 'expr' (derived)."""
    t = text.strip()
    if not t:
        return "expr", None, False
    if NUM_RE.match(t):
        return "int", int(t), True
    if FLOAT_RE.match(t):
        return "float", float(t), True
    if t in ("true", "false"):
        return "bool", t == "true", True
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return "string", t[1:-1], False
    m = re.match(r"^Vector2(i?)\s*\(\s*(.+?)\s*\)$", t)
    if m:
        parts = [p.strip() for p in m.group(2).split(",")]
        nums = [_num(p) for p in parts[:2]]
        if all(v is not None for v in nums):
            key = ("x", "y") if m.group(1) else ("x", "y", "z")
            return "vector" + m.group(1), dict(zip(key, nums)), False
        return "expr", None, False
    m = re.match(r"^Color\s*\(\s*(.+?)\s*\)$", t)
    if m:
        nums = [_num(p.strip()) for p in m.group(1).split(",")]
        if nums and all(v is not None for v in nums):
            nums += [1.0] * (4 - len(nums))
            rgb = "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in nums[:3])
            return "color", f"#{rgb}" + (f"{round(nums[3] * 255):02x}" if nums[3] < 1.0 else ""), False
        return "expr", None, False
    if t.startswith("["):
        inner = t[1:-1] if t.endswith("]") else t[1:]
        items, depth, cur, in_str = [], 0, "", ""
        for ch in inner:
            if in_str:
                cur += ch
                if ch == in_str:
                    in_str = ""
                continue
            if ch in "\"'":
                in_str = ch
                cur += ch
            elif ch in "([{":
                depth += 1
                cur += ch
            elif ch in ")]}":
                depth -= 1
                cur += ch
            elif ch == "," and depth == 0:
                items.append(cur)
                cur = ""
            else:
                cur += ch
        if cur.strip():
            items.append(cur)
        parsed = []
        for it in items:
            ty, val, _ = classify(it)
            parsed.append(val if val is not None else it.strip())
        return "array", parsed, False
    if t.startswith("{"):
        return "dict", None, False
    return "expr", None, False


def default_range(value):
    """Half/double the starting value — a hint, never a law."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        if 0 <= value <= 1:
            return [0, 4]
        lo = value // 2 if value > 0 else value * 2
        hi = value * 2 if value > 0 else min(0, value // 2)
        return [lo, hi]
    if isinstance(value, float):
        if value == 0.0:
            return [-1.0, 1.0]
        lo, hi = (value / 2, value * 2) if value > 0 else (value * 2, value / 2)
        return [round(lo, 4), round(hi, 4)]
    return None


def extract(game, root):
    feel = None
    for cand in FEEL_FILES.get(game, ["scripts/feel.gd"]):
        if os.path.exists(os.path.join(root, cand)):
            feel = cand
            break
    if not feel:
        # fall back to the const-heaviest script in the repo
        best, count = None, 0
        sdir = os.path.join(root, "scripts")
        if os.path.isdir(sdir):
            for f in sorted(os.listdir(sdir)):
                if not f.endswith(".gd"):
                    continue
                c = len(CONST_RE.findall(open(os.path.join(sdir, f), encoding="utf-8").read()))
                if c > count:
                    best, count = f"scripts/{f}", c
        feel = best
    if not feel:
        raise SystemExit(f"{game}: no tunables file found under {root}/scripts")

    source = open(os.path.join(root, feel), encoding="utf-8").read()
    parsed = parse_consts(source)

    tunables, curated_seen = {}, set()
    for name, anno, value_text, desc in parsed:
        type_name, value, editable = classify(value_text)
        crew_name, hit = curated_for(game, name)
        if hit:
            curated_seen.add(crew_name)
        tunables[crew_name] = {
            "value": value,
            "description": (hit or {}).get("description") or desc or prettify(name),
            "range": (hit or {}).get("range") or default_range(value),
            "editable": editable,
            "type": type_name,
            "const": name,
        }

    # curated names with no const in the file (typo guard) — surface, don't hide
    missing = [k for (g, k) in CURATED
               if g == game and (CURATED[(g, k)].get("const", k) not in
                                 {name for name, _, _, _ in parsed})]
    data = {
        "game": game,
        "source": feel,
        "extracted": datetime.datetime.now().isoformat(timespec="seconds"),
        "tunables": tunables,
    }
    if feel != "scripts/feel.gd":
        data["note"] = (f"{game} has no scripts/feel.gd — tunables extracted from {feel} "
                        "(the repo's constants_not_magic file)")
    out_path = os.path.join(root, "data", "tunables.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return out_path, len(tunables), missing


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game", help="one game slug (default: all)")
    ap.add_argument("--root", help="override the game repo directory")
    args = ap.parse_args()

    roots = dict(GAME_ROOTS)
    try:
        roots.update(json.loads(os.environ.get("GAME_ROOTS", "{}")))
    except ValueError:
        pass

    games = [args.game] if args.game else list(roots)
    if args.game and args.game not in roots and not args.root:
        raise SystemExit(f"unknown game {args.game!r} — pass --root or set GAME_ROOTS")

    for game in games:
        root = args.root or roots.get(game)
        if not root or not os.path.isdir(root):
            print(f"SKIP {game}: no repo dir at {root}")
            continue
        path, n, missing = extract(game, root)
        print(f"{game}: {n} tunables -> {path}")
        if missing:
            print(f"  WARNING curated tunables not found in source: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
