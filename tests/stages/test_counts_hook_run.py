# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An expected-count hook end to end (ADR 0016 section 3): plan, apply and verify a bundle.

A stub hook, registered by a fixture and unregistered after each test, gives each account an
expected number of invoices from its status: a closed account one, any other three. The invoices of
``tests/fixtures/exact-counts`` then take their accounts from the layout that follows it. What is
pinned, on kernel 0.2.0:

* **The allotment.** Open accounts hold three times the invoices of closed ones, and the counts
  total the invoices.
* **No hook, no change.** A hook that returns None for every relationship leaves the plan, the
  digests and the bytes as they were.
* **The plan.** The member moves the digest of the invoice emit and of no other.
* **Two paths, one answer.** The column path's rows equal the per-row path's, the shares of a
  ``--shard`` joined are the whole run's bytes, and ``--scale`` below 1 plans and applies.
* **Jobs.** The ``--jobs`` workers start from a server that does not hold a test's stub, so the
  multi-process case is covered by slices: each slice of the table is cut in a process that works
  the layout out afresh (``tests/generator/test_expected_count_hook.py``).
* **Verify** passes on the run.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import relations
from flux_engine.generator.counts import (
    expected_count_hooks,
    register_expected_count_hook,
)
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify
from tests.generator.test_rules_over_links import _by_both_paths

from .test_counts_run import ACCOUNTS, INVOICES, _planned_emits, _shares, _table, bundle_with

pytestmark = pytest.mark.integration

MEMBER = {"expected": "by-status"}


class ByStatus:
    """One expected invoice for a closed account and three for any other."""

    def __init__(self, member: Optional[Dict[str, Any]] = MEMBER) -> None:
        self.declared = member
        self.rows: List[int] = []

    def member(
        self, namespace: Any, child: GeneratorPlan, relationship: str
    ) -> Optional[Dict[str, Any]]:
        asked = (child.expose.expose_id, relationship)
        return self.declared if asked == ("invoice", "account") else None

    def expected(
        self, parent: GeneratorPlan, row: int, final_row: Tuple[Any, ...], relationship: str
    ) -> Fraction:
        names = [column_plan.column.name for column_plan in parent.columns]
        self.rows.append(row)
        return Fraction(1 if final_row[names.index("status")] == "closed" else 3)


def _test_hooks() -> Tuple[Any, ...]:
    """The registered hooks other than the engine's own exposure hook (ADR 0025 section 4)."""
    from flux_engine.generator.exposure import EXPOSURE_HOOK

    return tuple(hook for hook in expected_count_hooks() if hook is not EXPOSURE_HOOK)


@pytest.fixture(autouse=True)
def fresh_layouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relations, "_LAYOUTS", {})


@pytest.fixture
def hook() -> Iterator[Callable[..., ByStatus]]:
    undo: List[Callable[[], None]] = []

    def add(member: Optional[Dict[str, Any]] = MEMBER) -> ByStatus:
        stub = ByStatus(member)
        undo.append(register_expected_count_hook(stub))
        return stub

    yield add
    for unregister in undo:
        unregister()
    assert _test_hooks() == ()


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """The exact-counts bundle with no counts entry: only the hook counts the invoices."""
    return bundle_with(tmp_path, "hooked", counted=False)


def _counts(outcome: Any) -> Dict[Any, int]:
    held = {row["account_id"]: 0 for row in _table(outcome, "account")}
    for row in _table(outcome, "invoice"):
        held[row["account_id"]] += 1
    return held


def test_open_accounts_hold_three_times_the_invoices_of_closed_ones(
    bundle: Path, hook: Callable[..., ByStatus]
) -> None:
    stub = hook()
    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    held = _counts(outcome)
    status = {row["account_id"]: row["status"] for row in _table(outcome, "account")}
    closed = [held[key] for key in held if status[key] == "closed"]
    opened = [held[key] for key in held if status[key] != "closed"]
    assert closed and opened and sum(held.values()) == INVOICES
    # 1200 invoices over weights 1 and 3: each count is the proportion or one off it.
    unit = Fraction(INVOICES, len(closed) + 3 * len(opened))
    assert all(abs(count - unit) < 1 for count in closed)
    assert all(abs(count - 3 * unit) < 1 for count in opened)
    assert stub.rows == list(range(ACCOUNTS))


