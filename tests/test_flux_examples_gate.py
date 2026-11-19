# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The sibling-FLUX gate fails under CI instead of skipping.

CI checked out only ``flux-engine``, so thirteen ``skipif`` gates keyed on
``../FLUX`` silently removed 226 tests from a green run -- the whole
real-binary acceptance success path among them -- while the >=70% coverage
gate still passed at 95%, because coverage is measured over what ran.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.flux_examples import (
    FLUX_REPO,
    REQUIRED_ENV_VAR,
    examples_are_required,
    is_flux_examples_skip_reason,
    skip_or_fail_without_flux_examples,
)

pytestmark = pytest.mark.unit


def test_a_missing_example_fails_when_the_flag_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRED_ENV_VAR, "1")
    assert examples_are_required()
    with pytest.raises(BaseException) as raised:
        skip_or_fail_without_flux_examples("no-such-example")
    assert REQUIRED_ENV_VAR in str(raised.value)


def test_a_missing_example_only_skips_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REQUIRED_ENV_VAR, raising=False)
    assert not examples_are_required()


@pytest.mark.parametrize("value", ["", "0", "false", "no"], ids=repr)
def test_falsey_spellings_do_not_arm_the_gate(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(REQUIRED_ENV_VAR, value)
    assert not examples_are_required()


# --- the matcher the reporting-boundary hook keys off ----------------------
#
# The guarantee is enforced once, at the reporting boundary, over the skip
# *reason* -- so the set of reasons the matcher recognises is the set of
# gates that cannot silently vanish. Every hand-rolled spelling in this
# suite is listed here; a ninth gate phrased some other way is a hole, and
# this is where it shows up.


@pytest.mark.parametrize(
    "reason",
    [
        f"sibling FLUX spec checkout not found at {FLUX_REPO}",
        f"sibling FLUX repo example not found at {FLUX_REPO}/examples/telco-payment-recovery",
        "the FLUX spec repo is not checked out beside this one",
        "the FLUX spec repo's telco example is not checked out beside this repo",
        f"the FLUX example bundle is not checked out at {FLUX_REPO}",
        f"the FLUX specification repository is not checked out at {FLUX_REPO}",
    ],
)
def test_every_hand_rolled_gate_reason_is_recognised(reason: str) -> None:
    assert is_flux_examples_skip_reason(reason)


@pytest.mark.parametrize(
    "reason",
    ["openlineage is not installed", "requires posix", "no numpy on this platform"],
)
def test_an_unrelated_skip_is_left_alone(reason: str) -> None:
    assert not is_flux_examples_skip_reason(reason)


# --- the reporting-boundary hook itself ------------------------------------
#
# ``tests/flux_examples.py`` names this hook as where the guarantee is
# enforced, and for a while it named a hook that was not there: the eight
# hand-rolled gates still skipped under ``FLUX_EXAMPLES_REQUIRED=1``, and
# nothing said so. These drive the hook directly, so the documented
# mechanism cannot go missing again without a red test.


def _drive(report: SimpleNamespace) -> SimpleNamespace:
    """Run the ``wrapper=True`` hook over one report and return what it produced."""
    from tests.conftest import pytest_runtest_makereport

    generator = pytest_runtest_makereport(item=None, call=None)  # type: ignore[arg-type]
    next(generator)
    try:
        generator.send(report)
    except StopIteration as stop:
        return stop.value  # type: ignore[no-any-return]
    raise AssertionError("the hook did not stop after one send")


def _skip_report(reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        skipped=True, outcome="skipped", longrepr=("x.py", 1, f"Skipped: {reason}")
    )


def test_the_hook_turns_a_sibling_flux_skip_into_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REQUIRED_ENV_VAR, "1")
    report = _drive(_skip_report(f"the FLUX example bundle is not checked out at {FLUX_REPO}"))
    assert report.outcome == "failed"
    assert REQUIRED_ENV_VAR in str(report.longrepr)


def test_the_hook_leaves_an_unrelated_skip_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRED_ENV_VAR, "1")
    report = _drive(_skip_report("openlineage is not installed"))
    assert report.outcome == "skipped"


def test_the_hook_does_nothing_while_the_flag_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control: a developer without the sibling checkout still skips."""
    monkeypatch.delenv(REQUIRED_ENV_VAR, raising=False)
    report = _drive(_skip_report(f"the FLUX example bundle is not checked out at {FLUX_REPO}"))
    assert report.outcome == "skipped"


def test_an_expected_failure_is_not_a_vanished_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """``wasxfail`` rides on a skipped report; an xfail is not a missing checkout."""
    monkeypatch.setenv(REQUIRED_ENV_VAR, "1")
    report = _skip_report(f"the FLUX example bundle is not checked out at {FLUX_REPO}")
    report.wasxfail = "expected to fail"
    assert _drive(report).outcome == "skipped"
