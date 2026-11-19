"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const fallbackSchemes = {
    claim: { supported: "Supported", contradicted: "Contradicted", insufficient: "Insufficient visual evidence" },
    bands: { down_large: "Down more than 1%", down_small: "Down 0.5% to 1%", stable: "Within ±0.5%", up_small: "Up 0.5% to 1%", up_large: "Up more than 1%" },
    binary: { up: "Up", not_up: "Down or unchanged" },
  };
  const terminal = new Set(["complete", "failed", "cancelled", "interrupted"]);
  const state = { config: null, run: null, rows: [], timer: null, generation: 0, preview: null, sort: "origin", direction: 1, view: "predict", historyGeneration: 0 };
  const escape = (value) => String(value ?? "—").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const number = (value, digits = 3) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : "—";
  const percent = (value) => typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
  const points = (value) => typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(3)} pp` : "—";
  const isDrawdown = (run = state.run) => run?.experiment === "drawdown-comparison-v1";
  const isClaim = (run = state.run) => isDrawdown(run) || run?.experiment === "spy-outperformance-v1";
  const pairLabel = (run = state.run) => isDrawdown(run) ? `${run.ticker} versus ${run.comparison}` : run.ticker;
  const marginValue = (row) => isDrawdown() ? row.drawdown_margin : row.outperformance_margin;
  const humanize = (value) => String(value ?? "Not reported").replace(/[_-]/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  const runPath = (id) => `/api/runs/${encodeURIComponent(id)}`;
  const classes = (scheme = state.run?.scheme || "claim") => state.config?.schemes?.[scheme] || fallbackSchemes[scheme] || {};
  const classLabel = (id, scheme) => id == null ? "—" : classes(scheme)[id] || humanize(id);
  const statusBadge = (status) => `<span class="badge ${["queued", "running", "complete", "failed", "cancelled", "interrupted", "evaluated", "scored", "pending", "skipped"].includes(status) ? status : "neutral"}">${escape(humanize(status))}</span>`;
  const metadata = (items) => items.map(([name, value]) => `<dl><dt>${escape(name)}</dt><dd>${escape(value ?? "—")}</dd></dl>`).join("");
  function announce(message) { $("announcement").textContent = message; }
  function showError(error) {
    $("error").textContent = error instanceof Error ? error.message : String(error);
    $("error").hidden = false;
  }
  function clearError() { $("error").hidden = true; $("error").textContent = ""; }
  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers } });
    let data;
    try { data = await response.json(); } catch { throw new Error(`The service returned a non-JSON response (${response.status}). Check that the local backend is running.`); }
    if (!response.ok) {
      const detail = data.detail;
      const message = Array.isArray(detail) ? detail.map((item) => `${(item.loc || []).filter((v) => v !== "body").join(".")}: ${item.msg || "Invalid value"}`).join("; ") : typeof detail === "object" && detail !== null ? `${detail.message || "Request failed"}${detail.code ? ` (${detail.code})` : ""}` : detail || `Request failed (${response.status})`;
      throw new Error(message);
    }
    return data;
  }
  const post = (path, body) => api(path, { method: "POST", ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  function setView(view) {
    state.view = view;
    document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${view}`; });
    document.querySelectorAll("[data-view]").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === view);
      if (button.dataset.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    $("breadcrumb").textContent = { predict: "Check a claim", backtest: "Historical evaluation", history: "Run history", about: "Experiment guide" }[view];
    $("run-panel").hidden = !state.run || ["history", "about"].includes(view) || (state.run.kind === "backtest" ? view !== "backtest" : view !== "predict");
    clearError();
    if (view === "history") loadHistory();
  }
  function updateScheme() {
    const labels = Object.values(fallbackSchemes.claim);
    $("scheme-guide").innerHTML = labels.map((label) => `<span class="scheme-chip">${escape(label)}</span>`).join("");
    $("scheme-guide").setAttribute("aria-label", "Supported, contradicted, or insufficient visual evidence. Equal maximum drawdowns contradict the claim.");
  }
  async function loadConfig() {
    try {
      state.config = await api("/api/config");
      const mode = state.config.mode;
      $("mode-badge").textContent = mode === "demo" ? "Synthetic demo" : mode === "live" ? "Live · Foundry" : "Unconfigured";
      $("mode-badge").className = `badge ${mode === "demo" ? "demo" : "neutral"}`;
      $("mode-notice").className = `notice ${mode === "live" ? "live" : ""}`;
      $("mode-notice").textContent = mode === "demo" ? "SYNTHETIC DEMO — Scores are generated for demonstration. They are NOT Foundry judgments and do not measure Microsoft-Decision-1.1 performance." : mode === "live" ? "LIVE EXPERIMENT — Verify visible chart evidence with Microsoft-Decision-1.1. No future prices are predicted." : "INFERENCE NOT CONFIGURED — Configure the local backend to enable claim checks and historical evaluation. Existing runs remain inspectable.";
      $("latest-session").textContent = state.config.latest || "Not reported";
      if (state.config.latest) $("backtest-start").max = state.config.latest;
      if (state.config.demo_start) $("backtest-start").value = state.config.demo_start;
      const enabled = ["demo", "live"].includes(mode);
      $("predict-submit").disabled = !enabled;
      $("preview-submit").disabled = !enabled;
    } catch (error) {
      $("mode-badge").textContent = "Service unavailable";
      $("mode-notice").textContent = "The local service is unavailable. No demo data or judgments have been substituted. Reload after starting the backend.";
      showError(error);
    }
  }
  function formBody(form, backtest = false) {
    const data = new FormData(form);
    const result = { scheme: data.get("scheme") };
    for (const name of ["ticker", "comparison"]) {
      const symbol = String(data.get(name) ?? "").trim().toUpperCase();
      if (!/^[A-Z][A-Z0-9]{0,9}(?:[-.][A-Z0-9]{1,4})?$/.test(symbol)) throw new Error(`Enter a valid ${name === "ticker" ? "target" : "comparison"} ticker without spaces or slashes. Provider punctuation, such as BRK-B, is preserved.`);
      form.elements.namedItem(name).value = symbol;
      result[name] = symbol;
    }
    if (backtest) {
      result.start = data.get("start");
      result.count = Number(data.get("count"));
      if (!Number.isInteger(result.count) || result.count < 1 || result.count > 252) throw new Error("Choose between 1 and 252 trading sessions.");
    }
    return result;
  }
  async function startPrediction(event) {
    event.preventDefault(); clearError();
    const button = $("predict-submit"); button.disabled = true; button.textContent = "Queuing claim check…";
    try {
      const run = await post("/api/predictions", formBody(event.target));
      await openRun(run.id);
    } catch (error) { showError(error); }
    finally { button.disabled = !["demo", "live"].includes(state.config?.mode); button.innerHTML = 'Check claim <span aria-hidden="true">↗</span>'; }
  }
  function invalidatePreview() { state.preview = null; $("preview").hidden = true; }
  async function previewBacktest(event) {
    event.preventDefault(); clearError(); invalidatePreview();
    const button = $("preview-submit"); button.disabled = true; button.textContent = "Resolving sessions…";
    try {
      const body = formBody(event.target, true);
      const fingerprint = JSON.stringify(body);
      const preview = await post("/api/backtests/preview", body);
      if (fingerprint !== JSON.stringify(formBody(event.target, true))) return;
      state.preview = { body, data: preview };
      $("preview").innerHTML = `<h3>Review before running</h3><div class="meta-grid">${metadata([
        ["Target", preview.ticker], ["Comparison", preview.comparison], ["Requested start", preview.requested_start], ["First chart ends", preview.effective_start], ["Last chart ends", preview.last_origin], ["Requested charts", number(preview.count, 0)], ["Eligible charts", number(preview.eligible, 0)], ["Maximum inference attempts", number(preview.max_attempts, 0)], ["Cached results", number(preview.cached, 0)],
      ])}</div><p class="muted">The experiment remains fixed for this run. Cache reuse and data eligibility may reduce new calls. Cost is not estimated: provider pricing is not reported.</p><button id="confirm-backtest" class="button primary" ${preview.eligible === 0 ? "disabled" : ""}>Confirm & start ${escape(number(preview.count, 0))}-session run →</button>`;
      $("preview").hidden = false;
      $("confirm-backtest").addEventListener("click", confirmBacktest);
      announce("Historical evaluation preview ready. Review the resolved interval and confirm the run.");
    } catch (error) { showError(error); }
    finally { button.disabled = !["demo", "live"].includes(state.config?.mode); button.innerHTML = 'Preview evaluation <span aria-hidden="true">→</span>'; }
  }
  async function confirmBacktest() {
    if (!state.preview) return;
    const snapshot = state.preview;
    const button = $("confirm-backtest"); button.disabled = true; clearError();
    try {
      const run = await post("/api/backtests", snapshot.body);
      invalidatePreview();
      await openRun(run.id);
    } catch (error) { showError(error); if (button.isConnected) button.disabled = false; }
  }
  async function openRun(id) {
    clearError(); clearTimeout(state.timer);
    const generation = ++state.generation;
    state.rows = [];
    try {
      const run = await api(runPath(id));
      if (generation !== state.generation) return;
      state.run = run;
      setView(run.kind === "backtest" ? "backtest" : "predict");
      renderRun(); $("run-panel").scrollIntoView({ behavior: "auto", block: "start" });
      await refreshRows(generation);
      if (!terminal.has(run.status)) schedulePoll(generation);
    } catch (error) { if (generation === state.generation) showError(error); }
  }
  function schedulePoll(generation, delay = 1600) {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => poll(generation), delay);
  }
  async function poll(generation) {
    if (generation !== state.generation || !state.run) return;
    try {
      const run = await api(runPath(state.run.id));
      if (generation !== state.generation) return;
      const oldStatus = state.run.status;
      state.run = run;
      renderRun();
      await refreshRows(generation);
      if (oldStatus !== run.status) announce(`Run ${humanize(run.status)}. ${run.stage || ""}`);
      if (!terminal.has(run.status)) schedulePoll(generation, document.hidden ? 5000 : 1600);
    } catch (error) {
      if (generation !== state.generation) return;
      showError(new Error(`Unable to refresh this run: ${error.message} Retrying in 5 seconds.`));
      schedulePoll(generation, 5000);
    }
  }
  async function refreshRows(generation) {
    try {
      const rows = await api(`${runPath(state.run.id)}/rows?offset=0&limit=252`);
      if (generation !== state.generation) return;
      if (!Array.isArray(rows)) throw new Error("The service returned an invalid session list.");
      state.rows = rows; renderRows(); renderPrediction();
    } catch (error) {
      if (generation === state.generation) {
        $("rows-content").innerHTML = `<p class="row-error">Unable to load session evidence: ${escape(error.message)}</p><button id="retry-rows" class="button secondary">Retry session evidence</button>`;
        $("retry-rows").addEventListener("click", () => refreshRows(generation));
      }
    }
  }
  function renderRun() {
    const focusedId = $("run-panel").contains(document.activeElement) ? document.activeElement.id : null;
    const run = state.run;
    $("run-panel").hidden = !((run.kind === "backtest" && state.view === "backtest") || (run.kind === "prediction" && state.view === "predict"));
    $("run-kicker").textContent = `${run.mode === "demo" ? "SYNTHETIC DEMO · NOT A FOUNDRY JUDGMENT" : run.mode === "live" ? "LIVE FOUNDRY EXPERIMENT" : "MODE NOT REPORTED"} · ${isClaim(run) ? run.kind === "backtest" ? "HISTORICAL EVALUATION" : "CLAIM CHECK" : "LEGACY FORECAST"}`;
    $("run-title").textContent = `${pairLabel(run)} · ${isDrawdown(run) ? "Smaller maximum drawdown" : isClaim(run) ? "Target versus SPY" : run.scheme === "binary" ? "Up / down or unchanged" : "Five return bands"}`;
    $("run-status").className = `badge ${["queued", "running", "complete", "failed", "cancelled", "interrupted"].includes(run.status) ? run.status : "neutral"}`;
    $("run-status").textContent = humanize(run.status);
    $("run-meta").innerHTML = metadata([["Run ID", run.id], ["Requested start", run.requested_start], [isClaim(run) ? "First chart ends" : "Effective origin", run.effective_start], [isClaim(run) ? "Last chart ends" : "Final target", isClaim(run) ? run.last_origin : run.last_target]]);
    const warnings = [run.mode === "demo" ? "Synthetic scores only. These are NOT Foundry judgments; metrics measure the demo generator, not Microsoft-Decision-1.1." : null, !isDrawdown(run) ? `Legacy ${isClaim(run) ? "SPY-outperformance" : "forecast"} experiment: original labels and metrics are preserved. It cannot resume under drawdown-comparison rules.` : null, run.warning, run.error].filter(Boolean);
    $("run-warning").hidden = !warnings.length;
    $("run-warning").textContent = warnings.join(" ");
    const completed = typeof run.completed === "number" ? run.completed : 0;
    const count = run.count || (run.kind === "prediction" ? 1 : 0);
    $("run-stage").textContent = run.stage ? humanize(run.stage) : humanize(run.status);
    $("run-progress-text").textContent = count ? `${number(completed, 0)} / ${number(count, 0)} origins processed` : "Waiting for session count";
    $("progress").max = count || 1; $("progress").value = Math.max(0, Math.min(completed, count || 1));
    $("progress").setAttribute("aria-valuetext", $("run-progress-text").textContent);
    const active = !terminal.has(run.status);
    $("run-actions").innerHTML = `<a class="button secondary" href="${runPath(run.id)}/manifest.json" download>↓ JSON manifest</a><a class="button secondary" href="${runPath(run.id)}/predictions.csv" download>↓ Results CSV</a>${run.kind === "backtest" && active ? '<button id="cancel-run" class="button danger">Cancel run</button>' : ""}${isDrawdown(run) && run.kind === "backtest" && ["cancelled", "interrupted"].includes(run.status) ? '<button id="resume-run" class="button primary">Resume run →</button>' : ""}<button id="refresh-run" class="button secondary">Refresh status</button>`;
    $("cancel-run")?.addEventListener("click", () => controlRun("cancel"));
    $("resume-run")?.addEventListener("click", () => controlRun("resume"));
    $("refresh-run").addEventListener("click", () => { clearTimeout(state.timer); poll(state.generation); });
    renderReport(run.report); renderPrediction();
    if (focusedId) $(focusedId)?.focus({ preventScroll: true });
  }
  async function controlRun(action) {
    const id = state.run.id;
    const generation = state.generation;
    const button = $(`${action}-run`); if (button) button.disabled = true;
    clearError();
    try {
      await post(`/api/backtests/${encodeURIComponent(id)}/${action}`);
      if (generation !== state.generation) return;
      announce(action === "cancel" ? "Cancellation requested. In-flight work may finish." : "Run resume requested.");
      clearTimeout(state.timer); await poll(generation);
    } catch (error) { showError(error); if (button?.isConnected) button.disabled = false; }
  }
  function table(headers, rows, caption = "") {
    return `<div class="table-wrap"><table>${caption ? `<caption>${escape(caption)}</caption>` : ""}<thead><tr>${headers.map((value) => `<th scope="col">${escape(value)}</th>`).join("")}</tr></thead><tbody>${rows.join("") || `<tr><td colspan="${headers.length}">No observations available.</td></tr>`}</tbody></table></div>`;
  }
  function renderReport(report) {
    if (!report) { $("run-report").innerHTML = state.run.kind === "prediction" ? '<div id="prediction-summary"></div>' : '<p class="muted">Metrics will appear as evaluated outcomes become available. Missing metrics are not zero.</p>'; return; }
    const counts = report.counts || {};
    const claim = isClaim();
    const metrics = [["Accuracy", percent(report.accuracy), claim ? "Correct / answered" : "Correct / evaluated"], ["Balanced accuracy", percent(report.balanced_accuracy), claim ? "Mean answered-class recall" : "Mean class recall"], ["Macro F1", number(report.macro_f1), claim ? "Answered claims only" : "Equal weight per class"], [claim ? "Answer coverage" : "Coverage", percent(report.coverage), claim ? "Answered / evaluated" : "Reported evaluation coverage"], ["Log loss", number(report.log_loss), claim ? "All judgments, including abstentions" : "Lower is better"], ["Brier score", number(report.brier), "Sum over classes; lower is better"], ["Median latency", report.latency_ms?.median == null ? "—" : `${number(report.latency_ms.median, 0)} ms`, "Per inference"], ["P95 latency", report.latency_ms?.p95 == null ? "—" : `${number(report.latency_ms.p95, 0)} ms`, "Per inference"]];
    if (claim) metrics.push(
      ["Evaluation coverage", percent(report.evaluation_coverage), "Evaluated / requested"],
      ["Effective coverage", percent(report.effective_coverage), "Answered / requested"],
      ["Overall accuracy", percent(report.overall_accuracy), "Abstentions count as incorrect"],
    );
    const ids = [...new Set([...Object.keys(classes()), ...Object.keys(report.confusion || {}), ...Object.keys(report.classes || {}), ...Object.values(report.confusion || {}).flatMap((values) => Object.keys(values || {}))])];
    const confusion = report.confusion || {};
    const maxCell = Math.max(1, ...Object.values(confusion).flatMap((row) => Object.values(row || {}).filter((value) => typeof value === "number")));
    const actualIds = claim ? Object.keys(confusion) : ids;
    const confusionHtml = `<div class="table-wrap"><table><caption>Rows: actual · Columns: selected · Counts, including abstentions</caption><thead><tr><th scope="col">Actual ↓ / Selected →</th>${ids.map((id) => `<th scope="col">${escape(classLabel(id))}</th>`).join("")}</tr></thead><tbody>${actualIds.map((actual) => `<tr><th scope="row">${escape(classLabel(actual))}</th>${ids.map((predicted) => { const value = confusion[actual]?.[predicted]; const intensity = typeof value === "number" && Number.isFinite(value) ? Math.min(.25, Math.max(0, value / maxCell * .25)) : 0; return `<td class="confusion-cell" style="--intensity:${intensity}">${number(value, 0)}</td>`; }).join("")}</tr>`).join("")}</tbody></table></div>`;
    const classRows = Object.entries(report.classes || {}).map(([id, value]) => `<tr><th scope="row">${escape(classLabel(id))}</th><td>${percent(value.precision)}</td><td>${percent(value.recall)}</td><td>${number(value.f1)}</td><td>${number(value.support, 0)}</td><td>${number(value.predicted, 0)}</td></tr>`);
    const baselines = Object.entries(report.baselines || {}).map(([name, value]) => `<tr><th scope="row">${escape(humanize(name))}</th><td>${percent(value.accuracy)}</td><td>${number(value.count, 0)}</td><td>${typeof value.delta === "number" ? `${value.delta > 0 ? "+" : ""}${(value.delta * 100).toFixed(1)} pp` : "—"}</td><td>${number(value.log_loss)}</td><td>${number(value.brier)}</td><td>${percent(value.matched_model_accuracy)}</td><td>${number(value.matched_model_log_loss)}</td><td>${number(value.matched_model_brier)}</td></tr>`);
    const calibration = (report.calibration || []).map((bin) => `<tr><td>${escape(Array.isArray(bin.range) ? bin.range.map(percent).join(" – ") : bin.range ?? "—")}</td><td>${number(bin.count, 0)}</td><td>${percent(bin.mean_confidence)}</td><td>${percent(bin.accuracy)}</td></tr>`);
    const months = Object.entries(report.months || {}).sort(([a], [b]) => a.localeCompare(b)).map(([month, value]) => `<tr><th scope="row">${escape(month)}</th><td>${number(value.count, 0)}</td><td>${percent(value.accuracy)}</td></tr>`);
    const margins = Object.entries(report.margin_slices || {}).map(([range, value]) => `<tr><th scope="row">${escape(range)}</th><td>${number(value.count, 0)}</td><td>${number(value.answered, 0)}</td><td>${number(value.abstained, 0)}</td><td>${percent(value.coverage)}</td><td>${percent(value.accuracy)}</td></tr>`);
    const countKeys = ["requested", "eligible", "scored", "evaluated", ...(claim ? ["answered", "abstained"] : []), "failed", "skipped", "pending"];
    $("run-report").innerHTML = `<div id="prediction-summary"></div>
      ${report.exploratory ? '<div class="notice warning">Exploratory results: fewer than 30 evaluated charts. Overlapping windows are dependent observations.</div>' : ""}
      <div class="counts">${countKeys.map((key) => `<span>${escape(humanize(key))} <strong>${number(counts[key], 0)}</strong></span>`).join("")}</div>
      <div class="metric-grid">${metrics.map(([label, value, note]) => `<div class="metric"><div class="metric-label">${escape(label)}</div><div class="metric-value">${escape(value)}</div><div class="metric-footnote">${escape(note)}</div></div>`).join("")}</div>
      <p class="field-help">${claim ? `Accuracy and class metrics exclude abstentions; probability losses and calibration include them. Coverage exposes abstentions and failures. Macro F1 includes: ${(report.macro_f1_classes || []).map((id) => escape(classLabel(id))).join(", ") || "no supported truth classes"}. Balanced accuracy requires both truth classes among answered rows.` : "Legacy classification metrics use scored-and-labeled outcomes."} “—” means unavailable, not zero. Overlapping 30-session windows create dependent observations.</p>
      <div class="report-block"><h3>Baseline comparison</h3>${table(["Baseline", "Accuracy", "Count", "Model − baseline", "Log loss", "Brier", "Matched model accuracy", "Matched model log loss", "Matched model Brier"], baselines, claim ? "Classification compares the same answered rows; historical majority uses 252 earlier windows. Probability metrics use the matched subset. Uniform is a three-choice probability reference on all evaluated rows." : "Compare identical scored-and-labeled origins; a delta is not a significance test.")}</div>
      ${claim ? `<div class="report-block"><h3>Performance by ${isDrawdown() ? "drawdown" : "outperformance"} margin</h3>${table(["Absolute margin", "Evaluated", "Answered", "Abstained", "Answer coverage", "Accuracy"], margins, `${isDrawdown() ? "Margins are Comparison minus Target maximum drawdowns, in percentage points. Positive supports the claim." : "Margins are target minus SPY percentage-point returns."} Near-ties remain true or false; these bins do not redefine ground truth.`)}</div>` : ""}
      <div class="report-block"><h3>Confusion matrix</h3>${confusionHtml}</div>
      <div class="report-grid"><div class="report-block"><h3>Performance by class</h3>${table(["Class", "Precision", "Recall", "F1", "Actual (answered)", "Selected"], classRows)}</div>
      <div class="report-block"><h3>Probability calibration</h3>${table(["Confidence bin", "Count", "Mean confidence", "Accuracy"], calibration, claim ? "All evaluated judgments; abstentions do not match either truth class." : "Legacy forecast confidence is not an established probability of future market moves.")}</div>
      <div class="report-block"><h3>Monthly stability</h3>${table(["Month", "Evaluated", claim ? "Answered accuracy" : "Accuracy"], months)}</div>
      <div class="report-block"><h3>Execution accounting</h3><div class="meta-grid">${metadata([["Inference attempts", number(report.attempts, 0)], ["Cache hits", number(report.cache_hits, 0)]])}</div>${jsonDetails("Metered usage, when returned", report.usage)}<p class="muted">No forecasting, return, or profit claims are inferred from visual classification.</p></div></div>`;
  }
  function probabilities(row) {
    const probabilities = row.probabilities;
    if (!probabilities || typeof probabilities !== "object") return '<p class="muted">No scored probability distribution is available for this session.</p>';
    const ids = [...new Set([...Object.keys(classes()), ...Object.keys(probabilities)])];
    return table(["Outcome", "Probability", "Distribution"], ids.map((id) => {
      const value = probabilities[id];
      const width = typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(100, value * 100)) : 0;
      return `<tr><th scope="row">${escape(classLabel(id))}${row.choice === id ? " · selected" : ""}</th><td class="numeric">${percent(value)}</td><td><div class="prob-track" aria-hidden="true"><div class="prob-fill" style="--probability:${width}%"></div></div></td></tr>`;
    }));
  }
  function renderPrediction() {
    const container = $("prediction-summary");
    if (!container || state.run?.kind !== "prediction") return;
    const row = state.rows[0];
    if (!row) { container.innerHTML = ""; return; }
    container.innerHTML = `<div class="result-summary"><p>${state.run.mode === "demo" ? "SYNTHETIC DEMO JUDGMENT" : "SELECTED JUDGMENT"}</p><h3>${escape(classLabel(row.choice))}</h3><p>${isClaim() ? `Chart ${escape(row.window_start)} through ${escape(row.origin)} · Truth: ${escape(classLabel(row.actual))} · Margin: ${escape(points(marginValue(row)))}` : `Legacy origin ${escape(row.origin)} → target ${escape(row.target)}`} · ${escape(humanize(row.status))}</p></div><div class="report-grid"><div><h3>Complete probability distribution</h3>${probabilities(row)}</div><div><h3>Exact submitted image</h3>${row.image_sha ? imageMarkup(row) : '<p class="muted">The chart is not available yet. No illustration is substituted for submitted evidence.</p>'}<button class="button secondary" id="inspect-prediction">Open decision inspector →</button></div></div>`;
    bindImages(container);
    $("inspect-prediction").addEventListener("click", () => inspectRow(0));
  }
  function renderRows() {
    const focusedSort = $("rows-content").contains(document.activeElement) ? document.activeElement.dataset.sort : null;
    const focusedOrigin = $("rows-content").contains(document.activeElement) ? document.activeElement.dataset.inspect : null;
    const filter = $("row-status").value;
    const indexed = state.rows.map((row, index) => ({ row, index })).filter(({ row }) => filter === "all" || row.status === filter);
    indexed.sort((a, b) => {
      let left = a.row[state.sort]; let right = b.row[state.sort];
      if (left == null) return right == null ? 0 : 1;
      if (right == null) return -1;
      return (typeof left === "number" && typeof right === "number" ? left - right : String(left).localeCompare(String(right))) * state.direction;
    });
    if (!indexed.length) { $("rows-content").innerHTML = '<div class="empty">No sessions to show yet for this status.</div>'; return; }
    const claim = isClaim();
    const headers = [["origin", claim ? "Chart ends" : "Origin"], [claim ? "window_start" : "target", claim ? "Window starts" : "Target"], ["status", "Status"], ["choice", "Selected judgment"], ["actual", "Calculated truth"], [claim ? isDrawdown() ? "drawdown_margin" : "outperformance_margin" : "realized_return", claim ? isDrawdown() ? "Drawdown: Comparison − Target" : "Target − SPY" : "Price return"], ["latency_ms", "Latency"], ["attempts", "Attempts"]];
    $("rows-content").innerHTML = `<div class="table-wrap"><table><caption>${indexed.length} of ${state.rows.length} loaded origins · up to 252 per run · activate a column to sort</caption><thead><tr>${headers.map(([key, label]) => `<th scope="col" ${state.sort === key ? `aria-sort="${state.direction === 1 ? "ascending" : "descending"}"` : ""}><button data-sort="${key}">${label} ${state.sort === key ? state.direction === 1 ? "↑" : "↓" : "↕"}</button></th>`).join("")}<th scope="col">Cache / error</th></tr></thead><tbody>${indexed.map(({ row, index }) => `<tr><td><button class="text-button" data-inspect="${index}" aria-label="Inspect origin ${escape(row.origin)}">${escape(row.origin)}</button></td><td>${escape(claim ? row.window_start : row.target)}</td><td>${statusBadge(row.status)}</td><td>${escape(classLabel(row.choice))}</td><td>${escape(classLabel(row.actual))}</td><td class="numeric">${claim ? points(marginValue(row)) : percent(row.realized_return)}</td><td class="numeric">${row.latency_ms == null ? "—" : `${number(row.latency_ms, 0)} ms`}</td><td class="numeric">${number(row.attempts, 0)}</td><td class="${row.error ? "row-error" : ""}">${escape(row.error || (row.cache_hit === true ? "Cache hit" : row.cache_hit === false ? "Not cached" : "—"))}</td></tr>`).join("")}</tbody></table></div>`;
    $("rows-content").querySelectorAll("[data-sort]").forEach((button) => button.addEventListener("click", () => { const sort = button.dataset.sort; state.direction = state.sort === sort ? -state.direction : 1; state.sort = sort; renderRows(); }));
    $("rows-content").querySelectorAll("[data-inspect]").forEach((button) => button.addEventListener("click", () => inspectRow(Number(button.dataset.inspect))));
    if (focusedSort) $("rows-content").querySelector(`[data-sort="${focusedSort}"]`)?.focus({ preventScroll: true });
    if (focusedOrigin != null) $("rows-content").querySelector(`[data-inspect="${focusedOrigin}"]`)?.focus({ preventScroll: true });
  }
  function imageMarkup(row) {
    const url = `${runPath(state.run.id)}/charts/${encodeURIComponent(row.origin)}.png`;
    return `<figure style="margin:0"><img class="evidence-image" src="${url}" alt="Exact submitted 30-session price chart for ${escape(pairLabel())}, ending ${escape(row.origin)}; ${isDrawdown() ? "anonymous Target and Comparison price paths" : "stock and DIA, QQQ, SPY comparison"} with historical 52-week extremes."><figcaption class="evidence-caption">Stored submitted PNG · <a href="${url}" download>Download chart image ↓</a></figcaption></figure>`;
  }
  function bindImages(container) {
    container.querySelectorAll(".evidence-image").forEach((image) => image.addEventListener("error", () => {
      const replacement = document.createElement("p"); replacement.className = "image-placeholder"; replacement.textContent = "Stored chart image is unavailable. No substitute image is shown."; image.replaceWith(replacement);
    }, { once: true }));
  }
  function jsonDetails(title, value) {
    return `<details class="json-details"><summary>${escape(title)}</summary><pre>${escape(value == null ? "Not reported by the backend." : JSON.stringify(value, null, 2))}</pre></details>`;
  }
  function inspectRow(index) {
    const row = state.rows[index]; if (!row) return;
    $("inspector-title").textContent = isClaim() ? `${pairLabel()} · chart ending ${row.origin}` : `${state.run.ticker} · legacy ${row.origin} → ${row.target}`;
    const evidence = isDrawdown() ? [
      ["Window starts", row.window_start], ["Chart ends", row.origin],
      ["Target maximum drawdown", percent(row.target_drawdown)],
      ["Comparison maximum drawdown", percent(row.comparison_drawdown)],
      ["Drawdown margin (Comparison − Target)", points(row.drawdown_margin)],
    ] : isClaim() ? [
      ["Window starts", row.window_start], ["Chart ends", row.origin],
      ["Target price return", percent(row.target_return)], ["SPY price return", percent(row.spy_return)],
      ["Outperformance margin", points(row.outperformance_margin)],
    ] : [["Realized price return", percent(row.realized_return)]];
    $("inspector-content").innerHTML = `${state.run.mode === "demo" ? '<div class="notice warning">SYNTHETIC DEMO — This is NOT a Foundry judgment.</div>' : ""}${isClaim() ? `<p><strong>${escape(state.run.claim)}</strong></p>` : ""}<div class="meta-grid">${metadata([["Status", humanize(row.status)], ["Selected judgment", classLabel(row.choice)], ["Calculated truth", classLabel(row.actual)], ...evidence, ["Model", row.model], ["Latency", row.latency_ms == null ? "—" : `${number(row.latency_ms, 0)} ms`], ["Cache hit", row.cache_hit == null ? "Not reported" : row.cache_hit ? "Yes" : "No"], ["Attempts", number(row.attempts, 0)]])}</div>${row.error ? `<p class="notice error">${escape(row.error)}</p>` : ""}${row.image_sha ? imageMarkup(row) : '<p class="muted">No submitted chart hash was recorded for this session.</p>'}<p class="evidence-caption">This image is the only market evidence submitted to the model. Numerical measurements, margins, and calculated truth are evaluator-only data, not model input.</p><h3>Outcome probabilities</h3>${probabilities(row)}<p class="field-help">Selected outcomes are shown as returned; no argmax substitution or model rationale is invented.</p><h3>Image SHA</h3><p class="hash">${escape(row.image_sha)}</p>${jsonDetails("Fixed question, criteria & sanitized request", row.request)}${jsonDetails("Model response & usage, when returned", row.response)}${jsonDetails("Per-origin baselines", row.baselines)}${jsonDetails("Run metadata, experiment version & data freshness, when reported", state.run)}`;
    bindImages($("inspector-content"));
    if (!$("inspector").open) $("inspector").showModal();
  }
  async function loadHistory() {
    const generation = ++state.historyGeneration;
    $("history-content").innerHTML = '<div class="empty">Loading recent runs…</div>';
    try {
      const runs = await api("/api/runs");
      if (generation !== state.historyGeneration) return;
      if (!Array.isArray(runs)) throw new Error("The service returned an invalid run list.");
      if (!runs.length) { $("history-content").innerHTML = '<div class="empty"><h3>A clean research slate.</h3>Your claim checks and historical evaluations will appear here. Start with a ticker to create your first experiment.</div>'; return; }
      $("history-content").innerHTML = table(["Ticker pair / run", "Experiment", "Task", "Mode", "Status", "Chart ends / origin", "Actions"], runs.map((run, index) => `<tr><td><strong>${escape(pairLabel(run))}</strong><br><span class="field-help">${escape(run.id)}</span></td><td>${isClaim(run) ? `${isDrawdown(run) ? "" : "Legacy "}${run.kind === "backtest" ? "Historical evaluation" : "Claim check"}` : `Legacy ${escape(humanize(run.kind))}`}</td><td>${isDrawdown(run) ? "Smaller maximum drawdown" : isClaim(run) ? "Target outperformed SPY" : run.scheme === "binary" ? "Binary forecast" : "Five-band forecast"}</td><td>${escape(run.mode === "demo" ? "Synthetic demo" : humanize(run.mode))}</td><td>${statusBadge(run.status)}</td><td>${escape(run.effective_start || run.requested_start)}</td><td><div class="history-actions"><button class="text-button" data-open="${index}">Open</button><button class="text-button danger-text" data-delete="${index}">Delete</button></div></td></tr>`));
      $("history-content").querySelectorAll("[data-open]").forEach((button) => button.addEventListener("click", () => openRun(runs[Number(button.dataset.open)].id)));
      $("history-content").querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", () => deleteRun(runs[Number(button.dataset.delete)], button)));
    } catch (error) {
      if (generation === state.historyGeneration) $("history-content").innerHTML = `<div class="empty">Could not load history: ${escape(error.message)}<br>Use Refresh history to try again.</div>`;
    }
  }
  async function deleteRun(run, button) {
    if (!window.confirm(`Permanently delete ${run.ticker} ${run.kind} run ${run.id} and its local artifacts? This cannot be undone.`)) return;
    button.disabled = true; clearError();
    try {
      const response = await fetch(runPath(run.id), { method: "DELETE", headers: { Accept: "application/json" } });
      if (!response.ok) {
        let payload = {}; try { payload = await response.json(); } catch { /* HTTP status remains useful without a JSON error. */ }
        throw new Error(payload.detail?.message || (typeof payload.detail === "string" ? payload.detail : `Could not delete the run (${response.status}). Active runs may need cancellation first.`));
      }
      if (state.run?.id === run.id) { clearTimeout(state.timer); state.generation++; state.run = null; state.rows = []; $("run-panel").hidden = true; }
      announce("Run deleted."); loadHistory();
    } catch (error) { showError(error); if (button.isConnected) button.disabled = false; }
  }
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $("predict-scheme").addEventListener("change", updateScheme);
  $("predict-form").addEventListener("submit", startPrediction);
  $("backtest-form").addEventListener("submit", previewBacktest);
  $("backtest-form").addEventListener("input", invalidatePreview);
  $("backtest-form").addEventListener("change", invalidatePreview);
  $("refresh-history").addEventListener("click", loadHistory);
  $("row-status").addEventListener("change", renderRows);
  $("close-inspector").addEventListener("click", () => $("inspector").close());
  $("inspector").addEventListener("click", (event) => {
    if (event.target !== $("inspector")) return;
    const bounds = $("inspector").getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) $("inspector").close();
  });
  window.addEventListener("pagehide", () => { clearTimeout(state.timer); state.generation++; });
  window.addEventListener("pageshow", (event) => { if (event.persisted && state.run && !terminal.has(state.run.status)) schedulePoll(state.generation); });
  updateScheme(); loadConfig();
})();
