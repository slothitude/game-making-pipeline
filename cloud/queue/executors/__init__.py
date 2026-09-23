"""GMP queue executors — the audit's missing wires, as one importable package.

Each module exposes `run(job) -> dict`, raising on failure (the router's
executor contract: an exception means the server-side retry law decides
retry/fail). stdlib + subprocess only; every module reconfigures stdout/stderr
to UTF-8 and narrates one line per action.

REGISTRY is the routing table the router picks up in one line (see README.md):

    from executors import REGISTRY
    EXECUTORS.update(REGISTRY)

Covered here: critique (+ its issue->job actioning wire), deploy,
generate_art, gpu.train / gpu.mesh / gpu.render (all exec_gpu with kind
injected), emulator. Still router-side stubs, deliberately: llm (the ladder
brain), device_test, gate.
"""

from . import exec_art, exec_critique, exec_deploy, exec_emulator, exec_gpu

__all__ = ["REGISTRY"]


def _gpu(kind):
    """gpu.train / gpu.mesh / gpu.render -> exec_gpu.run with kind injected."""
    def _run(job):
        payload = dict(job.get("payload") or {})
        payload.setdefault("kind", kind)
        return exec_gpu.run(dict(job, payload=payload))
    _run.__name__ = f"gpu_{kind}"
    _run.__doc__ = f"exec_gpu.run with payload['kind']={kind!r} injected"
    return _run


REGISTRY = {
    "critique": exec_critique.run,
    "deploy": exec_deploy.run,
    "generate_art": exec_art.run,
    "gpu.train": _gpu("train"),
    "gpu.mesh": _gpu("mesh"),
    "gpu.render": _gpu("render"),
    "emulator": exec_emulator.run,
}
