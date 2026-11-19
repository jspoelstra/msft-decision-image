"""Verify session cutoffs, split neutrality, and image isolation."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

import pandas as pd
import pytest
from PIL import Image

from chart_lens.chart import render
from chart_lens.domain import (
    Bar,
    ExperimentError,
    History,
    Sessions,
    chart_input,
    window_drawdowns,
)
from chart_lens.market import DemoMarket, decode_history
from chart_lens.report import Row, join_truth


@pytest.mark.parametrize(
    ("target_path", "comparison_path", "label"),
    [
        (("100", "90", "80"), ("100", "95", "70"), "supported"),
        (("100", "150", "90", "160"), ("100", "110", "100", "105"), "contradicted"),
        (("100", "80", "200", "190"), ("100", "120", "84", "100"), "supported"),
        (("100", "110", "120"), ("100", "120", "130"), "contradicted"),
        (("100",), ("100",), "contradicted"),
        (("100", "80", "100"), ("200", "160", "220"), "contradicted"),
        (("100", "90.0000000001"), ("100", "90"), "supported"),
    ],
)
def test_given_price_paths_when_joining_truth_then_chronological_drawdown_decides(
    target_path, comparison_path, label
):
    # Arrange
    sessions = Sessions()
    index = sessions.on_or_after(date(2025, 1, 6))
    days = sessions.dates[index - 29 : index + 1]
    histories = {}
    for symbol, path in (("MSFT", target_path), ("AAPL", comparison_path)):
        values = tuple(map(Decimal, path + (path[-1],) * (30 - len(path))))
        histories[symbol] = History(
            {day: Bar(value, value, value) for day, value in zip(days, values, strict=True)}, {}
        )
    histories["MSFT"].bars[sessions.dates[index - 30]] = Bar(
        Decimal(10000), Decimal(10000), Decimal(10000)
    )
    row = Row(origin=days[-1].isoformat(), choice="supported", status="scored")

    # Act
    join_truth(row, histories, "MSFT", "AAPL", sessions, index)

    # Assert
    assert row.actual == label
    assert row.target is None and row.status == "evaluated"
    assert "historical_majority" not in row.baselines
    assert row.target_return is row.spy_return is row.outperformance_margin is None


def test_given_early_close_when_hour_elapses_then_session_becomes_eligible():
    # Arrange
    sessions = Sessions()

    # Act / Assert
    assert sessions.dates[sessions.latest(datetime(2025, 11, 28, 18, 59, tzinfo=UTC))] == date(
        2025, 11, 26
    )
    assert sessions.dates[sessions.latest(datetime(2025, 11, 28, 19, 0, tzinfo=UTC))] == date(
        2025, 11, 28
    )


def test_given_retro_adjusted_prices_when_normalized_then_no_double_split_or_dividend_adjustment():
    # Arrange
    first, target, later = date(2024, 6, 7), date(2024, 6, 10), date(2024, 6, 11)
    data = pd.DataFrame(
        {
            "High": [51, 52, 53],
            "Low": [49, 50, 51],
            "Close": [50, 51, 52],
            "Adj Close": [12, 12, 12],
            "Stock Splits": [0, 4, 2],
        },
        index=pd.to_datetime([first, target, later]),
    )

    # Act
    result = decode_history(data, "TEST", start=first, end=target)

    # Assert
    assert result.bars[first].close == 400
    assert result.bars[target].close == 102
    assert result.on_basis(first, target).close == 100
    assert result.change(first, target) == Decimal("0.02")
    assert later not in result.bars and later not in result.splits


def test_given_future_changes_when_rendering_then_chart_bytes_do_not_change():
    # Arrange
    sessions = Sessions()
    index = sessions.on_or_after(date(2025, 1, 6))
    origin, target = sessions.dates[index : index + 2]
    data = DemoMarket(sessions).fetch(("MSFT", "AAPL"), origin - timedelta(days=370), target)
    known_high = Decimal("9999")
    earlier = sessions.dates[index - 80]
    bar = data["MSFT"].bars[earlier]
    data["MSFT"].bars[earlier] = Bar(known_high, bar.low, bar.close)

    # Act
    inputs = chart_input(data, "MSFT", "AAPL", sessions, index)
    truth = window_drawdowns(data, "MSFT", "AAPL", sessions, index)
    png = render(inputs)
    data["MSFT"].bars[target] = Bar(known_high * 2, Decimal(1), known_high)
    data["MSFT"].splits[target] = Decimal(4)
    alternate = render(chart_input(data, "MSFT", "AAPL", sessions, index))

    # Assert
    assert inputs.high == float(known_high)
    assert len(inputs.close) == 30 and set(inputs.relative) == {"Target", "Comparison"}
    assert png == alternate
    assert truth == window_drawdowns(data, "MSFT", "AAPL", sessions, index)
    with Image.open(BytesIO(png)) as image:
        assert max(image.size) <= 1568
        assert image.size == (1568, 980)
        assert image.info == {"dpi": pytest.approx((98, 98), abs=0.1)}
    del data["AAPL"].bars[origin]
    with pytest.raises(ExperimentError, match="Missing required session"):
        chart_input(data, "MSFT", "AAPL", sessions, index)


def test_given_window_split_when_comparing_drawdowns_then_no_false_split_loss_is_counted():
    # Arrange
    sessions = Sessions()
    index = sessions.on_or_after(date(2025, 1, 6))
    days = sessions.dates[index - 29 : index + 1]
    histories = {
        "MSFT": History(
            {day: Bar(Decimal(100), Decimal(100), Decimal(100)) for day in days},
            {days[-1]: Decimal(2)},
        ),
        "AAPL": History({day: Bar(Decimal(100), Decimal(100), Decimal(100)) for day in days}, {}),
    }
    histories["MSFT"].bars[days[-1]] = Bar(Decimal(55), Decimal(55), Decimal(55))

    # Act
    target, comparison = window_drawdowns(histories, "MSFT", "AAPL", sessions, index)

    # Assert
    assert (target, comparison) == (Decimal(0), Decimal(0))


def test_given_current_window_change_when_baselining_then_only_earlier_windows_are_used():
    # Arrange
    sessions = Sessions()
    index = sessions.on_or_after(date(2025, 1, 6))
    days = sessions.dates[index - 281 : index + 1]
    histories = {
        symbol: History({day: Bar(Decimal(100), Decimal(100), Decimal(100)) for day in days}, {})
        for symbol in ("MSFT", "AAPL")
    }
    before = Row(origin=days[-1].isoformat(), choice="supported")
    after = before.model_copy(deep=True)

    # Act
    join_truth(before, histories, "MSFT", "AAPL", sessions, index)
    histories["AAPL"].bars[days[-1]] = Bar(Decimal(50), Decimal(50), Decimal(50))
    join_truth(after, histories, "MSFT", "AAPL", sessions, index)

    # Assert
    assert before.actual == "contradicted" and after.actual == "supported"
    assert before.baselines == after.baselines
    baseline = after.baselines["historical_majority"]
    assert baseline.choice == "contradicted"
    assert baseline.probabilities == {
        "supported": 1 / 254,
        "contradicted": 253 / 254,
        "insufficient": 0,
    }


def test_given_multiple_peaks_when_evaluating_then_loss_is_relative_to_prior_peak_not_start():
    # Arrange
    sessions = Sessions()
    index = sessions.on_or_after(date(2025, 1, 6))
    days = sessions.dates[index - 29 : index + 1]
    values = tuple(map(Decimal, ("100", "200", "160", "300", "150") + ("300",) * 25))
    history = History(
        {day: Bar(value, value, value) for day, value in zip(days, values, strict=True)}, {}
    )

    # Act
    target, comparison = window_drawdowns({"MSFT": history}, "MSFT", "MSFT", sessions, index)

    # Assert
    assert target == comparison == Decimal("0.5")
