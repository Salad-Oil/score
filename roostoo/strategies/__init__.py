"""Pluggable strategy implementations.

``STRATEGY=roostoo.strategies.mean_reversion:MeanReversionStrategy`` selects one
by ``module:Class``. Adding a strategy means adding a module here; nothing in the
engine changes.
"""

from .base import (
    ENTER_LONG,
    ENTER_SHORT,
    EXIT_LONG,
    EXIT_SHORT,
    HOLD,
    MarketContext,
    Signal,
    Strategy,
    load_strategy,
)
from .mean_reversion import MeanReversionStrategy

__all__ = [
    "ENTER_LONG",
    "ENTER_SHORT",
    "EXIT_LONG",
    "EXIT_SHORT",
    "HOLD",
    "MarketContext",
    "MeanReversionStrategy",
    "Signal",
    "Strategy",
    "load_strategy",
]
