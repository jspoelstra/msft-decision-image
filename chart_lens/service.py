"""Job lifecycle, frozen inputs, and judgment-before-truth orchestration."""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from importlib.metadata import version
from threading import Lock
from uuid import uuid4

from chart_lens.chart import TEMPLATE, render
from chart_lens.decision import DecisionError, Scorer, payload
from chart_lens.domain import (
    CLAIM,
    EXPERIMENT,
    ExperimentError,
    History,
    Scheme,
    Sessions,
    chart_input,
)
from chart_lens.market import DOWNLOAD_OPTIONS, Market
from chart_lens.report import Row, join_truth, summarize
from chart_lens.store import Store, canonical, digest, pack, unpack

logger = logging.getLogger(__name__)
ACTIVE: tuple[str, ...] = ("queued", "running")
FIRST_ORIGIN: date = date(1991, 1, 1)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Service:
    """Run one local job at a time and preserve every completed origin."""

    def __init__(
        self,
        store: Store,
        sessions: Sessions,
        market: Market,
        scorer: Scorer | None,
        *,
        mode: str,
        deployment: str,
        model_revision: str,
        endpoint: str,
    ) -> None:
        self.store, self.sessions, self.market, self.scorer = store, sessions, market, scorer
        self.mode, self.deployment, self.model_revision = mode, deployment, model_revision
        self.identity = digest([mode, endpoint, deployment, model_revision])
        self._jobs = Lock()
        self._market = Lock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chart-lens")
        for run in store.runs():
            if run["status"] in ACTIVE:
                store.update(run["id"], status="interrupted", stage="Interrupted by restart")

    def close(self) -> None:
        """Stop after the in-flight row, then release the worker."""
        for run in self.store.runs():
            if run["status"] in ACTIVE:
                self.store.update(run["id"], interrupt_requested=True)
        self._worker.shutdown()

    def require_ready(self) -> None:
        """Reject live actions until an explicit, ready scorer is configured."""
        if self.scorer is None:
            raise ExperimentError(
                "not_configured",
                "Enable synthetic mode or configure live Yahoo and Foundry access.",
                503,
            )

    def interval(self, start: date, count: int) -> range:
        """Resolve chart-ending sessions without requiring a future outcome."""
        if start < FIRST_ORIGIN:
            raise ExperimentError("invalid_date", "Start date must be on or after 1991-01-01.")
        first = self.sessions.on_or_after(start)
        if first + count - 1 > self.sessions.latest():
            raise ExperimentError("future_origin", "The final chart-ending session is not yet available.")
        return range(first, first + count)

    def _fingerprint(self, scheme: Scheme, ticker: str, comparison: str) -> str:
        return digest(
            [
                EXPERIMENT,
                ticker,
                comparison,
                self.identity,
                TEMPLATE,
                version("matplotlib"),
                version("exchange-calendars"),
                self._market_version(),
                payload(self.deployment, scheme),
            ]
        )

    def _market_version(self) -> str:
        return "synthetic-market-v1" if self.mode == "demo" else version("yfinance")

    def _load(
        self,
        ticker: str,
        comparison: str,
        first: int,
        last: int,
    ) -> tuple[str, str, dict[str, History]]:
        days = self.sessions.dates
        start = min(days[first] - timedelta(weeks=52), days[first - 281])
        end = days[last]
        symbols = tuple(sorted({ticker, comparison}))
        version_key = digest([self._market_version(), DOWNLOAD_OPTIONS])
        symbol_key = canonical(symbols)
        ttl = timedelta(minutes=10) if end == days[self.sessions.latest()] else timedelta(hours=12)
        with self._market:
            found = self.store.lookup_snapshot(
                self.mode,
                symbol_key,
                start.isoformat(),
                end.isoformat(),
                version_key,
                (datetime.now(UTC) - ttl).isoformat(),
            )
            if found:
                identity, retrieved, data = found
                return identity, retrieved, unpack(data)
            histories = self.market.fetch(symbols, start, end)
            allowed = set(days[self.sessions.on_or_after(start) : last + 1])
            if any(
                not (history.bars.keys() <= allowed and history.splits.keys() <= allowed)
                for history in histories.values()
            ):
                raise ExperimentError(
                    "invalid_history", "Provider returned non-session price or split dates.", 502
                )
            identity, retrieved, _ = self.store.insert_snapshot(
                self.mode,
                symbol_key,
                start.isoformat(),
                end.isoformat(),
                version_key,
                _now(),
                pack(histories),
            )
        return identity, retrieved, histories

    def preview(
        self, ticker: str, comparison: str, scheme: Scheme, start: date, count: int
    ) -> dict:
        """Inspect data coverage and the upper bound of live inference calls."""
        self.require_ready()
        indices = self.interval(start, count)
        days = self.sessions.dates
        _, _, histories = self._load(ticker, comparison, indices.start, indices.stop - 1)
        eligible = cached = 0
        fingerprint = self._fingerprint(scheme, ticker, comparison)
        for index in indices:
            try:
                data = chart_input(histories, ticker, comparison, self.sessions, index)
            except ExperimentError:
                continue
            eligible += 1
            key = digest([fingerprint, asdict(data)])
            cached += self.store.cached(key) is not None
        return {
            "ticker": ticker,
            "comparison": comparison,
            "requested_start": start.isoformat(),
            "effective_start": days[indices.start].isoformat(),
            "last_origin": days[indices.stop - 1].isoformat(),
            "count": count,
            "max_attempts": 0 if self.mode == "demo" else 3 * count,
            "eligible": eligible,
            "cached": cached,
        }

    def _assert_idle(self) -> None:
        if any(run["status"] in ACTIVE for run in self.store.runs()):
            raise ExperimentError(
                "busy", "Another run is active. Wait for it to finish or cancel it.", 409
            )

    def start(
        self,
        ticker: str,
        comparison: str,
        scheme: Scheme,
        *,
        start: date | None = None,
        count: int = 1,
    ) -> dict:
        """Persist a new run before handing work to the job worker."""
        self.require_ready()
        latest = self.sessions.latest()
        indices = self.interval(start, count) if start else range(latest, latest + 1)
        days = self.sessions.dates
        rows = [
            Row(origin=days[i].isoformat(), window_start=days[i - 29].isoformat())
            for i in indices
        ]
        run = {
            "id": uuid4().hex,
            "kind": "backtest" if start else "prediction",
            "ticker": ticker,
            "comparison": comparison,
            "scheme": scheme,
            "experiment": EXPERIMENT,
            "claim": CLAIM,
            "truth_definition": "Target maximum closing-price drawdown < Comparison's; ties contradict",
            "margin_definition": "Comparison maximum drawdown minus Target maximum drawdown",
            "baseline_policy": "252 earlier 30-session windows; one Laplace count per truth class",
            "metrics_policy": "Accuracy on answered rows; losses on all evaluated judgments",
            "mode": self.mode,
            "status": "queued",
            "stage": "Queued",
            "count": len(rows),
            "completed": 0,
            "requested_start": start.isoformat() if start else None,
            "effective_start": rows[0].origin,
            "last_origin": rows[-1].origin,
            "error": None,
            "warning": None,
            "report": None,
            "created_at": _now(),
            "fingerprint": self._fingerprint(scheme, ticker, comparison),
            "snapshot_id": None,
            "chart_template": TEMPLATE,
            "calendar": f"XNYS/{version('exchange-calendars')}",
            "renderer": f"matplotlib/{version('matplotlib')}",
            "market_version": self._market_version(),
            "market_options": DOWNLOAD_OPTIONS if self.mode == "live" else {},
            "price_policy": "Split-adjusted closing-price peak-to-later-trough loss within the 30-session window",
            "acceptance_policy": "Regular session close + 60 minutes; complete daily bars",
            "rubric": payload(self.deployment, scheme)["questions"],
            "prompt_hash": digest(payload(self.deployment, scheme)["questions"]),
            "model_revision": self.model_revision or None,
            "deployment": self.deployment,
            "evaluation_type": (
                "synthetic"
                if self.mode == "demo"
                else "historical chart claim verification"
                if start
                else "latest-session chart claim verification"
            ),
        }
        with self._jobs:
            self._assert_idle()
            if len(self.store.runs()) >= 24:
                raise ExperimentError(
                    "retention_limit", "Delete a saved run before starting another.", 409
                )
            self.store.create(run, rows)
            self._worker.submit(self._execute, run["id"])
        return run

    def cancel(self, run_id: str) -> dict:
        """Request cancellation after the in-flight origin."""
        with self._jobs:
            run = self.store.get(run_id)
            if run["kind"] != "backtest" or run["status"] not in ACTIVE:
                raise ExperimentError(
                    "not_running", "Only an active backtest can be cancelled.", 409
                )
            return self.store.update(run_id, cancel_requested=True, stage="Cancellation requested")

    def resume(self, run_id: str) -> dict:
        """Resume unfinished origins without changing the saved experiment."""
        self.require_ready()
        with self._jobs:
            self._assert_idle()
            run = self.store.get(run_id)
            if run.get("experiment") != EXPERIMENT:
                raise ExperimentError(
                    "legacy_experiment",
                    "Legacy experiments are read-only. Start a new drawdown comparison.",
                    409,
                )
            if run["kind"] != "backtest" or run["status"] not in ("cancelled", "interrupted"):
                raise ExperimentError(
                    "not_resumable", "This run has no resumable backtest work.", 409
                )
            if run["fingerprint"] != self._fingerprint(
                run["scheme"], run["ticker"], run["comparison"]
            ):
                raise ExperimentError(
                    "experiment_changed", "Restore the original experiment configuration.", 409
                )
            updated = self.store.update(
                run_id,
                status="queued",
                stage="Queued",
                cancel_requested=False,
                interrupt_requested=False,
                error=None,
            )
            self._worker.submit(self._execute, run_id)
        return updated

    def delete(self, run_id: str) -> None:
        """Delete a finished run without racing the worker."""
        with self._jobs:
            if self.store.get(run_id)["status"] in ACTIVE:
                raise ExperimentError("busy", "Wait for the run to stop before deleting it.", 409)
            self.store.delete(run_id)

    def _evaluate_row(
        self,
        run: dict,
        row: Row,
        index: int,
        histories: dict[str, History],
    ) -> None:
        scorer = self.scorer
        if scorer is None:
            self.require_ready()
            return
        run_id, ticker, scheme = run["id"], run["ticker"], run["scheme"]
        if row.status == "pending":
            try:
                data = chart_input(histories, ticker, run["comparison"], self.sessions, index)
            except ExperimentError as exc:
                row.status, row.error = "skipped", str(exc)
                self.store.save(run_id, row)
                return
            self.store.update(run_id, stage="Rendering chart")
            row.eligible = True
            png = render(data)
            row.image_sha = sha256(png).hexdigest()
            row.cache_key = digest([run["fingerprint"], asdict(data)])
            row.request = payload(self.deployment, scheme)
            cached = self.store.cached(row.cache_key)
            self.store.save(run_id, row, png)
            self.store.update(run_id, stage="Scoring image")
            if cached and cached.image_sha == row.image_sha:
                row.choice, row.probabilities = cached.choice, cached.probabilities
                row.normalized, row.model = cached.normalized, cached.model
                row.response, row.latency_ms = cached.response, cached.latency_ms
                row.cache_hit, row.source_inference_at = True, cached.source_inference_at
            else:
                try:
                    decision = scorer.score(png, scheme, 3 if run["kind"] == "backtest" else 1)
                except DecisionError as exc:
                    row.status, row.error, row.attempts = "failed", str(exc), exc.attempts
                    self.store.save(run_id, row)
                    return
                row.choice, row.probabilities = decision.choice, decision.probabilities
                row.normalized, row.model = decision.normalized, decision.model
                row.latency_ms, row.attempts = decision.latency_ms, decision.attempts
                row.source_inference_at = _now()
                row.response = {
                    "model": decision.model,
                    "usage": decision.usage,
                    "answers": {
                        "claim": {
                            "type": "choice",
                            "choice": row.choice,
                            "probabilities": row.probabilities,
                        },
                    },
                }
            row.status = "scored"
            self.store.save(run_id, row)
        self.store.update(run_id, stage="Checking same-window claim truth")
        join_truth(row, histories, ticker, run["comparison"], self.sessions, index)
        self.store.save(run_id, row)

    def _execute(self, run_id: str) -> None:
        try:
            run = self.store.update(
                run_id, status="running", stage="Loading immutable market history"
            )
            index = self.sessions.on_or_after(date.fromisoformat(run["effective_start"]))
            prediction = run["kind"] == "prediction"
            if run.get("snapshot_id"):
                snap, retrieved, data = self.store.snapshot(run["snapshot_id"])
                histories = unpack(data)
            else:
                snap, retrieved, histories = self._load(
                    run["ticker"],
                    run["comparison"],
                    index,
                    index + run["count"] - 1,
                )
                self.store.update(
                    run_id,
                    snapshot_id=snap,
                    data_retrieved_at=retrieved,
                    data_hash=digest(pack(histories)),
                )
            if prediction:
                index = self._resolve_latest(run, histories, index)
            for offset, row in enumerate(self.store.rows(run_id)):
                state = self.store.get(run_id)
                if state.get("cancel_requested") or state.get("interrupt_requested"):
                    outcome = "interrupted" if state.get("interrupt_requested") else "cancelled"
                    self.store.update(run_id, status=outcome, stage=outcome.title())
                    return
                if row.status == "pending" or (
                    row.status == "scored" and not row.error
                ):
                    self._evaluate_row(run, row, index + offset, histories)
                rows = self.store.rows(run_id)
                self.store.update(
                    run_id,
                    completed=sum(r.status != "pending" for r in rows),
                    report=summarize(rows, run["scheme"]),
                )
            rows = self.store.rows(run_id)
            failed = not any(row.choice for row in rows)
            self.store.update(
                run_id,
                status="failed" if failed else "complete",
                stage="Finished",
                error=(rows[0].error if len(rows) == 1 else "No origins were scored.")
                if failed
                else None,
                finished_at=_now(),
            )
        except ExperimentError as exc:
            self.store.update(run_id, status="failed", stage="Stopped", error=str(exc))
        except Exception as exc:
            logger.error("Run %s failed unexpectedly (%s)", run_id, type(exc).__name__)
            self.store.update(
                run_id,
                status="failed",
                stage="Stopped",
                error="Unexpected local error. Check the server log and try again.",
            )

    def _resolve_latest(self, run: dict, histories: dict[str, History], index: int) -> int:
        days = self.sessions.dates
        ticker = run["ticker"]
        comparison = run["comparison"]
        if all(days[index] in histories[symbol].bars for symbol in (ticker, comparison)):
            return index
        for previous in range(index - 1, index - 6, -1):
            try:
                chart_input(histories, ticker, comparison, self.sessions, previous)
            except ExperimentError:
                continue
            row = Row(
                origin=days[previous].isoformat(), window_start=days[previous - 29].isoformat()
            )
            self.store.replace_prediction_origin(run["id"], row)
            self.store.update(
                run["id"],
                effective_start=row.origin,
                last_origin=row.origin,
                warning="Latest accepted market data is older; the claim uses the displayed window.",
            )
            return previous
        raise ExperimentError(
            "insufficient_history", "No recent session has the required complete history."
        )
