"""The strategy interface.

A strategy is a pure function of market state: it receives closed candles and the
current book, and returns intents. It never touches the network, never sizes a
position and never decides whether it is *allowed* to trade -- sizing belongs to
the risk layer and approval belongs to the engine. Keeping that separation is
what makes the same strategy code run unchanged in the backtester and live.

Because the live loop and the backtest both call :meth:`Strategy.generate` once
per **closed bar**, a signal cannot accidentally depend on sampling frequency.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..candles import Candle
from ..models import Position, Ticker

# Signal actions.
ENTER_LONG = "ENTER_LONG"
ENTER_SHORT = "ENTER_SHORT"
EXIT_LONG = "EXIT_LONG"
EXIT_SHORT = "EXIT_SHORT"
HOLD = "HOLD"

ENTRY_ACTIONS = (ENTER_LONG, ENTER_SHORT)
EXIT_ACTIONS = (EXIT_LONG, EXIT_SHORT)


@dataclass(frozen=True)
class Signal:
    """A trading intent, with enough context to audit it later."""

    pair: str
    action: str
    strength: float = 1.0
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.action in ENTRY_ACTIONS

    @property
    def is_exit(self) -> bool:
        return self.action in EXIT_ACTIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "action": self.action,
            "strength": round(self.strength, 4),
            "reason": self.reason,
            "meta": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.meta.items()},
        }


@dataclass
class MarketContext:
    """Everything a strategy may look at, assembled once per bar.

    ``candles`` holds **closed** bars only, oldest first, and includes the bar
    that just finished. The still-forming bar is deliberately excluded: acting on
    a partial bar would make the live bot and the backtest disagree.
    """

    now_ms: int
    bar_seconds: int
    candles: dict[str, list[Candle]]
    tickers: dict[str, Ticker] = field(default_factory=dict)
    nav: float = 0.0
    cash_usd: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    universe: list[str] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)
    daily_halt: bool = False
    bar_index: int = 0
    state: dict[str, Any] = field(default_factory=dict)
    is_backtest: bool = False

    # -- convenience accessors -----------------------------------------
    def series(self, pair: str) -> list[Candle]:
        return self.candles.get(pair, [])

    def has_bars(self, pair: str, count: int) -> bool:
        return len(self.candles.get(pair, [])) >= count

    def closes(self, pair: str) -> list[float]:
        return [c.close for c in self.candles.get(pair, [])]

    def highs(self, pair: str) -> list[float]:
        return [c.high for c in self.candles.get(pair, [])]

    def lows(self, pair: str) -> list[float]:
        return [c.low for c in self.candles.get(pair, [])]

    def volumes(self, pair: str) -> list[float]:
        return [c.volume for c in self.candles.get(pair, [])]

    def bar_count(self, pair: str) -> int:
        return len(self.candles.get(pair, []))

    def price(self, pair: str) -> Optional[float]:
        """Latest close, or the live mid if no bar exists yet."""
        series = self.candles.get(pair)
        if series:
            return series[-1].close
        ticker = self.tickers.get(pair)
        return ticker.mid if ticker else None

    def held_pairs(self) -> set[str]:
        return {p for p, pos in self.positions.items() if pos.quantity > 0 or pos.is_short}

    def open_position_count(self) -> int:
        return len(self.held_pairs())


class Strategy(ABC):
    """Base class. Subclasses implement :meth:`generate`."""

    name = "strategy"
    #: Bars of history required before ``generate`` can produce anything useful.
    min_bars = 1

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        self.params: dict[str, Any] = dict(self.default_params())
        if params:
            unknown = set(params) - set(self.params)
            if unknown:
                # A typo'd parameter silently doing nothing is a classic way to
                # spend a week backtesting the wrong configuration.
                raise ValueError(
                    f"{self.name}: unknown parameter(s) {sorted(unknown)}; "
                    f"known parameters are {sorted(self.params)}"
                )
            self.params.update(params)
        #: Per-pair explanation of the last decision, for the journal.
        self.diagnostics: dict[str, dict[str, Any]] = {}

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {}

    @property
    def required_bars(self) -> int:
        """Bars needed before this strategy can produce a meaningful signal.

        Exposed so the backtester and the universe filter can exclude pairs that
        are still warming up instead of silently trading on partial indicators.
        """
        return self.min_bars

    @property
    def max_context_bars(self) -> int:
        """How many trailing bars of history this strategy wants to see.

        Defaults to ``required_bars``. The engine and the backtester hand over
        exactly this much, which keeps long backtests fast: giving a strategy the
        engine's entire rolling buffer would make every indicator rescan hundreds
        of bars for every pair on every bar.

        Override upward only if a lookback genuinely needs more than
        ``required_bars``.
        """
        return self.required_bars

    def param(self, key: str) -> Any:
        return self.params[key]

    def prepare(self, ctx: MarketContext) -> None:
        """Optional hook to warm up state from seeded history. Default: no-op."""

    def note(self, pair: str, **fields: Any) -> None:
        """Record why a pair was or was not traded this bar."""
        entry = self.diagnostics.setdefault(pair, {})
        entry.update(fields)

    def reset_diagnostics(self) -> None:
        self.diagnostics = {}

    def describe(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in sorted(self.params.items()))})"

    @abstractmethod
    def generate(self, ctx: MarketContext) -> list[Signal]:
        """Return this bar's intents. Exits are expected before entries."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_BUILTIN_ALIASES = {
    "mean_reversion": "roostoo.strategies.mean_reversion:MeanReversionStrategy",
    "trend": "roostoo.strategies.mean_reversion:MeanReversionStrategy",
}


def load_strategy(spec: str, params: Optional[dict[str, Any]] = None) -> Strategy:
    """Build a strategy from a ``"module.path:ClassName"`` spec.

    ``spec`` may also be a built-in alias such as ``"mean_reversion"``.
    """
    target = _BUILTIN_ALIASES.get(spec.strip().lower(), spec.strip())
    if ":" not in target:
        raise ValueError(f"strategy spec must be 'module:Class' or a known alias, got {spec!r}")
    module_path, _, class_name = target.partition(":")
    module = importlib.import_module(module_path)
    try:
        klass = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(f"{module_path} has no attribute {class_name!r}") from exc
    if not (isinstance(klass, type) and issubclass(klass, Strategy)):
        raise ValueError(f"{target} is not a Strategy subclass")
    return klass(params or {})


def filter_pairs(signals: Iterable[Signal], pairs: Iterable[str]) -> list[Signal]:
    """Keep only signals for an allowed pair set."""
    allowed = set(pairs)
    return [s for s in signals if s.pair in allowed]
