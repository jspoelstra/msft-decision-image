"""Check answered accuracy, abstentions, and probability losses by hand."""

from math import log

import pytest

from chart_lens.report import Baseline, Row, summarize


def test_given_answers_abstention_and_failure_when_reporting_then_denominators_are_explicit():
    # Arrange
    first = Row(
        origin="2025-01-02",
        status="evaluated",
        eligible=True,
        choice="supported",
        actual="supported",
        normalized={"supported": 0.75, "contradicted": 0.2, "insufficient": 0.05},
        baselines={"always_contradicted": Baseline(choice="contradicted")},
    )
    second = first.model_copy(
        update={
            "origin": "2025-01-03",
            "actual": "contradicted",
            "baselines": {},
        }
    )
    abstention = first.model_copy(
        update={
            "origin": "2025-01-06",
            "choice": "insufficient",
            "baselines": {},
            "normalized": {"supported": 0.1, "contradicted": 0.2, "insufficient": 0.7},
        }
    )
    failure = Row(origin="2025-01-07", status="failed", eligible=True)

    # Act
    report = summarize([first, second, abstention, failure])

    # Assert
    assert report["accuracy"] == report["balanced_accuracy"] == 0.5
    assert report["overall_accuracy"] == pytest.approx(1 / 3)
    assert report["coverage"] == pytest.approx(2 / 3)
    assert report["evaluation_coverage"] == 0.75
    assert report["effective_coverage"] == 0.5
    assert report["macro_f1"] == pytest.approx(1 / 3)
    assert report["log_loss"] == pytest.approx(-(log(0.75) + log(0.2) + log(0.1)) / 3)
    assert report["brier"] == pytest.approx((0.105 + 1.205 + 1.34) / 3)
    assert report["confusion"] == {
        "supported": {"supported": 1, "contradicted": 0, "insufficient": 1},
        "contradicted": {"supported": 1, "contradicted": 0, "insufficient": 0},
    }
    assert report["baselines"]["always_contradicted"]["delta"] == 1
    assert report["baselines"]["always_contradicted"]["count"] == 1
    assert report["counts"]["answered"] == 2 and report["counts"]["abstained"] == 1
    assert sum(item["count"] for item in report["calibration"]) == 3


def test_given_only_abstentions_when_reporting_then_accuracy_is_unavailable_not_perfect():
    # Arrange
    row = Row(
        origin="2025-01-02",
        status="evaluated",
        choice="insufficient",
        actual="supported",
        normalized={"supported": 0.1, "contradicted": 0.2, "insufficient": 0.7},
    )

    # Act
    report = summarize([row])

    # Assert
    assert report["coverage"] == report["overall_accuracy"] == 0
    assert report["accuracy"] is None and report["balanced_accuracy"] is None
    assert report["log_loss"] == pytest.approx(-log(0.1))
    assert report["baselines"]["always_supported"]["count"] == 0


def test_given_no_scored_rows_when_reporting_then_metrics_are_unavailable():
    # Arrange
    row = Row(origin="2025-01-02", status="skipped")

    # Act
    report = summarize([row])

    # Assert
    assert report["coverage"] is None and report["evaluation_coverage"] == 0
    assert all(
        report[key] is None for key in ("accuracy", "balanced_accuracy", "macro_f1", "brier")
    )


def test_given_exact_margin_boundaries_when_reporting_then_slices_use_percentage_points():
    # Arrange
    rows = [
        Row(
            origin=f"2025-01-0{i + 2}",
            status="evaluated",
            actual="supported",
            choice="supported",
            drawdown_margin=margin,
        )
        for i, margin in enumerate((0, 0.001, -0.005, 0.02))
    ]

    # Act
    report = summarize(rows)

    # Assert
    assert [item["count"] for item in report["margin_slices"].values()] == [1, 1, 1, 1]
    assert all(item["accuracy"] == 1 for item in report["margin_slices"].values())
