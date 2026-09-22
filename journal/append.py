"""Append a dated entry to the Pipeline diary (called by workflows + agents).

Usage:
  python journal/append.py "Title of the event" "What happened, honestly." [image_path]

- Entries are APPENDED to journal/DIARY.md before the trailing footer block.
- An image path (relative to repo root, e.g. journal/images/foo.png) is
  embedded if given and the file exists.
- Nothing is ever rewritten — the format law.
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIARY = os.path.join(HERE, "DIARY.md")
FOOTER_MARKER = "*Next entries write themselves"


def append(title: str, body: str, image: str = "") -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## {stamp} — {title}", "", body.strip(), ""]
    if image and os.path.exists(os.path.join(HERE, "..", image)):
        lines += [f"![{title}](../{image})", ""]
    entry = "\n".join(lines)
    text = open(DIARY, encoding="utf-8").read()
    if FOOTER_MARKER in text:
        head, tail = text.split(FOOTER_MARKER, 1)
        footer = FOOTER_MARKER + tail
        text = head.rstrip() + "\n\n---\n\n" + entry + "\n" + footer
    else:
        text = text.rstrip() + "\n\n---\n\n" + entry + "\n"
    open(DIARY, "w", encoding="utf-8", newline="\n").write(text)
    print(f"diary entry appended: {title}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    append(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
