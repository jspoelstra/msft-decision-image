---
title: Chart Lens
description: A local chart-claim verification and historical evaluation demo for Microsoft Decision 1.1.
---

Chart Lens asks a narrow question: **can Microsoft Decision 1.1 tell whether a
chart supports the claim "The target experienced a smaller maximum drawdown
than the comparison over the displayed 30 sessions"?** Choose both tickers,
inspect the same anonymous PNG the model receives, then compare its
judgment with calculated truth and straightforward baselines.

## Try it locally

With [uv](https://docs.astral.sh/uv/) installed, run:

```bash
uv sync --python 3.11
CHART_LENS_MODE=demo uv run --no-sync uvicorn chart_lens.app:app --host 127.0.0.1
```

Open <http://127.0.0.1:8000>. Try **Check a claim** with target `MSFT` and
comparison `AAPL`, open the decision
inspector, then use **Historical evaluation** with the suggested start date
and 10 charts.
You can download the exact PNG, run manifest, and per-session CSV. Run history
persists locally; a cancelled historical evaluation can resume under its
original settings.

**Demo mode uses synthetic prices and scores.** It does not call Yahoo or
Microsoft Foundry and cannot measure the model's visual judgment.

## Use real data and Decision

You need an existing Microsoft-Decision-1.1 deployment and an Entra identity
with inference access. Review the [Decision API guide][decision] for setup.
Before enabling live mode, confirm Yahoo's terms permit your intended
retrieval, retention, and processing of the derived chart by Foundry.
[yfinance][yfinance] is unofficial and has no service guarantee.

From the project root, copy [`.env.example`](.env.example) to `.env` if you
don't already have one:

```bash
cp .env.example .env
```

In `.env`, set `CHART_LENS_MODE=live`, `CHART_LENS_ALLOW_YAHOO=1`, and your
`AZURE_ENDPOINT` and `DEPLOYMENT_NAME`. Sign in and start the app:

```bash
az login
uv run --no-sync uvicorn chart_lens.app:app --host 127.0.0.1
```

The app reads `.env` from the working directory at startup. Shell environment
variables take precedence, so the demo command above works even with live
settings in `.env`. Restart the app after editing the file. `.env` is ignored
by Git.

`AZURE_ENDPOINT` is the resource root, not a project URL or a URL ending in
`systemone`. Live inference is billable. Start with one claim check before
running a historical evaluation. The preview shows a worst-case attempt count; cache hits,
ineligible origins, and requests that do not need a retry reduce actual calls.
If you repoint a deployment, update `DECISION_MODEL_REVISION` to invalidate
local inference reuse. A deployment name alone does not guarantee replay.

Keep the server on loopback with one process. It is not a public hosting
configuration. Local artifacts and snapshots are kept in `.chart-lens/`;
`CHART_LENS_DB` can point to a different SQLite file. Use **Run history** to
delete saved runs. No Yahoo key or Finnhub subscription is needed.

## Interpret the results

The chart uses 30 completed regular sessions, historical 52-week extremes,
and two relative-price paths labeled Target and Comparison. Both ticker inputs
are required; there is no fixed-index fallback. Decision selects **Supported**,
**Contradicted**, or **Insufficient visual evidence**. The evaluator calculates
each path's largest percentage decline from an earlier closing peak to a later
trough inside the window, using split-adjusted prices. A strictly smaller target
drawdown supports the claim; ties contradict it. Flat or rising paths have zero
drawdown. Identical tickers are allowed as a tie control.

Unlike endpoint outperformance, this requires following the full path, even if
prices later recover. Loss is measured relative to the earlier peak, not as the
vertical percentage-point drop on the rebased chart. The drawdown margin is
Comparison minus Target: positive supports the claim. No future outcome is needed.

Accuracy and class metrics use answered claims; answer coverage exposes
abstentions. Evaluation coverage also exposes missing or failed judgments.
Overall accuracy counts abstentions as incorrect. Probability losses and the
two-by-three confusion matrix include abstentions. Margin slices distinguish
near-ties from obvious gaps, and baselines use matched answered sessions.
The historical-majority baseline uses 252 earlier 30-session windows only.

Historical runs save each judgment before attaching numerical truth. These
drawdown measurements and labels never accompany the model's image. Data retrieved today can
include revisions, and overlapping windows are dependent. Fewer than 30
evaluated charts are flagged exploratory. Reading a chart correctly does not
establish forecasting or trading ability. This is not investment advice.

Old SPY-outperformance, five-band, and binary forecasting runs remain in **Run history**, labeled
as legacy experiments with their original results and exports. They cannot
resume under drawdown-comparison rules or supply cached drawdown scores.

## Verify the app

```bash
uv run --no-sync ruff check chart_lens tests
uv run --no-sync pytest -q
```

Tests run without live credentials. See the [experiment specification](doc/spec.md)
for data rules, class boundaries, and evaluation methodology.

[decision]: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision
[yfinance]: https://ranaroussi.github.io/yfinance/
