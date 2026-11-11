# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim expr eval``: a worked example that runs, and an error that helps.

The example shipped ``tenure_months > 12``; the attribute is spelled
``tenureMonths``, so the one worked example the expression language ships
exited 1 against the project's own example bundle. The FLX-6004 that
followed listed none of the sixteen attributes the engine was holding in
memory and offered no closest match -- and because ``--values`` was
parsed *after* the expression was checked, a JSON typo in ``--values``
was reported as an error in the expression.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from flux_engine.cli import _build_parser, main

pytestmark = pytest.mark.unit

FIXTURE_BUNDLE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "telco-fidelity-small"


def _eval(source: str, values: str, capsys) -> tuple[int, dict]:
    exit_code = main(
        ["expr", "eval", source, "--bundle", str(FIXTURE_BUNDLE), "--values", values, "--json"]
    )
    return exit_code, json.loads(capsys.readouterr().out)


def _expr_eval_example() -> str:
    parser = _build_parser()
    sub = parser._subparsers._group_actions[0]  # type: ignore[union-attr]
    eval_parser = sub.choices["expr"]._subparsers._group_actions[0].choices["eval"]
    return str(eval_parser.examples[0].one_line())


def test_the_shipped_example_actually_runs(capsys) -> None:
    example = _expr_eval_example()
    match = re.search(r'fluxsim expr eval "([^"]+)" --bundle \S+ --values \'(.+)\'\s*$', example)
    assert match, example
    exit_code, envelope = _eval(match.group(1), match.group(2), capsys)
    assert exit_code == 0, envelope
    assert envelope["artifacts"]["result"] is True


def test_an_unknown_attribute_lists_the_scope_and_suggests_the_closest(capsys) -> None:
    exit_code, envelope = _eval("tenure_months > 12", '{"tenure_months": 24}', capsys)
    assert exit_code == 1
    message = envelope["error"]["message"]
    assert envelope["error"]["code"] == "FLX-6004"
    assert "did you mean 'tenureMonths'?" in message
    for attribute in ("arpu", "lifecycle", "traits.monthlySpend", "worldview.riskTolerance"):
        assert attribute in message, attribute


def test_malformed_values_is_diagnosed_as_values_not_as_the_expression(capsys) -> None:
    exit_code, envelope = _eval("data.amount > 12", "not-json", capsys)
    assert exit_code == 1
    message = envelope["error"]["message"]
    assert "--values" in message
    assert "FLX-6004" not in message, "a JSON typo is not an expression error"


def test_a_real_expression_error_still_wins_when_values_are_fine(capsys) -> None:
    """The negative control: moving the JSON parse first must not swallow FLX-6004."""
    exit_code, envelope = _eval("nope > 12", '{"nope": 1}', capsys)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-6004"
