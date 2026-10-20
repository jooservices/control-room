"""Small, deterministic calculations used by the snapshot assembler."""

from statistics import quantiles
from typing import Sequence


def eta(
    remaining: int | float,
    cycle_samples: Sequence[int | float],
    workers: int | float | None,
) -> dict[str, float] | None:
    """Estimate remaining minutes from completed ticket cycles and worker capacity.

    Quartiles use inclusive linear interpolation. Fewer than five samples, no
    available workers, or a negative remaining count cannot support an ETA.
    """
    if len(cycle_samples) < 5 or workers is None or workers <= 0 or remaining < 0:
        return None

    quartiles = quantiles(sorted(cycle_samples), n=4, method="inclusive")
    workload = remaining / workers
    return {
        "low": quartiles[0] * workload,
        "middle": quartiles[1] * workload,
        "high": quartiles[2] * workload,
    }