def test_a_hook_that_returns_none_leaves_the_plan_the_digests_and_the_bytes(
    bundle: Path, hook: Callable[..., ByStatus], tmp_path: Path
) -> None:
    before = _planned_emits(bundle, tmp_path / "before")
    plain = pipeline.execute(bundle, kernel=KERNEL_V2)
    hook(member=None)
    after = _planned_emits(bundle, tmp_path / "after")
    silent = pipeline.execute(bundle, kernel=KERNEL_V2)
    assert after == before
    for expose_id in ("product", "account", "invoice", "invoice_line"):
        assert silent.emit(expose_id).writer.stream_bytes() == (
            plain.emit(expose_id).writer.stream_bytes()
        ), expose_id


def test_the_member_moves_the_digest_of_the_invoice_emit_and_of_no_other(
    bundle: Path, hook: Callable[..., ByStatus], tmp_path: Path
) -> None:
    plain = _planned_emits(bundle, tmp_path / "plain")
    hook()
    hooked = _planned_emits(bundle, tmp_path / "hooked")
    assert hooked["invoice"]["generator"]["generatorDigest"] != (
        plain["invoice"]["generator"]["generatorDigest"]
    )
    for expose_id in ("product", "account", "invoice_line"):
        assert hooked[expose_id] == plain[expose_id], expose_id


def test_plan_apply_and_verify_pass(
    bundle: Path, hook: Callable[..., ByStatus], tmp_path: Path
) -> None:
    hook()
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state, kernel="0.2.0", stage=False)
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, state_dir=state, run_id=run_id)
    assert applied.success, applied.stderr
    written = RunLayout.for_state_dir(state, run_id).emit_path("invoice")
    assert len(written.read_text("utf-8").splitlines()) == INVOICES
    verified = verify(bundle, run_id=run_id, state_dir=state, allow_declared_baseline_drift=True)
    assert verified.success, verified.stderr


def test_the_column_path_equals_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, bundle: Path, hook: Callable[..., ByStatus]
) -> None:
    hook()
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(bundle, kernel=KERNEL_V2)
    )
    for expose_id in ("invoice", "invoice_line", "account"):
        assert by_columns.emit(expose_id).rows == by_rows.emit(expose_id).rows, expose_id
    assert taken and all(taken)


@pytest.mark.parametrize("count", [3, 7])
def test_the_shares_joined_are_the_whole_runs_bytes(
    bundle: Path, hook: Callable[..., ByStatus], count: int
) -> None:
    hook()
    whole, parts = _shares(bundle, count)
    for expose_id in ("invoice", "invoice_line"):
        joined = b"".join(part.emit(expose_id).writer.stream_bytes() for part in parts)
        assert joined == whole.emit(expose_id).writer.stream_bytes(), expose_id


@pytest.mark.parametrize("scale", ["0.5", "0.37"])
def test_a_scaled_run_plans_and_applies_and_the_counts_total_the_scaled_rows(
    bundle: Path, hook: Callable[..., ByStatus], tmp_path: Path, scale: str
) -> None:
    hook()
    planned = plan(bundle, state_dir=tmp_path / "state", kernel="0.2.0", scale=scale, stage=False)
    assert planned.success, planned.stderr
    applied = apply(bundle, run_id=str(planned.artifacts["run_id"]), state_dir=tmp_path / "state")
    assert applied.success, applied.stderr
    scaled = pipeline.execute(bundle, kernel=KERNEL_V2, scale=Decimal(scale))
    assert sum(_counts(scaled).values()) == int(INVOICES * Decimal(scale))


def test_a_hook_that_gives_a_negative_count_is_refused_when_the_layout_is_worked_out(
    bundle: Path, hook: Callable[..., ByStatus]
) -> None:
    stub = hook()
    stub.expected = lambda *args: Fraction(-1)  # type: ignore[method-assign]
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403" and "below zero" in str(caught.value)


def test_kernel_0_1_0_refuses_the_member_and_names_the_kernel(
    bundle: Path, hook: Callable[..., ByStatus]
) -> None:
    hook()
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)
