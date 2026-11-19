"""Exercise a real local worker, database, renderer, and HTTP interface."""

import csv
from hashlib import sha256
from io import StringIO
from threading import Event
from time import sleep

import pytest
from fastapi.testclient import TestClient

from chart_lens.app import create_app
from chart_lens.decision import validate
from chart_lens.report import Row


def test_given_dotenv_live_settings_when_app_starts_then_configures_scorer(
    tmp_path, monkeypatch, mocker
):
    # Arrange
    monkeypatch.chdir(tmp_path)
    for name in (
        "CHART_LENS_MODE",
        "CHART_LENS_ALLOW_YAHOO",
        "AZURE_ENDPOINT",
        "DEPLOYMENT_NAME",
        "DECISION_MODEL_REVISION",
        "CHART_LENS_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "CHART_LENS_MODE=live\nCHART_LENS_ALLOW_YAHOO=1\n"
        "AZURE_ENDPOINT=https://example.services.ai.azure.com/\n"
        "DEPLOYMENT_NAME='file-deployment'\nDECISION_MODEL_REVISION=fixture-v1\n"
        "CHART_LENS_DB='data directory/runs.sqlite3'\n",
        encoding="utf-8",
    )
    scorer = mocker.patch("chart_lens.app.FoundryScorer")
    app = create_app()

    # Act / Assert
    with TestClient(app) as client:
        assert client.get("/api/config").json()["mode"] == "live"
        assert app.state.service.model_revision == "fixture-v1"
        assert (tmp_path / "data directory" / "runs.sqlite3").is_file()
    scorer.assert_called_once_with("https://example.services.ai.azure.com", "file-deployment")


def test_given_dotenv_opt_in_when_environment_disables_yahoo_then_startup_is_rejected(
    tmp_path, monkeypatch
):
    # Arrange
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHART_LENS_MODE", raising=False)
    monkeypatch.setenv("CHART_LENS_ALLOW_YAHOO", "0")
    (tmp_path / ".env").write_text(
        "CHART_LENS_MODE=live\nCHART_LENS_ALLOW_YAHOO=1\n", encoding="utf-8"
    )

    # Act / Assert
    with pytest.raises(ValueError, match="CHART_LENS_ALLOW_YAHOO=1"):
        create_app()


def _wait(client, run_id):
    for _ in range(250):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        sleep(0.04)
    raise AssertionError("Local job did not stop in time")


def test_given_weekend_backtest_when_run_then_exports_match_and_repeat_uses_cache(tmp_path):
    # Arrange
    request = {
        "ticker": " msft ", "comparison": " aapl ", "scheme": "claim",
        "start": "2025-01-18", "count": 2,
    }
    storage = tmp_path / "runs.sqlite3"

    # Act
    with TestClient(create_app(storage=storage, mode="demo")) as client:
        preview = client.post("/api/backtests/preview", json=request).json()
        run_id = client.post("/api/backtests", json=request).json()["id"]
        run = _wait(client, run_id)
        rows = client.get(f"/api/runs/{run_id}/rows").json()
        image = client.get(f"/api/runs/{run_id}/charts/{rows[0]['origin']}.png")
        csv = client.get(f"/api/runs/{run_id}/predictions.csv")
        manifest = client.get(f"/api/runs/{run_id}/manifest.json").json()
    with TestClient(create_app(storage=storage, mode="demo")) as client:
        again_id = client.post("/api/backtests", json=request).json()["id"]
        again = _wait(client, again_id)

    # Assert
    assert (preview["effective_start"], preview["last_origin"]) == ("2025-01-21", "2025-01-22")
    assert run["status"] == "complete", run
    assert run["report"]["counts"]["evaluated"] == 2
    assert run["report"]["counts"]["answered"] + run["report"]["counts"]["abstained"] == 2
    assert run["experiment"] == "drawdown-comparison-v1"
    assert run["ticker"] == manifest["ticker"] == preview["ticker"] == "MSFT"
    assert run["comparison"] == manifest["comparison"] == preview["comparison"] == "AAPL"
    assert all(row["target"] is None and row["drawdown_margin"] is not None for row in rows)
    assert all(row["outperformance_margin"] is None for row in rows)
    assert rows[0]["actual"] and rows[0]["model"] == "synthetic-hash-v1"
    assert sha256(image.content).hexdigest() == rows[0]["image_sha"]
    assert csv.status_code == 200 and rows[0]["origin"] in csv.text
    assert manifest["snapshot_id"] and manifest["data_hash"]
    assert again["report"]["cache_hits"] == 2


