"""Fixed-probability scorer — the fake brain for local selfplay verification.

Same duck type the recorder consumes from NumpyScorer: a module-level
``probabilities(context, options) -> list[float]``. Weights descend by option
position, so a run at temperature 0.35 mostly takes the early options but
still lands deeper ones — the point is to show the sampling law, not a
policy. No npz, no numpy.
"""
from __future__ import annotations


def probabilities(context: str, options: list[str]) -> list[float]:
    count = max(len(options), 1)
    weights = [float(count - index) for index in range(count)]
    total = sum(weights)
    return [weight / total for weight in weights]
