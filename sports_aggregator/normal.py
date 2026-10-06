"""Small numpy-only normal CDF (scipy is not a production dependency)."""
from __future__ import annotations

import math

import numpy as np


def ndtr(x: np.ndarray | float) -> np.ndarray:
    """Standard normal CDF via the Abramowitz-Stegun erf fit (|err| < 1.5e-7); numpy only."""
    z = np.asarray(x, dtype=float) / math.sqrt(2.0)
    sign = np.sign(z)
    a = np.abs(z)
    t = 1.0 / (1.0 + 0.3275911 * a)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    erf = sign * (1.0 - poly * np.exp(-a * a))
    return 0.5 * (1.0 + erf)
