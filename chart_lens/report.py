"""Evaluator-only claim truth and abstention-aware matched-sample reporting."""

from collections import Counter, defaultdict
from math import ceil, log
from statistics import mean, median
from typing import Literal

from pydantic import BaseModel, Field

from chart_lens.domain import ExperimentError, History, Scheme, Sessions, window_drawdowns

TRUTH = ("supported", "contradicted")
CHOICES = (*TRUTH, "insufficient")


class Baseline(BaseModel):
    """A baseline choice and optional probability distribution."""

    choice: str
    probabilities: dict[str, float] | None = None


class Row(BaseModel):
    """A durable judgment with separately attached numerical ground truth."""

    origin: str
    target: str | None = None
    window_start: str | None = None
    status: Literal["pending", "scored", "evaluated", "failed", "skipped"] = "pending"
    eligible: bool = False
    image_sha: str | None = None
    choice: str | None = None
    probabilities: dict[str, float] | None = None
    normalized: dict[str, float] | None = None
    actual: str | None = None
    target_return: float | None = None
    spy_return: float | None = None
    outperformance_margin: float | None = None
    target_drawdown: float | None = None
    comparison_drawdown: float | None = None
    drawdown_margin: float | None = None
    realized_return: float | None = None
    error: str | None = None
    model: str | None = None
    latency_ms: float | None = None
    attempts: int = 0
    cache_hit: bool = False
    cache_key: str | None = None
    source_inference_at: str | None = None
    request: dict | None = None
    response: dict | None = None
    baselines: dict[str, Baseline] = Field(default_factory=dict)


def join_truth(
    row: Row, histories: dict[str, History], ticker: str, comparison: str,
    sessions: Sessions, index: int
) -> None:
    """Attach same-window truth after saving the image-only model judgment."""
    target, other = window_drawdowns(histories, ticker, comparison, sessions, index)
    row.window_start = sessions.dates[index - 29].isoformat()
    row.target_drawdown, row.comparison_drawdown = float(target), float(other)
    row.drawdown_margin = float(other - target)
    row.actual = "supported" if target < other else "contradicted"
    row.status = "evaluated"
    row.baselines = {f"always_{label}": Baseline(choice=label) for label in TRUTH}
    recent: Counter = Counter()
    for pos in range(index - 252, index):
        try:
            previous_target, previous_other = window_drawdowns(
                histories, ticker, comparison, sessions, pos
            )
        except ExperimentError:
            return
        recent["supported" if previous_target < previous_other else "contradicted"] += 1
    probabilities = {label: (recent[label] + 1) / 254 for label in TRUTH}
    probabilities["insufficient"] = 0
    row.baselines["historical_majority"] = Baseline(
        choice="supported" if recent["supported"] > recent["contradicted"] else "contradicted",
        probabilities=probabilities,
    )


def _probability_losses(rows: list[Row]) -> tuple[float | None, float | None]:
    if not rows:
        return None, None
    losses, briers = [], []
    for row in rows:
        if row.normalized is None or row.actual is None:
            continue
        clipped = {key: max(value, 1e-15) for key, value in row.normalized.items()}
        losses.append(-log(clipped[row.actual] / sum(clipped.values())))
        briers.append(sum((p - (key == row.actual)) ** 2 for key, p in row.normalized.items()))
    return (mean(losses), mean(briers)) if losses else (None, None)


def _slice(rows: list[Row]) -> dict:
    answered = [row for row in rows if row.choice in TRUTH]
    return {
        "count": len(rows),
        "answered": len(answered),
        "abstained": len(rows) - len(answered),
        "coverage": len(answered) / len(rows) if rows else None,
        "accuracy": mean(row.choice == row.actual for row in answered) if answered else None,
    }


def _baselines(answered: list[Row], evaluated: list[Row]) -> dict:
    output = {}
    for key in ("always_supported", "always_contradicted", "historical_majority"):
        matched = [row for row in answered if key in row.baselines]
        accuracy = (
            mean(row.baselines[key].choice == row.actual for row in matched) if matched else None
        )
        model_accuracy = mean(row.choice == row.actual for row in matched) if matched else None
        probabilistic = [
            row.model_copy(update={"normalized": row.baselines[key].probabilities})
            for row in matched
            if row.baselines[key].probabilities is not None
        ]
        log_loss, brier = _probability_losses(probabilistic)
        model_log_loss, model_brier = (
            _probability_losses(matched) if key == "historical_majority" else (None, None)
        )
        output[key] = {
            "accuracy": accuracy,
            "count": len(matched),
            "delta": model_accuracy - accuracy if accuracy is not None else None,
            "log_loss": log_loss,
            "brier": brier,
            "matched_model_accuracy": model_accuracy,
            "matched_model_log_loss": model_log_loss,
            "matched_model_brier": model_brier,
        }
    uniform = [
        row.model_copy(update={"normalized": dict.fromkeys(CHOICES, 1 / 3)}) for row in evaluated
    ]
    log_loss, brier = _probability_losses(uniform)
    output["uniform"] = {
        "accuracy": None,
        "count": len(evaluated),
        "delta": None,
        "log_loss": log_loss,
        "brier": brier,
    }
    return output


