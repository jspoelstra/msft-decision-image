---
title: Chart Lens educational demo specification
description: Image-only comparison of two tickers' maximum drawdowns with Microsoft Decision 1.1 and auditable historical evaluation.
ms.date: 2026-11-19
ms.topic: reference
---

## Purpose and status

Chart Lens asks whether Microsoft-Decision-1.1 can judge a claim from a chart:

> The target experienced a smaller maximum drawdown than the comparison over
> the displayed 30 sessions.

Specification version 0.6 replaces fixed-SPY endpoint outperformance with
maximum-drawdown comparison against a user-entered ticker. Comparing final
relative-performance endpoints was deliberately simple; drawdown requires
reasoning over ordered peaks and subsequent troughs across the entire path.
The model judges visible evidence, not future prices.
The numerical evaluator determines whether the claim is true independently.
Existing forecast and SPY-outperformance runs remain inspectable and exportable
as legacy experiments; they are never relabeled, mixed with new results, or
resumed under new rules.

> [!WARNING]
> Educational experiment only. Not investment advice or a trading system.
> Correctly reading historical charts does not establish predictive ability.
> Model probabilities are not established probabilities of financial outcomes.

## Scope and user experience

Retain the local FastAPI application, static dashboard, one background worker,
SQLite run history, immutable snapshots, chart inspector, cancellation/resume,
cache accounting, JSON manifests, CSV results, and image downloads.

### Check a claim

1. Enter target and comparison U.S. stock or ETF tickers. Both are required
   and use the same validation and normalization. There is one fixed claim
   and rubric, not a selectable task or arbitrary user prompt.
2. Resolve the latest completed NYSE session after its one-hour acceptance delay.
   If required data is not finalized, disclose the older effective session.
3. Retrieve aligned history and render the chart.
4. Ask Decision to select Supported, Contradicted, or Insufficient visual evidence.
5. Persist the decision before attaching the calculated truth and drawdown margin.
6. Show the selected judgment, all three probabilities, actual claim truth,
   target and comparison maximum drawdowns, and their difference in percentage points.

Maximum drawdown is a nonnegative fractional loss from an earlier closing-price
peak to a subsequent closing-price trough, both inside the displayed window.
Smaller is better. Equal drawdowns, including identical tickers and two
nondecreasing series with zero drawdown, contradict the strict claim.
Allow identical tickers as a useful tie control; fetch each unique symbol once.

### Historical evaluation

Choose a first chart-ending session and 1 to 252 consecutive exchange sessions
(default 60). Weekend and holiday starts advance to the next exchange session.
Preview the effective interval, eligible charts, cached results, and maximum
live attempts before confirmation.

For N charts beginning at index i, the last chart ends at i + N - 1.
The latest finalized session itself is eligible. No next-session outcome is
required, fetched for labeling, or shown as the evaluation target. Reject
intervals extending beyond the latest accepted session; do not silently shorten.
Missing required bars produce explicit skipped rows, not substituted sessions.

Run history distinguishes legacy forecasting, SPY-outperformance, and drawdown tasks.
Existing API route names may remain for compatibility, but new manifests carry
`experiment: drawdown-comparison-v1`, both tickers, and `scheme: claim`. Only the claim scheme
can create new runs. Legacy run artifacts retain their original semantics.

## Market-data contract

Use the existing pinned yfinance adapter. It is unofficial, without a service
guarantee. Before live use, require explicit acknowledgment that applicable Yahoo
terms permit retrieval, retention, and processing of derived charts by Foundry.
Do not silently substitute another provider. No Finnhub or Yahoo credentials
are required.

Retain explicit download options: daily regular-session data, actions enabled,
`auto_adjust=False`, `back_adjust=False`, `repair=False`, `rounding=False`,
`keepna=True`, bounded timeout, and raised provider errors. Reject unsupported
currencies/instruments, duplicate or unsorted dates, nonpositive or nonfinite
prices, and invalid split factors. Preserve missing bars as missing.

Use split-adjusted price returns, not dividend-adjusted total returns. Normalize
provider history to original share basis, then express each chart's prices on
the basis effective at its ending session. Do not double-adjust splits or use
`Adj Close`. Later splits must not alter an earlier chart's absolute levels.

Freeze downloaded inputs, retrieval time, provider options/version, and hashes.
Retrieve enough earlier history for the chart's 52-week range and the historical
baseline. A missing baseline history must not make an otherwise valid chart
ineligible. Throttle requests and reuse fresh covering snapshots.

## Image specification and isolation

