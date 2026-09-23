#!/usr/bin/env python3
"""One-shot: harden exec_milestone's host parsing against scheme-carrying env values."""
p = "/home/ubuntu/pipeline/queue/executors/exec_milestone.py"
s = open(p, encoding="utf-8").read()
if "removeprefix" not in s:
    old = '    host = os.environ.get("FORGEJO_HOST", FORGEJO_HOST)'
    new = ('    host = os.environ.get("FORGEJO_HOST", FORGEJO_HOST).strip()'
           '.removeprefix("https://").removeprefix("http://")')
    assert old in s, "host line not found"
    s = s.replace(old, new)
    open(p, "w", encoding="utf-8").write(s)
    print("hardened")
else:
    print("already")
