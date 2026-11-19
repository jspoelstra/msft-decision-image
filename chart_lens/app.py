"""Loopback-only FastAPI interface and static dashboard."""

import csv
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from dotenv import dotenv_values
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from chart_lens.decision import DemoScorer, FoundryScorer
from chart_lens.domain import CLAIM, SCHEMES, ExperimentError, Scheme, Sessions
from chart_lens.market import DemoMarket, YahooMarket
from chart_lens.report import Row
from chart_lens.service import Service
from chart_lens.store import Store, canonical

STATIC = Path(__file__).with_name("static")
DEFAULT_STORE = Path(".chart-lens") / "runs.sqlite3"
Symbol = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{0,9}(?:[-.][A-Z0-9]{1,4})?$")]


class PredictionRequest(BaseModel):
    """Public request for the latest accepted session."""

    model_config = ConfigDict(extra="forbid")
    ticker: Symbol
    comparison: Symbol
    scheme: Scheme = "claim"

    @field_validator("ticker", "comparison", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        """Normalize input without discarding meaningful symbol punctuation."""
        return value.strip().upper() if isinstance(value, str) else value


class BacktestRequest(PredictionRequest):
    """User-selected historical interval, measured in market sessions."""

    start: date
    count: int = Field(default=60, ge=1, le=252, strict=True)


def _configured_endpoint(raw: str) -> str:
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.port not in (443, None)
        or not parsed.hostname
        or not parsed.hostname.endswith((".services.ai.azure.com", ".cognitiveservices.azure.com"))
    ):
        raise ValueError("AZURE_ENDPOINT must be a public HTTPS Foundry resource root URL.")
    return raw.rstrip("/")