def test_given_active_job_when_cancelled_then_resume_preserves_completed_inference(
    tmp_path, mocker
):
    # Arrange
    entered, release = Event(), Event()
    app = create_app(storage=tmp_path / "runs.sqlite3", mode="demo")
    request = {
        "ticker": "DIA", "comparison": "AAPL", "scheme": "claim",
        "start": "2025-01-14", "count": 3,
    }
    with TestClient(app) as client:
        scorer = app.state.service.scorer
        original = scorer.score

        def delayed(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)

        spy = mocker.patch.object(scorer, "score", side_effect=delayed)

        # Act
        run_id = client.post("/api/backtests", json=request).json()["id"]
        try:
            assert entered.wait(5)
            before_label = client.get(f"/api/runs/{run_id}/rows").json()[0]
            assert client.post(f"/api/backtests/{run_id}/cancel").status_code == 200
        finally:
            release.set()
        cancelled = _wait(client, run_id)
        assert client.post(f"/api/backtests/{run_id}/resume").status_code == 202
        resumed = _wait(client, run_id)

    # Assert
    assert before_label["actual"] is None and before_label["image_sha"]
    assert before_label["target_return"] is before_label["spy_return"] is None
    assert set(before_label["request"]) == {"model", "state", "assets", "questions"}
    assert set(before_label["request"]["questions"]) == {"claim"}
    assert cancelled["status"] == "cancelled" and cancelled["completed"] == 1
    assert resumed["status"] == "complete" and resumed["completed"] == 3
    assert resumed["comparison"] == "AAPL"
    assert before_label["target_drawdown"] is before_label["comparison_drawdown"] is None
    assert spy.call_count == 3


def test_given_identical_tickers_when_complete_then_tie_is_contradicted_and_fetch_is_deduplicated(
    tmp_path, mocker
):
    # Arrange
    app = create_app(storage=tmp_path / "runs.sqlite3", mode="demo")
    with TestClient(app) as client:
        fetch = mocker.spy(app.state.service.market, "fetch")
        # Act
        run_id = client.post(
            "/api/predictions", json={"ticker": "SPY", "comparison": "SPY"}
        ).json()["id"]
        run = _wait(client, run_id)
        (row,) = client.get(f"/api/runs/{run_id}/rows").json()

        # Assert
        assert run["status"] == "complete"
        assert row["status"] == "evaluated" and row["actual"] == "contradicted"
        assert row["drawdown_margin"] == 0 and row["target"] is None
        assert row["target_drawdown"] == row["comparison_drawdown"]
        assert fetch.call_args.args[0] == ("SPY",)


def test_given_future_chart_or_cross_origin_when_submitted_then_no_job_is_created(tmp_path):
    # Arrange
    with TestClient(create_app(storage=tmp_path / "runs.sqlite3", mode="demo")) as client:
        latest = client.get("/api/config").json()["latest"]

        # Act
        accepted = client.post(
            "/api/backtests/preview",
            json={"ticker": "MSFT", "comparison": "AAPL", "start": latest, "count": 1},
        )
        future = client.post(
            "/api/backtests",
            json={"ticker": "MSFT", "comparison": "AAPL", "start": latest, "count": 2},
        )
        cross = client.post(
            "/api/predictions", json={"ticker": "MSFT", "comparison": "AAPL"},
            headers={"Origin": "https://example.com"},
        )

        # Assert
        assert accepted.status_code == 200 and accepted.json()["last_origin"] == latest
        assert future.status_code == 422 and future.json()["detail"]["code"] == "future_origin"
        assert cross.status_code == 403
        assert client.get("/api/runs").json() == []


def test_given_abstained_claim_when_exporting_then_truth_is_retained_and_hit_is_blank(
    tmp_path, mocker
):
    # Arrange
    app = create_app(storage=tmp_path / "runs.sqlite3", mode="demo")
    answer = validate(
        {
            "model": "fixture",
            "answers": {
                "claim": {
                    "type": "choice",
                    "choice": "insufficient",
                    "probabilities": {"supported": 0.1, "contradicted": 0.2, "insufficient": 0.7},
                }
            },
        },
        "claim",
        1,
        1,
    )
    with TestClient(app) as client:
        mocker.patch.object(app.state.service.scorer, "score", return_value=answer)

        # Act
        run_id = client.post(
            "/api/predictions", json={"ticker": "SPY", "comparison": "SPY"}
        ).json()["id"]
        run = _wait(client, run_id)
        (export,) = csv.DictReader(StringIO(client.get(f"/api/runs/{run_id}/predictions.csv").text))
        (row,) = client.get(f"/api/runs/{run_id}/rows").json()

        # Assert
        assert run["status"] == "complete"
        assert run["report"]["counts"]["abstained"] == 1
        assert run["report"]["accuracy"] is None and run["report"]["coverage"] == 0
        assert export["hit"] == "" and export["actual"] == row["actual"] == "contradicted"
        assert export["drawdown_margin"] == str(row["drawdown_margin"])
        assert export["ticker"] == export["comparison"] == "SPY"
        assert export["target_drawdown"] == str(row["target_drawdown"])
        assert export["comparison_drawdown"] == str(row["comparison_drawdown"])
        assert export["outperformance_margin"] == ""
        assert export["target"] == "" and export["window_start"] == row["window_start"]


