"""Verify Yahoo adapter configuration and explicit failure modes."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from yfinance.exceptions import YFRateLimitError

from chart_lens.domain import ExperimentError
from chart_lens.market import YahooMarket


def test_given_us_share_class_when_fetching_then_preserves_missing_sessions(mocker):
    # Arrange
    yahoo = mocker.patch("chart_lens.market.yf.Ticker")
    yahoo.return_value.get_history_metadata.return_value = {
        "currency": "USD",
        "exchangeTimezoneName": "America/New_York",
        "instrumentType": "EQUITY",
    }
    yahoo.return_value.history.return_value = pd.DataFrame(
        {"High": [110, None], "Low": [90, None], "Close": [100, None], "Stock Splits": [0, 0]},
        index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
    )
    start, end = date(2025, 1, 2), date(2025, 1, 3)

    # Act
    history = YahooMarket().fetch(("BRK.B",), start, end)["BRK.B"]

    # Assert
    yahoo.assert_called_once_with("BRK-B")
    options = yahoo.return_value.history.call_args.kwargs
    assert options["start"] == start.isoformat()
    assert date.fromisoformat(options["end"]) > max(end, datetime.now(UTC).date())
    assert options["interval"] == "1d" and options["actions"]
    assert not options["auto_adjust"] and not options["prepost"]
    assert options["keepna"] and options["raise_errors"]
    assert set(history.bars) == {start}


def test_given_rate_limit_when_fetching_then_no_remaining_downloads_are_started(mocker):
    # Arrange
    yahoo = mocker.patch("chart_lens.market.yf.Ticker")
    yahoo.return_value.history.side_effect = YFRateLimitError()
    day = date(2025, 1, 2)

    # Act / Assert
    with pytest.raises(ExperimentError, match="throttling") as caught:
        YahooMarket().fetch(("DIA", "QQQ", "SPY"), day, day)
    assert caught.value.status == 429
    yahoo.assert_called_once_with("DIA")
