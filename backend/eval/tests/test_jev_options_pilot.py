"""Offline contract checks before the one-shot paid architecture comparison."""

import json

import pytest

from eval.jev_options_pilot import (
    GLOBAL_INPUT_CAP,
    MAX_NEW_CALLS,
    PRIOR_ACCOUNTED_INPUT,
    cases_from_fixtures,
    parse_answer,
    planned_requests,
)


def test_options_manifest_reserves_every_request_inside_the_approved_cap() -> None:
    cases, hashes = cases_from_fixtures()
    planned = planned_requests(cases)
    assert len(hashes) == 2
    assert len(cases) == 20
    assert len(planned) == MAX_NEW_CALLS == 76
    assert sum(variant.startswith("baseline") for _, variant, _ in planned) == 32
    assert sum(variant.startswith("packed") for _, variant, _ in planned) == 22
    assert sum(variant.startswith("choice") for _, variant, _ in planned) == 22
    assert PRIOR_ACCOUNTED_INPUT + sum(len(raw) + 4096 for _, _, raw in planned) <= GLOBAL_INPUT_CAP


def test_packed_and_choice_answers_require_complete_typed_outputs() -> None:
    common = {"model": "jev-1.13.0", "usage": {"input_tokens": 100, "output_tokens": 8}}
    packed = {
        **common,
        "answers": {f"p{index}": {"type": "noul", "noul": 0.2 * index} for index in range(4)},
    }
    values, _, _ = parse_answer(json.dumps(packed).encode(), "packed")
    assert values == [0.0, 0.2, 0.4, 0.6000000000000001]
    choice = {
        **common,
        "answers": {
            "best": {
                "type": "choice",
                "choice": "p2",
                "probabilities": {"p0": 0.1, "p1": 0.1, "p2": 0.6, "p3": 0.1, "none": 0.1},
            }
        },
    }
    assert parse_answer(json.dumps(choice).encode(), "choice")[0] == [0.1, 0.1, 0.6, 0.1]
    invalid_choice = {
        **common,
        "answers": {
            "best": {
                "type": "choice",
                "choice": "p2",
                "probabilities": {"p0": 0.1, "p1": 0.1, "p2": 0.6, "p3": 0.1, "none": 0.0},
            }
        },
    }
    with pytest.raises(ValueError, match="probability mass"):
        parse_answer(json.dumps(invalid_choice).encode(), "choice")
