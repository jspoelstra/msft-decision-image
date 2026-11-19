"""Verify the documented image-only API contract without Azure access."""

from base64 import b64decode
from types import SimpleNamespace

import httpx
import pytest

from chart_lens.decision import API_PATH, DecisionError, FoundryScorer, validate


def _wire(probabilities, choice="supported"):
    return {
        "model": "microsoft-decision-1.1",
        "answers": {
            "claim": {"type": "choice", "choice": choice, "probabilities": probabilities},
        },
    }


@pytest.mark.parametrize(
    "probabilities",
    [
        {"supported": True, "contradicted": 0.0, "insufficient": 0.0},
        {"supported": float("nan"), "contradicted": 0.3, "insufficient": 0.0},
        {"supported": 0.2, "contradicted": 0.8, "insufficient": 0.0},
        {"supported": 0.9, "contradicted": 0.3, "insufficient": 0.0},
        {"supported": 1.0, "other": 0.0, "insufficient": 0.0},
    ],
)
def test_given_invalid_choice_when_validated_then_not_scored(probabilities):
    # Act / Assert
    with pytest.raises(DecisionError, match="invalid choice"):
        validate(_wire(probabilities), "claim", 10, 1)


def test_given_throttled_image_request_when_retried_then_exact_png_is_sent(mocker):
    # Arrange
    scorer = FoundryScorer("https://example.services.ai.azure.com", "decision")
    mocker.patch.object(
        scorer.credential, "get_token", return_value=SimpleNamespace(token="fixture")
    )
    post = mocker.patch.object(
        scorer.client,
        "post",
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(
                200, json=_wire({"supported": 0.59, "contradicted": 0.3, "insufficient": 0.1})
            ),
        ],
    )
    image = b"\x89PNG\r\n\x1a\nfixture"

    # Act
    try:
        answer = scorer.score(image, "claim", 3)
    finally:
        scorer.close()

    # Assert
    (url,) = post.call_args.args
    request = post.call_args.kwargs["json"]
    assert url.endswith(API_PATH)
    assert set(request) == {"model", "state", "assets", "questions"}
    assert request["state"] == "Verify the claim using only this chart: image:chart.png"
    assert set(request["questions"]) == {"claim"}
    assert set(request["questions"]["claim"]["criteria"]) == {
        "supported",
        "contradicted",
        "insufficient",
    }
    assert b64decode(request["assets"]["chart.png"]["data_base64"]) == image
    assert answer.attempts == 2 and sum(answer.normalized.values()) == pytest.approx(1)


def test_given_unreadable_chart_when_model_abstains_then_distribution_is_preserved():
    # Arrange
    raw = _wire({"supported": 0.1, "contradicted": 0.2, "insufficient": 0.7}, "insufficient")

    # Act
    answer = validate(raw, "claim", 12, 1)

    # Assert
    assert answer.choice == "insufficient"
    assert answer.normalized == raw["answers"]["claim"]["probabilities"]