@pytest.mark.parametrize("experiment", [None, "spy-outperformance-v1"])
def test_given_legacy_run_when_opening_then_artifacts_remain_and_resume_is_rejected(
    tmp_path, experiment
):
    # Arrange
    app = create_app(storage=tmp_path / "runs.sqlite3", mode="demo")
    legacy = {
        "id": "legacy",
        "created_at": "2025-01-02",
        "kind": "backtest",
        "scheme": "binary",
        "status": "cancelled",
        "mode": "demo",
        "ticker": "MSFT",
        "report": {"accuracy": 0.5},
        "fingerprint": "old-experiment",
    }
    if experiment:
        legacy.update(experiment=experiment, scheme="claim")
    original = (
        Row(origin="2025-01-02", choice="supported", actual="supported",
            target_return=0.1, spy_return=0.05, outperformance_margin=0.05)
        if experiment else
        Row(origin="2025-01-02", target="2025-01-03", choice="up", actual="not_up")
    )
    with TestClient(app) as client:
        app.state.service.store.create(legacy, [original])

        # Act
        manifest = client.get("/api/runs/legacy/manifest.json").json()
        (row,) = client.get("/api/runs/legacy/rows").json()
        resume = client.post("/api/backtests/legacy/resume")
        forecast = client.post(
            "/api/predictions",
            json={"ticker": "MSFT", "comparison": "AAPL", "scheme": "binary"},
        )
        (export,) = csv.DictReader(StringIO(client.get("/api/runs/legacy/predictions.csv").text))

        # Assert
        assert manifest == legacy
        assert row["choice"] == original.choice and row["target"] == original.target
        assert row["outperformance_margin"] == original.outperformance_margin
        assert export["drawdown_margin"] == export["comparison"] == ""
        assert resume.status_code == 409 and resume.json()["detail"]["code"] == "legacy_experiment"
        assert forecast.status_code == 422


@pytest.mark.parametrize("path", ["/api/predictions", "/api/backtests", "/api/backtests/preview"])
@pytest.mark.parametrize("comparison", [None, "", "BAD SYMBOL", "../AAPL"])
def test_given_missing_or_invalid_comparison_when_submitting_then_request_is_rejected(
    tmp_path, path, comparison
):
    # Arrange
    request = {"ticker": "MSFT"}
    if comparison is not None:
        request["comparison"] = comparison
    if "backtests" in path:
        request.update(start="2025-01-06", count=1)
    with TestClient(create_app(storage=tmp_path / "runs.sqlite3", mode="demo")) as client:
        # Act
        response = client.post(path, json=request)

        # Assert
        assert response.status_code == 422
        assert client.get("/api/runs").json() == []


def test_given_equal_images_for_different_pairs_when_running_then_pair_scores_are_not_reused(
    tmp_path, mocker
):
    # Arrange
    app = create_app(storage=tmp_path / "runs.sqlite3", mode="demo")
    with TestClient(app) as client:
        market = app.state.service.market
        original = market.fetch

        def fetch(symbols, first, last):
            histories = original(symbols, first, last)
            if "IBM" in histories:
                histories["IBM"] = original(("AAPL",), first, last)["AAPL"]
            return histories

        fetch_spy = mocker.patch.object(market, "fetch", side_effect=fetch)
        scorer = mocker.spy(app.state.service.scorer, "score")
        runs, images = [], []

        # Act
        for comparison in ("AAPL", "IBM"):
            run_id = client.post(
                "/api/predictions", json={"ticker": "MSFT", "comparison": comparison}
            ).json()["id"]
            runs.append(_wait(client, run_id))
            images.append(client.get(f"/api/runs/{run_id}/rows").json()[0]["image_sha"])

        # Assert
        assert all(run["status"] == "complete" for run in runs)
        assert images[0] == images[1]
        assert runs[0]["fingerprint"] != runs[1]["fingerprint"]
        assert runs[1]["report"]["cache_hits"] == 0 and scorer.call_count == 2
        assert [call.args[0] for call in fetch_spy.call_args_list] == [
            ("AAPL", "MSFT"), ("IBM", "MSFT"),
        ]
