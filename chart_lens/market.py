"""Yahoo and deterministic synthetic market-data adapters."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from math import prod
from random import Random
from typing import Protocol

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFException, YFRateLimitError

from chart_lens.domain import Bar, ExperimentError, History, Sessions

DOWNLOAD_OPTIONS = {
    "interval": "1d",
    "prepost": False,
    "auto_adjust": False,
    "back_adjust": False,
    "actions": True,
    "repair": False,
    "rounding": False,
    "keepna": True,
    "timeout": 12,
    "raise_errors": True,
}


class Market(Protocol):
    """Source of regular-session price histories."""

    def fetch(self, symbols: tuple[str, ...], start: date, end: date) -> dict[str, History]:
        """Return histories from start through end, inclusive."""


def decode_history(table: pd.DataFrame, symbol: str, *, start: date, end: date) -> History:
    """Normalize split-adjusted Yahoo OHLC back to the original share basis."""
    required = {"High", "Low", "Close", "Stock Splits"}
    if table.empty or not required.issubset(table.columns):
        raise ExperimentError("no_history", f"No usable Yahoo history for {symbol}.", 502)
    if not table.index.is_unique or not table.index.is_monotonic_increasing:
        raise ExperimentError("invalid_history", f"Invalid Yahoo session index for {symbol}.", 502)

    values = list(table[list(sorted(required))].itertuples(index=True, name=None))
    splits: dict[date, Decimal] = {}
    bars: dict[date, Bar] = {}
    for stamp, close, high, low, split in values:
        day = stamp.date()
        if pd.notna(split) and split != 0:
            ratio = Decimal(str(split))
            if not ratio.is_finite() or ratio <= 0:
                raise ExperimentError("invalid_split", f"Invalid split ratio for {symbol}.", 502)
            splits[day] = ratio
        if not start <= day <= end or any(pd.isna(v) for v in (close, high, low)):
            continue
        c, h, low_price = (Decimal(str(v)) for v in (close, high, low))
        if not all(x.is_finite() and x > 0 for x in (c, h, low_price)) or not low_price <= c <= h:
            raise ExperimentError(
                "invalid_history", f"Invalid Yahoo prices for {symbol} on {day}.", 502
            )
        if day in bars:
            raise ExperimentError("invalid_history", f"Duplicate Yahoo session for {symbol}.", 502)
        bars[day] = Bar(h, low_price, c)

    if not bars:
        raise ExperimentError("no_history", f"No usable Yahoo history for {symbol}.", 502)
    original = {
        day: bar.scale(
            prod(
                (ratio for effective, ratio in splits.items() if day < effective), start=Decimal(1)
            )
        )
        for day, bar in bars.items()
    }
    return History(original, {day: ratio for day, ratio in splits.items() if day <= end})


class YahooMarket:
    """Unofficial Yahoo download adapter; no account or private credentials."""

    def fetch(self, symbols: tuple[str, ...], start: date, end: date) -> dict[str, History]:
        """Fetch actions through today, then strip out bars beyond the accepted end."""
        through = max(end, datetime.now(UTC).date()) + timedelta(days=1)
        return {symbol: self._one(symbol, start, end, through) for symbol in symbols}

    @staticmethod
    def _one(symbol: str, start: date, end: date, through: date) -> History:
        # A dot plus a single letter is the US share-class notation we accept as an alias.
        provider_symbol = (
            symbol.replace(".", "-") if len(symbol.rsplit(".", 1)[-1]) == 1 else symbol
        )
        ticker = yf.Ticker(provider_symbol)
        try:
            table = ticker.history(
                start=start.isoformat(),
                end=through.isoformat(),
                **DOWNLOAD_OPTIONS,
            )
            meta = ticker.get_history_metadata()
        except YFRateLimitError:
            raise ExperimentError(
                "market_throttled",
                "Yahoo is throttling requests. Try later.",
                429,
            ) from None
        except (YFException, OSError, ValueError):
            raise ExperimentError(
                "market_unavailable",
                f"No usable Yahoo history for {symbol}. Check the ticker or try later.",
                502,
            ) from None
        if (
            meta.get("currency") != "USD"
            or meta.get("exchangeTimezoneName") != "America/New_York"
            or meta.get("instrumentType") not in ("EQUITY", "ETF")
            or "." in provider_symbol
        ):
            raise ExperimentError(
                "unsupported_instrument", f"{symbol} is not a supported US equity or ETF."
            )
        return decode_history(table, symbol, start=start, end=end)


class DemoMarket:
    """Seeded synthetic prices with no network calls or real market observations."""

    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions

    def fetch(self, symbols: tuple[str, ...], start: date, end: date) -> dict[str, History]:
        """Generate stable overlapping histories for every requested symbol."""
        first = self.sessions.on_or_after(min(start, date(2000, 1, 1)))
        last = self.sessions.on_or_after(end + timedelta(days=1))
        result: dict[str, History] = {}
        for symbol in symbols:
            rng = Random(int(sha256(symbol.encode()).hexdigest()[:16], 16))
            common = Random(87)
            price = 90.0 + rng.random() * 150
            bars = {}
            for day in self.sessions.dates[first:last]:
                market_move = common.gauss(0.00015, 0.008)
                price *= 1 + market_move + rng.gauss(0.00008, 0.009)
                if day >= start:
                    c = Decimal(str(round(price, 2)))
                    swing = c * Decimal("0.004")
                    bars[day] = Bar(c + swing, c - swing, c)
            result[symbol] = History(bars, {})
        return result
