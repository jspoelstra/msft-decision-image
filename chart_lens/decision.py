"""Decision API contract, validation, and offline demonstration scorer."""

from base64 import b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from math import isfinite
from random import Random
from time import perf_counter, sleep
from typing import Protocol

import httpx
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential

from chart_lens.domain import CLAIM, CRITERIA, SCHEMES, ExperimentError, Scheme

PROMPT: str = (
    f"Judge this claim using only the supplied chart: {CLAIM} "
    "The upper panel gives the target's regular-session daily "
    "closing prices for the last 30 completed trading sessions, and its trailing 52-week "
    "intraday high and low as of session 30. The lower panel shows Target and Comparison, "
    "each as percentage change from its own first displayed close on a common axis. "
    "For each series, maximum drawdown is the largest percentage loss from an earlier "
    "closing-price peak to a subsequent closing-price trough, both within sessions 1-30. "
    "Use the full path, not just the final endpoints or a low that occurred before a peak. "
    "Measure loss relative to that earlier peak, not as the percentage-point drop on "
    "the relative-performance axis. Prices are split-adjusted, not dividend-adjusted. "
    "Drawdown is nonnegative; a nondecreasing series has zero drawdown. Equal maximum "
    "drawdowns contradict the strict smaller-drawdown claim. Select "
    "insufficient visual evidence if the image cannot resolve the comparison reliably. "
    "Do not infer missing facts or predict future prices."
)
API_PATH: str = "/providers/microsoft/v1/systemone"
SCOPE: str = "https://cognitiveservices.azure.com/.default"


@dataclass(frozen=True)
class Decision:
    """Validated selection, probabilities, and safe service metadata."""

    choice: str
    probabilities: dict[str, float]
    normalized: dict[str, float]
    model: str
    usage: dict[str, int | float]
    latency_ms: float
    attempts: int


class DecisionError(ExperimentError):
    """A scoring failure with the number of attempted inference requests."""

    def __init__(self, code: str, message: str, attempts: int) -> None:
        super().__init__(code, message, 502)
        self.attempts = attempts


class Scorer(Protocol):
    """Scores only chart bytes and a fixed experiment scheme."""

    def score(self, image: bytes, scheme: Scheme, attempts: int) -> Decision:
        """Classify a chart with the supplied attempt budget."""


def payload(deployment: str, scheme: Scheme, image: bytes | None = None) -> dict:
    """Create the wire request, or its sanitized inspector representation."""
    return {
        "model": deployment,
        "state": "Verify the claim using only this chart: image:chart.png",
        "assets": {
            "chart.png": {
                "type": "image",
                "format": "png",
                "data_base64": b64encode(image).decode("ascii") if image else "<stored PNG>",
            }
        },
        "questions": {
            "claim": {"type": "choice", "instructions": PROMPT, "criteria": CRITERIA[scheme]}
        },
    }


def validate(raw: object, scheme: Scheme, latency_ms: float, attempts: int) -> Decision:
    """Reject malformed or inconsistent answers without guessing probabilities."""
    error = DecisionError(
        "invalid_decision", "Foundry returned an invalid choice distribution.", attempts
    )
    if not isinstance(raw, dict):
        raise error
    answers, model = raw.get("answers"), raw.get("model")
    if not isinstance(answers, dict) or set(answers) != {"claim"}:
        raise error
    if not isinstance(model, str) or not model:
        raise error
    answer = answers["claim"]
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise error
    choice, probabilities = answer.get("choice"), answer.get("probabilities")
    if not isinstance(choice, str) or choice not in SCHEMES[scheme]:
        raise error
    if not isinstance(probabilities, dict) or probabilities.keys() != SCHEMES[scheme].keys():
        raise error
    if any(type(x) not in (int, float) or not 0 <= x <= 1 for x in probabilities.values()):
        raise error
    total = sum(probabilities.values())
    if abs(total - 1) > 0.02 or max(probabilities.values()) - probabilities[choice] > 1e-8:
        raise error
    usage = raw.get("usage")
    return Decision(
        choice,
        probabilities,
        {key: value / total for key, value in probabilities.items()},
        model,
        {key: val for key, val in usage.items() if type(val) in (int, float) and isfinite(val)}
        if isinstance(usage, dict)
        else {},
        round(latency_ms, 2),
        attempts,
    )


class DemoScorer:
    """Deterministic mock probabilities, not a prediction model."""

    def score(self, image: bytes, scheme: Scheme, attempts: int) -> Decision:
        """Produce a synthetic distribution from the chart hash and scheme."""
        rng = Random(sha256(image + scheme.encode()).digest())
        weights = {key: rng.uniform(0.3, 1.8) for key in SCHEMES[scheme]}
        total = sum(weights.values())
        return validate(
            {
                "model": "synthetic-hash-v1",
                "answers": {
                    "claim": {
                        "type": "choice",
                        "probabilities": {k: v / total for k, v in weights.items()},
                        "choice": max(weights, key=weights.__getitem__),
                    }
                },
            },
            scheme,
            0,
            0,
        )


def _delay(retry_after: str, attempt: int) -> float | None:
    try:
        seconds = float(retry_after)
    except ValueError:
        try:
            when = parsedate_to_datetime(retry_after)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            seconds = 2 ** (attempt - 1)
    return max(0, seconds) if isfinite(seconds) and seconds <= 30 else None


class FoundryScorer:
    """Direct Entra-authenticated Microsoft Decision API client."""

    def __init__(self, endpoint: str, deployment: str) -> None:
        self.endpoint = endpoint
        self.deployment = deployment
        self.credential = DefaultAzureCredential()
        self.client = httpx.Client(timeout=45, follow_redirects=False)

    def close(self) -> None:
        """Release HTTP and credential resources."""
        self.client.close()
        self.credential.close()

    def score(self, image: bytes, scheme: Scheme, attempts: int) -> Decision:
        """Send an image-only request with bounded retries on transient failures."""
        started = perf_counter()
        try:
            token = self.credential.get_token(SCOPE).token
        except AzureError:
            raise DecisionError(
                "foundry_auth", "Azure sign-in or Foundry inference RBAC failed.", 0
            ) from None
        request = payload(self.deployment, scheme, image)
        for attempt in range(1, attempts + 1):
            wait = _delay("", attempt)
            try:
                result = self.client.post(
                    self.endpoint + API_PATH,
                    json=request,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.RequestError:
                error = DecisionError(
                    "foundry_timeout",
                    "Could not reach Foundry within the request timeout.",
                    attempt,
                )
            else:
                if result.is_success:
                    try:
                        raw = result.json()
                    except ValueError:
                        raise DecisionError(
                            "invalid_decision", "Foundry did not return valid JSON.", attempt
                        ) from None
                    return validate(raw, scheme, (perf_counter() - started) * 1000, attempt)
                status = result.status_code
                error = DecisionError(
                    f"foundry_{status}", f"Foundry returned HTTP {status}.", attempt
                )
                if status not in (429, 500, 502, 503, 504):
                    raise error
                wait = _delay(result.headers.get("Retry-After", ""), attempt)
            if attempt == attempts or wait is None:
                raise error
            sleep(wait)
        raise DecisionError("attempt_budget", "The inference attempt budget is exhausted.", 0)
