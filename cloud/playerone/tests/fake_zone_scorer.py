"""Fixed-probability zone scorer — the fake brain for generic_player's local
verification.

Same duck type the player consumes from NumpyScorer: a module-level
``probabilities(context, options) -> list[float]``. On any live context the
weights descend by option position, so a run at temperature 0.4 spreads over
the zone vocabulary (the sampling law on show). On a motion-none context —
the dead fixture, which ignores input and never animates — it parks on wait,
which is exactly what drives the anti-stuck law. No npz, no numpy.
"""
from __future__ import annotations


def probabilities(context: str, options: list[str]) -> list[float]:
    count = max(len(options), 1)
    if "motion none" in context:
        weights = [3.0 if option == "wait" else 0.1 for option in options]
    else:
        weights = [float(count - index) for index in range(count)]
    total = sum(weights)
    return [weight / total for weight in weights]