def summarize(rows: list[Row], scheme: Scheme = "claim") -> dict:
    """Separate answered accuracy, abstention coverage, and all-judgment losses."""
    evaluated = [row for row in rows if row.status == "evaluated"]
    answered = [row for row in evaluated if row.choice in TRUTH]
    matrix = {key: dict.fromkeys(CHOICES, 0) for key in TRUTH}
    for row in evaluated:
        if row.actual in matrix and row.choice in CHOICES:
            matrix[row.actual][row.choice] += 1
    classes = {}
    for label in TRUTH:
        support = sum(row.actual == label for row in answered)
        predicted = sum(row.choice == label for row in answered)
        tp = sum(row.actual == label and row.choice == label for row in answered)
        classes[label] = {
            "support": support,
            "predicted": predicted,
            "precision": tp / predicted if predicted else None,
            "recall": tp / support if support else None,
            "f1": 2 * tp / (support + predicted) if support else None,
        }
    recalls = [item["recall"] for item in classes.values() if item["recall"] is not None]
    f1s = [item["f1"] for item in classes.values() if item["f1"] is not None]
    log_loss, brier = _probability_losses(evaluated)
    calibration: list[list[tuple[float, bool]]] = [[] for _ in range(10)]
    months: dict[str, list[Row]] = defaultdict(list)
    margins: dict[str, list[Row]] = {
        "<0.1 pp": [],
        "0.1-<0.5 pp": [],
        "0.5-<2 pp": [],
        ">=2 pp": [],
    }
    for row in evaluated:
        months[row.origin[:7]].append(row)
        if row.normalized and row.choice:
            confidence = row.normalized[row.choice]
            calibration[min(9, int(confidence * 10))].append((confidence, row.choice == row.actual))
        if row.drawdown_margin is not None:
            margin = abs(row.drawdown_margin)
            key = (
                "<0.1 pp"
                if margin < 0.001
                else "0.1-<0.5 pp"
                if margin < 0.005
                else "0.5-<2 pp"
                if margin < 0.02
                else ">=2 pp"
            )
            margins[key].append(row)
    latencies = sorted(
        row.latency_ms for row in rows if row.latency_ms is not None and not row.cache_hit
    )
    return {
        "counts": {
            "requested": len(rows),
            "eligible": sum(row.eligible for row in rows),
            "scored": sum(row.choice is not None for row in rows),
            "evaluated": len(evaluated),
            "answered": len(answered),
            "abstained": sum(row.choice == "insufficient" for row in evaluated),
            "failed": sum(row.status == "failed" for row in rows),
            "skipped": sum(row.status == "skipped" for row in rows),
            "pending": sum(row.status == "pending" for row in rows),
        },
        "coverage": len(answered) / len(evaluated) if evaluated else None,
        "evaluation_coverage": len(evaluated) / len(rows) if rows else 0,
        "effective_coverage": len(answered) / len(rows) if rows else 0,
        "accuracy": mean(row.choice == row.actual for row in answered) if answered else None,
        "overall_accuracy": mean(row.choice == row.actual for row in evaluated)
        if evaluated
        else None,
        "balanced_accuracy": mean(recalls) if len(recalls) == len(TRUTH) else None,
        "macro_f1": mean(f1s) if f1s else None,
        "macro_f1_classes": [key for key, values in classes.items() if values["support"]],
        "log_loss": log_loss,
        "brier": brier,
        "confusion": matrix,
        "classes": classes,
        "calibration": [
            {
                "range": f"{i * 10}-{(i + 1) * 10}%",
                "count": len(items),
                "mean_confidence": mean(x[0] for x in items) if items else None,
                "accuracy": mean(x[1] for x in items) if items else None,
            }
            for i, items in enumerate(calibration)
        ],
        "baselines": _baselines(answered, evaluated),
        "latency_ms": {
            "median": median(latencies) if latencies else None,
            "p95": latencies[ceil(len(latencies) * 0.95) - 1] if latencies else None,
        },
        "attempts": sum(row.attempts for row in rows),
        "cache_hits": sum(row.cache_hit for row in rows),
        "usage": dict(sum_usage(rows)),
        "months": {month: _slice(items) for month, items in months.items()},
        "margin_slices": {key: _slice(items) for key, items in margins.items()},
        "exploratory": len(evaluated) < 30,
    }


def sum_usage(rows: list[Row]) -> Counter:
    """Aggregate metered usage from actual, noncached successful requests."""
    totals: Counter = Counter()
    for row in rows:
        if row.response and not row.cache_hit:
            totals.update(row.response.get("usage", {}))
    return totals