def create_app(*, storage: Path | None = None, mode: str | None = None) -> FastAPI:
    """Assemble local or opted-in live workflows without accessing external services at startup."""
    dotenv = dotenv_values(Path(".env"))

    def setting(name: str, default: str = "") -> str:
        value = os.getenv(name)
        if value is None:
            value = dotenv.get(name)
        return default if value is None else value

    sessions = Sessions()
    selected = mode or setting("CHART_LENS_MODE", "unconfigured").lower()
    if selected not in ("demo", "live", "unconfigured"):
        raise ValueError("CHART_LENS_MODE must be demo, live, or unconfigured.")
    endpoint = ""
    deployment = "synthetic-hash-v1" if selected == "demo" else setting("DEPLOYMENT_NAME")
    if selected == "live":
        if setting("CHART_LENS_ALLOW_YAHOO") != "1":
            raise ValueError(
                "Confirm the Yahoo data terms and set CHART_LENS_ALLOW_YAHOO=1 for live mode."
            )
        if not deployment:
            raise ValueError("Set DEPLOYMENT_NAME to the Microsoft Decision deployment name.")
        endpoint = _configured_endpoint(setting("AZURE_ENDPOINT"))
    revision = setting("DECISION_MODEL_REVISION") if selected == "live" else ""
    database = storage or Path(setting("CHART_LENS_DB", str(DEFAULT_STORE)))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        live = FoundryScorer(endpoint, deployment) if selected == "live" else None
        scorer = DemoScorer() if selected == "demo" else live
        api.state.service = Service(
            Store(database),
            sessions,
            DemoMarket(sessions) if selected == "demo" else YahooMarket(),
            scorer,
            mode=selected,
            deployment=deployment,
            model_revision=revision,
            endpoint=endpoint,
        )
        try:
            yield
        finally:
            api.state.service.close()
            if live is not None:
                live.close()

    api = FastAPI(title="Chart Lens", lifespan=lifespan, docs_url=None, redoc_url=None)
    api.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )

    @api.middleware("http")
    async def local_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method in ("POST", "DELETE"):
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            cross_site = request.headers.get("sec-fetch-site") not in (None, "same-origin", "none")
            if cross_site or (origin and origin != f"{request.url.scheme}://{host}"):
                return _error(
                    "cross_origin", "Use the local Chart Lens page for this operation.", 403
                )
            size = request.headers.get("content-length")
            if size is not None and (not size.isdecimal() or len(size) > 5 or int(size) > 4096):
                return _error("request_too_large", "Request body exceeds 4 KiB.", 413)
            if size is None and request.headers.get("transfer-encoding"):
                return _error(
                    "unknown_body_length", "Send a bounded request with Content-Length.", 411
                )
            if request.url.path in ("/api/predictions", "/api/backtests", "/api/backtests/preview"):
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].strip()
                    != "application/json"
                ):
                    return _error("content_type", "Send an application/json body.", 415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @api.exception_handler(ExperimentError)
    async def experiment_error(_: Request, exc: ExperimentError) -> JSONResponse:
        return _error(exc.code, str(exc), exc.status)

    def service() -> Service:
        return api.state.service

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/api/config")
    def config() -> dict:
        latest = sessions.latest()
        return {
            "mode": selected,
            "latest": sessions.dates[latest].isoformat(),
            "demo_start": sessions.dates[latest - 60].isoformat(),
            "schemes": SCHEMES,
            "claim": CLAIM,
        }

    @api.post("/api/predictions", status_code=202)
    def prediction(body: PredictionRequest) -> dict:
        return service().start(body.ticker, body.comparison, body.scheme)

    @api.post("/api/backtests/preview")
    def preview(body: BacktestRequest) -> dict:
        return service().preview(body.ticker, body.comparison, body.scheme, body.start, body.count)

    @api.post("/api/backtests", status_code=202)
    def backtest(body: BacktestRequest) -> dict:
        return service().start(
            body.ticker, body.comparison, body.scheme, start=body.start, count=body.count
        )

    @api.get("/api/runs")
    def runs() -> list[dict]:
        return [
            {key: value for key, value in run.items() if key != "report"}
            for run in service().store.runs()
        ]

    @api.get("/api/runs/{run_id}")
    def run(run_id: str) -> dict:
        return service().store.get(run_id)

    @api.get("/api/runs/{run_id}/rows")
    def rows(
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=252)] = 252,
    ) -> list[Row]:
        service().store.get(run_id)
        return service().store.rows(run_id, offset, limit)

    @api.get("/api/runs/{run_id}/charts/{origin}.png")
    def chart(run_id: str, origin: date) -> Response:
        return Response(service().store.image(run_id, origin.isoformat()), media_type="image/png")

    @api.get("/api/runs/{run_id}/manifest.json")
    def manifest(run_id: str) -> JSONResponse:
        run = service().store.get(run_id)
        return JSONResponse(run, headers=_attachment(f"{run['id']}-manifest.json"))

    @api.get("/api/runs/{run_id}/predictions.csv")
    def predictions(run_id: str) -> Response:
        run = service().store.get(run_id)
        with StringIO(newline="") as output:
            keys = (
                "ticker",
                "comparison",
                "origin",
                "target",
                "window_start",
                "status",
                "eligible",
                "choice",
                "actual",
                "realized_return",
                "target_return",
                "spy_return",
                "outperformance_margin",
                "target_drawdown",
                "comparison_drawdown",
                "drawdown_margin",
                "hit",
                "probabilities",
                "normalized",
                "baselines",
                "image_sha",
                "attempts",
                "cache_hit",
                "latency_ms",
                "error",
            )
            writer = csv.DictWriter(output, fieldnames=keys)
            writer.writeheader()
            for row in service().store.rows(run_id):
                data = row.model_dump(mode="json")
                data["ticker"] = run["ticker"]
                data["comparison"] = run.get("comparison")
                data["hit"] = (
                    row.choice == row.actual
                    if row.actual is not None and row.choice not in (None, "insufficient")
                    else None
                )
                writer.writerow(
                    {
                        key: canonical(data[key]) if isinstance(data[key], dict) else data[key]
                        for key in keys
                    }
                )
            return Response(
                output.getvalue(),
                media_type="text/csv",
                headers=_attachment(f"{run['id']}-predictions.csv"),
            )

    @api.post("/api/backtests/{run_id}/cancel")
    def cancel(run_id: str) -> dict:
        return service().cancel(run_id)

    @api.post("/api/backtests/{run_id}/resume", status_code=202)
    def resume(run_id: str) -> dict:
        return service().resume(run_id)

    @api.delete("/api/runs/{run_id}", status_code=204)
    def delete(run_id: str) -> Response:
        service().delete(run_id)
        return Response(status_code=204)

    @api.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    api.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return api


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


def _attachment(name: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{name}"'}


app = create_app()
