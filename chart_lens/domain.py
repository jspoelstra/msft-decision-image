"""Calendar, price-basis, and experiment rules shared by both workflows."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cached_property
from math import prod
from typing import Literal

import exchange_calendars as xcals

Scheme = Literal["claim"]
EXPERIMENT: str = "drawdown-comparison-v1"
CLAIM: str = (
    "The target experienced a smaller maximum drawdown than the comparison "
    "over the displayed 30 sessions."
)
SCHEMES: dict[str, dict[str, str]] = {
    "claim": {
        "supported": "Supported",
        "contradicted": "Contradicted",
        "insufficient": "Insufficient visual evidence",
    },
    "bands": {
        "down_large": "Down more than 1%",
        "down_small": "Down 0.5% to 1%",
        "stable": "Within +/-0.5%",
        "up_small": "Up 0.5% to 1%",
        "up_large": "Up more than 1%",
    },
    "binary": {"up": "Up", "not_up": "Down or unchanged"},
}
CRITERIA: dict[Scheme, dict[str, str]] = {
    "claim": {
        "supported": "Target's maximum closing-price drawdown is strictly smaller than Comparison's.",
        "contradicted": "Target's maximum closing-price drawdown is greater than or equal to Comparison's.",
        "insufficient": "The image does not allow the drawdown comparison to be resolved reliably.",
    },
}
GRACE: timedelta = timedelta(hours=1)


class ExperimentError(Exception):
    """A safe, actionable experiment failure."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Sessions:
    """NYSE regular-session calendar, including scheduled early closes."""

    @cached_property
    def market(self) -> xcals.ExchangeCalendar:
        """Load a single calendar covering supported historical requests."""
        return xcals.get_calendar("XNYS", start="1989-01-01", end="2100-12-31")

    @cached_property
    def dates(self) -> tuple[date, ...]:
        """Exchange-local session labels in ascending order."""
        return tuple(stamp.date() for stamp in self.market.sessions)

    def on_or_after(self, day: date) -> int:
        """Find the first trading session on or after a date."""
        return bisect_left(self.dates, day)

    def latest(self, now: datetime | None = None) -> int:
        """Find the latest session that has cleared the acceptance delay."""
        now = now or datetime.now(UTC)
        index = bisect_right(self.dates, now.date()) - 1
        close = self.market.session_close(self.dates[index].isoformat()).to_pydatetime()
        return index if now >= close + GRACE else index - 1


@dataclass(frozen=True)
class Bar:
    """Positive USD high, low, and close on the original share basis."""

    high: Decimal
    low: Decimal
    close: Decimal

    def scale(self, factor: Decimal) -> "Bar":
        """Convert all retained prices to the same share basis."""
        return Bar(self.high * factor, self.low * factor, self.close * factor)


@dataclass(frozen=True)
class History:
    """Validated original-basis bars and effective split ratios."""

    bars: dict[date, Bar]
    splits: dict[date, Decimal]

    def on_basis(self, day: date, basis: date) -> Bar:
        """Value a day's bar in shares effective on a later session."""
        divisor = prod(
            (ratio for effective, ratio in self.splits.items() if day < effective <= basis),
            start=Decimal(1),
        )
        return self.bars[day].scale(1 / divisor)

    def change(self, previous: date, target: date) -> Decimal:
        """Calculate a split-neutral, non-dividend-adjusted close-to-close return."""
        return self.bars[target].close / self.on_basis(previous, target).close - 1


@dataclass(frozen=True)
class ChartInput:
    """Only origin-bounded values, without ticker, dates, or future labels."""

    close: tuple[float, ...]
    high: float
    low: float
    relative: dict[str, tuple[float, ...]]


def _window_closes(
    histories: dict[str, History], ticker: str, comparison: str, sessions: Sessions, index: int
) -> dict[str, tuple[Decimal, ...]]:
    """Share the exact aligned, split-neutral window between chart and evaluator."""
    origin = sessions.dates[index]
    window = sessions.dates[index - 29 : index + 1]
    if len(window) != 30:
        raise ExperimentError("insufficient_history", "A claim needs 30 completed sessions.")
    try:
        return {
            role: tuple(histories[symbol].on_basis(day, origin).close for day in window)
            for role, symbol in (("Target", ticker), ("Comparison", comparison))
        }
    except KeyError as exc:
        raise ExperimentError(
            "missing_bar", f"Missing required session or symbol: {exc}."
        ) from None


def chart_input(
    histories: dict[str, History], ticker: str, comparison: str, sessions: Sessions, index: int
) -> ChartInput:
    """Extract anonymous paired closes and the target's trailing extremes."""
    origin = sessions.dates[index]
    closes = _window_closes(histories, ticker, comparison, sessions, index)
    yearly = sessions.dates[sessions.on_or_after(origin - timedelta(weeks=52)) : index + 1]
    try:
        levels = [histories[ticker].on_basis(day, origin) for day in yearly]
    except KeyError as exc:
        raise ExperimentError(
            "missing_bar", f"Missing required session or symbol: {exc}."
        ) from None
    return ChartInput(
        tuple(map(float, closes["Target"])),
        float(max(bar.high for bar in levels)),
        float(min(bar.low for bar in levels)),
        {
            name: tuple(float(100 * (value / values[0] - 1)) for value in values)
            for name, values in closes.items()
        },
    )


def window_drawdowns(
    histories: dict[str, History], ticker: str, comparison: str, sessions: Sessions, index: int
) -> tuple[Decimal, Decimal]:
    """Calculate positive peak-to-later-trough losses, bounded to the chart."""
    closes = _window_closes(histories, ticker, comparison, sessions, index)

    def maximum_drawdown(values: tuple[Decimal, ...]) -> Decimal:
        peak, largest = values[0], Decimal(0)
        for value in values:
            peak = max(peak, value)
            largest = max(largest, 1 - value / peak)
        return largest

    return maximum_drawdown(closes["Target"]), maximum_drawdown(closes["Comparison"])
