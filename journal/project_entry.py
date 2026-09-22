"""Append a phase entry to a project's own DIARY.md (the per-project diary law).

Every milestone that passes its gate wall gets an entry in ITS repo's diary:
  python journal/project_entry.py <repo_path> "<title>" "<body>" [image_rel_path]

Format law identical to the global diary: dated entries, appended, never
rewritten, failures recorded too. Images relative to the repo root.
"""
import datetime
import os
import sys

HEADER = "# {name} — Project Diary\n\n*Phase-by-phase log. Entries append, never rewrite. Nothing is done until its wall is green twice.*\n\n---\n"


def append(repo: str, title: str, body: str, image: str = "") -> None:
    diary = os.path.join(repo, "DIARY.md")
    name = os.path.basename(os.path.abspath(repo))
    if not os.path.exists(diary):
        open(diary, "w", encoding="utf-8", newline="\n").write(HEADER.format(name=name))
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## {stamp} — {title}", "", body.strip(), ""]
    if image and os.path.exists(os.path.join(repo, image)):
        lines += [f"![{title}]({image})", ""]
    with open(diary, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{name}: diary entry appended — {title}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    append(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