Render a deterministic 1568 by 980 PNG (at most 1568 pixels per side), with
fixed fonts, white background, high contrast, and no embedded text metadata.
Keep the existing two-panel template:

* Target closing prices for exactly 30 aligned completed trading sessions,
  plus the trailing 52-week intraday high and low.
* Target and Comparison percentage changes relative to each series' own
  first displayed close, on one common axis, with distinct colors and line styles.

Show relative session labels 1 through 30. Exclude ticker, company name,
calendar dates, and provider watermarks. Labels are `Target` and `Comparison`,
never the entered tickers. Show both identities in the surrounding UI and exports
only. Remove the fixed DIA/QQQ/SPY context series and downloads. Do not plot
precomputed drawdown curves, label extrema for the answer, or provide a numerical
shortcut that turns this back into endpoint comparison.
The 52-week interval is inclusive from T minus 364 days through T.

The inspector and model use exactly the same PNG bytes. Only the image and
fixed task/rubric reach Decision: no drawdown values, margins, truth labels, baselines,
historical outcomes, or numerical market features accompany the request.
Changing bars or splits after T must leave the image and its truth unchanged.

## Truth definition

For the 30 aligned closing prices expressed on the ending session's split
basis, compute each series' maximum drawdown in Decimal:

```text
running_peak(t) = max(close(1), ..., close(t))
drawdown(t) = 1 - close(t) / running_peak(t)
maximum_drawdown = max(drawdown(1), ..., drawdown(30))
margin = comparison_maximum_drawdown - target_maximum_drawdown
actual = supported if margin > 0 else contradicted
```

Use unrounded prices and drawdowns for the label, before converting to display
floats. There is no tolerance band that redefines the claim. Exact ties,
including the same target and comparison ticker, are contradicted. Do not use
the global high and low without respecting time order, include pre-window peaks,
or confuse a percentage-point decline on the normalized chart with a percentage
loss from the earlier peak. Insufficient visual evidence is
a model abstention, never a ground-truth class.

## Decision integration

Retain Entra authentication and the resource-root endpoint:

```text
POST {AZURE_ENDPOINT}/providers/microsoft/v1/systemone
```

Use the configured deployment name as `model`. The state references
`image:chart.png`; `assets.chart.png` contains the exact PNG as `data_base64`.
There is one `choice` question named `claim`, with these criteria:

| Choice | Meaning |
|--------|---------|
| `supported` | Target's maximum drawdown is strictly smaller than Comparison's |
| `contradicted` | Target's maximum drawdown is greater than or equal to Comparison's |
| `insufficient` | The image does not allow the comparison to be resolved reliably |

Explain the panels, common baseline, positive-loss convention, chronological
peak/trough definition, and within-window boundary in the fixed instructions.
State that final endpoints alone do not answer the question. Ask for visual
evidence only; do not infer missing data or future moves. Freeze the complete
question, criteria, and prompt hash in every manifest.

Validate exactly these three probability keys, a recognized selected choice,
finite nonboolean numbers in [0, 1], a sum within 0.02 of one, and selection
within 1e-8 of the maximum probability. Preserve raw probabilities and normalize
accepted near-unit distributions for metrics. Never substitute an argmax or
invent a rationale. Invalid responses remain failures.

Demo mode produces deterministic synthetic judgments from the image hash;
it must never be presented as Foundry performance.

## Execution and persistence

Persist images and decisions before joining evaluator-only labels. Evaluate
both single checks and historical runs. No future target is required.
Rows record the displayed window start, ending session, target/comparison drawdowns,
drawdown margin (comparison minus target), truth, judgment, raw/normalized probabilities, image hash,
request/response, latency, attempts, cache provenance, and safe errors.
Legacy origin/target, realized-return, and SPY-outperformance fields remain readable;
new rows have no future target.

Use one active job and one inference request at a time. Single checks allow
one attempt; historical evaluation allows at most three per origin, retrying
only transient transport errors, 429, and eligible 5xx responses with bounded
backoff and Retry-After. Live inference is billable; timeouts can incur duplicate
charges. Show the worst-case bound of 3 times requested charts before confirming.

Cancellation preserves completed work and stops scheduling new charts. Restart
marks active jobs interrupted. Resume uses frozen inputs only and requires a
matching experiment fingerprint. Cache identity includes experiment, rubric,
template/renderer, ordered target/comparison pair, chart inputs and exact image
check, deployment, and known model revision. Changing either ticker invalidates
pair-specific cache reuse. Never reuse old forecast or outperformance scores.

Keep old run data and reports unchanged. Reject resuming legacy experiments
with a clear message. Do not overwrite or migrate old results into new metrics.

## Performance analysis

Show requested, eligible, scored, evaluated, answered, abstained, failed,
skipped, and pending counts. An evaluated row has a valid judgment and truth;
an answered row selects Supported or Contradicted.

* Accuracy, balanced accuracy, per-class precision/recall/F1, and macro-F1 use
  answered rows. Report two truth classes and mark undefined metrics unavailable.
* Answer coverage is answered divided by evaluated. Also show evaluation
  coverage (evaluated/requested) and effective coverage (answered/requested),
  so failures and abstentions cannot disappear behind a favorable accuracy.
* Overall accuracy counts abstentions as incorrect across all evaluated rows.
* The confusion matrix has two actual rows and three predicted columns,
  including insufficient evidence.
* Log loss and Brier score use all evaluated rows and the full three-way
  distribution; the true probability of the abstention class is zero. Do not
  renormalize away abstention mass. Clip probabilities at 1e-15 and renormalize
  for log loss; Brier is the sum of squared errors, not divided by class count.
* Calibration bins use the selected choice's normalized confidence across all
  evaluated rows. Abstentions are not correct truth predictions.
* Report latency median/p95, attempts, usage, cache hits, and monthly slices.

Break results down by absolute maximum-drawdown margin, calculated in percentage
points: below 0.1, 0.1 to below 0.5, 0.5 to below 2, and at least 2. Each slice
shows evaluated/answered/abstained counts, answer coverage, and answered accuracy.
These are diagnostic bins, not alternate truth thresholds.

### Baselines

Compare always-supported, always-contradicted, and historical-majority baselines
on the same answered rows as model accuracy. Show the matched sample count and
model-minus-baseline accuracy difference in percentage points.

Historical majority uses exactly 252 earlier 30-session windows ending before
the current chart's ending session, using the same ordered ticker pair and drawdown
definition. Require complete target and comparison bars across
each window. Use one Laplace count per truth class; a probability tie selects
contradicted. Never fit the baseline on the current or future evaluation labels.
When history is missing, omit that row's historical baseline and disclose its
matched count. Compare its probability metrics with model metrics on that
matched subset. Keep a uniform three-choice probability reference.

Below 30 evaluated charts, label results exploratory. Overlapping windows are
dependent observations, not independent trials. Report no statistical
significance or trading returns. Hold out chronological evaluation periods
before prompt/template tuning; numerical ground truth measures visual reading,
not financial forecasting or general investment skill.

## Operations, API, and validation

Keep the existing loopback-only server, origin/host checks, bounded request
bodies, 24-run retention limit with explicit deletion, and server-side
credentials. Never expose tokens or raw provider secrets in artifacts.
No new cloud provisioning, dependencies, uploads, or public hosting are needed.

Retain existing run/status/rows, image, manifest, CSV, preview, cancel, resume,
and delete routes. New input defaults to `scheme: claim` and rejects forecast
schemes. Both `ticker` and `comparison` are required for new single checks,
historical previews, and historical runs. CSV includes both identities,
claim-window dates, truth, selected judgment, all probabilities, target/comparison
maximum drawdowns, signed drawdown margin, hit, provenance, and errors.
An abstention has a blank hit, not a fabricated binary judgment.

Required automated checks:

* Chronological peak/trough drawdowns that disagree with endpoint performance
  or global high/low comparisons, equal drawdowns, increasing/flat series,
  identical tickers, and splits inside the displayed window
* Latest-session eligibility, one-chart and multi-chart intervals, holidays,
  unavailable future chart ends, missing bars, and baseline history gaps
* Exact PNG wire bytes, claim-only request shape, malformed probabilities,
  abstention acceptance, retries, and absence of numerical label leakage
* Hand-calculated metrics for correct, incorrect, abstained, failed, and
  all-abstained runs; exact margin-bin boundaries and baseline matching
* Both ticker fields' validation and normalization, comparison data absence,
  no fixed-index downloads, ordered-pair cache isolation, and frozen-pair resume
* Persistence before labels, cancellation/resume, legacy
  history readability, JSON/CSV consistency, and image hashes
* Browser validation of a single check, historical preview/run, inspector,
  report, run history, and explicit failures without live credentials

Ordinary tests use isolated fixtures and require no live credentials. Live
smoke tests are separate, billable, and opt-in.

## References

* [Microsoft Decision API and image guide](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)
* [yfinance documentation](https://ranaroussi.github.io/yfinance/)
* [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)
