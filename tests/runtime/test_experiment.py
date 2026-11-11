# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Experiments: LRM arm sizes, the re-rooted shuffle, and the readout port."""

from __future__ import annotations

import sys
import types
from decimal import Decimal
from typing import Dict

import pytest

from flux_engine.core.lrm import largest_remainder
from flux_engine.core.seedtree import derive_root
from flux_engine.core.trace import TraceOp
from flux_engine.runtime.experiment import (
    ArmCounts,
    TwoProportionReadout,
    arm_counts,
    assign_variants,
    compile_experiments,
    default_readout,
    read_out,
)
from flux_engine.world import TraceLog

from .conftest import Staged, requires_example

pytestmark = [pytest.mark.integration, requires_example]


def experiment(staged: Staged):
    compiled = compile_experiments(staged.cast)
    assert compiled, "the cast holds no Experiment"
    return compiled[0]


def test_the_compiled_experiment_mirrors_the_document(staged: Staged) -> None:
    compiled = experiment(staged)
    spec = staged.cast.document(compiled.experiment_id)["spec"]
    assert [v.name for v in compiled.variants] == [v["name"] for v in spec["variants"]]
    assert compiled.assignment_seed == spec["assignment"]["seed"]
    assert compiled.control == next(v["name"] for v in spec["variants"] if v.get("isControl"))


def test_arm_sizes_are_the_largest_remainder_of_the_declared_weights(
    staged: Staged,
) -> None:
    compiled = experiment(staged)
    eligible = list(staged.store.ordinals)
    assigned = assign_variants(
        experiment=compiled, eligible=eligible, run_root=staged.seed_tree.root, trace=TraceLog()
    )
    expected = largest_remainder(len(eligible), dict(compiled.weights))
    counts: Dict[str, int] = {}
    for variant in assigned.values():
        counts[variant] = counts.get(variant, 0) + 1
    assert counts == {name: expected[name] for name in counts}
    assert sum(counts.values()) == len(eligible)


def test_every_eligible_subject_gets_exactly_one_arm(staged: Staged) -> None:
    compiled = experiment(staged)
    eligible = list(staged.store.ordinals)[:120]
    assigned = assign_variants(
        experiment=compiled, eligible=eligible, run_root=staged.seed_tree.root, trace=TraceLog()
    )
    assert sorted(assigned) == sorted(eligible)


def test_the_assignment_is_rooted_at_the_experiment_seed_not_the_run_seed(
    staged: Staged,
) -> None:
    """§3: "rooted at ``Experiment.spec.assignment.seed``, **not** at ``root``".

    So re-seeding the *run* must leave the arms exactly where they were, while
    changing the contract digest -- which every root is derived from -- must move
    them. Both halves are asserted: a stream that ignored the run seed by
    ignoring the root entirely would pass the first and fail the second.
    """
    compiled = experiment(staged)
    assert compiled.assignment_seed is not None
    eligible = list(staged.store.ordinals)[:120]

    def assign(seam: bytes, seed: int):
        return assign_variants(
            experiment=compiled,
            eligible=eligible,
            run_root=derive_root(seam, seed),
            trace=TraceLog(),
        )

    assert assign(b"H", 42) == assign(b"H", 43)
    assert assign(b"H", 42) != assign(b"H2", 42)


def test_changing_the_experiment_seed_moves_the_assignment(staged: Staged) -> None:
    compiled = experiment(staged)
    other = type(compiled)(
        experiment_id=compiled.experiment_id,
        variants=compiled.variants,
        assignment_seed=(compiled.assignment_seed or 0) + 1,
        stream_ordinal=compiled.stream_ordinal,
    )
    eligible = list(staged.store.ordinals)[:120]
    first = assign_variants(
        experiment=compiled, eligible=eligible, run_root=staged.seed_tree.root, trace=TraceLog()
    )
    second = assign_variants(
        experiment=other, eligible=eligible, run_root=staged.seed_tree.root, trace=TraceLog()
    )
    assert first != second


def test_an_experiment_with_no_seed_uses_the_runs_own_root(staged: Staged) -> None:
    compiled = experiment(staged)
    unseeded = type(compiled)(
        experiment_id=compiled.experiment_id,
        variants=compiled.variants,
        assignment_seed=None,
        stream_ordinal=compiled.stream_ordinal,
    )
    assert unseeded.root(staged.seed_tree.root) is staged.seed_tree.root


def test_the_eligible_order_does_not_change_the_assignment(staged: Staged) -> None:
    """The permutation is over "canonically sorted eligible ordinals"."""
    compiled = experiment(staged)
    eligible = list(staged.store.ordinals)[:120]
    forward = assign_variants(
        experiment=compiled, eligible=eligible, run_root=staged.seed_tree.root, trace=TraceLog()
    )
    backward = assign_variants(
        experiment=compiled,
        eligible=list(reversed(eligible)),
        run_root=staged.seed_tree.root,
        trace=TraceLog(),
    )
    assert forward == backward


def test_one_assign_op_is_traced_per_subject_in_ordinal_order(staged: Staged) -> None:
    compiled = experiment(staged)
    eligible = list(staged.store.ordinals)[:60]
    log = TraceLog()
    assigned = assign_variants(
        experiment=compiled, eligible=eligible, run_root=staged.seed_tree.root, trace=log
    )
    traced = log.of(TraceOp.ASSIGN)
    assert [record["ordinal"] for record in traced] == sorted(eligible)
    assert [record["variant"] for record in traced] == [assigned[o] for o in sorted(eligible)]


def test_an_empty_eligible_set_assigns_and_traces_nothing(staged: Staged) -> None:
    log = TraceLog()
    assert (
        assign_variants(
            experiment=experiment(staged), eligible=[], run_root=staged.seed_tree.root, trace=log
        )
        == {}
    )
    assert len(log) == 0


# --- counts and readout ------------------------------------------------------


def test_arm_counts_fold_an_assignment_and_a_success_set() -> None:
    assignments = {0: "a", 1: "a", 2: "b"}
    counts = arm_counts(assignments, frozenset({0, 2}))
    assert counts == (
        ArmCounts(variant="a", trials=2, successes=1),
        ArmCounts(variant="b", trials=1, successes=1),
    )


def test_an_arm_with_more_successes_than_trials_is_refused() -> None:
    with pytest.raises(ValueError):
        ArmCounts(variant="a", trials=1, successes=2)


def test_an_empty_arm_has_a_zero_rate_and_no_division() -> None:
    assert ArmCounts(variant="a", trials=0, successes=0).rate == Decimal(0)


def test_the_fallback_readout_is_exact_decimal_everywhere() -> None:
    row = TwoProportionReadout().compare(
        ArmCounts("control", 100, 20), ArmCounts("treatment", 100, 30), confidence=Decimal("0.95")
    )
    for value in (row.rate, row.control_rate, row.difference, row.interval_low, row.interval_high):
        assert isinstance(value, Decimal)
    assert row.difference == Decimal("0.1")
    assert row.interval_low < row.difference < row.interval_high


def test_a_wider_confidence_gives_a_wider_interval() -> None:
    engine = TwoProportionReadout()
    narrow = engine.compare(
        ArmCounts("c", 200, 40), ArmCounts("t", 200, 60), confidence=Decimal("0.80")
    )
    wide = engine.compare(
        ArmCounts("c", 200, 40), ArmCounts("t", 200, 60), confidence=Decimal("0.99")
    )
    assert (wide.interval_high - wide.interval_low) > (narrow.interval_high - narrow.interval_low)


def test_a_zero_variance_comparison_gives_a_point_interval() -> None:
    row = TwoProportionReadout().compare(
        ArmCounts("c", 10, 0), ArmCounts("t", 10, 0), confidence=Decimal("0.95")
    )
    assert row.interval_low == row.interval_high == Decimal(0)


def test_the_readout_compares_every_arm_against_the_named_control() -> None:
    counts = (
        ArmCounts("control", 50, 10),
        ArmCounts("fast", 50, 15),
        ArmCounts("slow", 50, 12),
    )
    rows = read_out(counts, control="control", statistics=TwoProportionReadout())
    assert [row.variant for row in rows] == ["fast", "slow"]
    assert {row.control for row in rows} == {"control"}


def test_the_readout_defaults_the_control_to_the_first_arm_by_name() -> None:
    rows = read_out((ArmCounts("b", 10, 1), ArmCounts("a", 10, 2)))
    assert [row.control for row in rows] == ["a"]


def test_an_empty_readout_is_empty() -> None:
    assert read_out(()) == ()


def test_the_readout_records_which_engine_produced_it() -> None:
    engine = default_readout()
    rows = read_out((ArmCounts("a", 10, 1), ArmCounts("b", 10, 2)), statistics=engine)
    assert {row.engine for row in rows} == {engine.engine}


def test_the_default_readout_is_the_exact_one_even_when_gbstats_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed ``gbstats`` used to switch the readout to float arithmetic.

    The engine no longer reads ``gbstats``, so what happens to be installed
    beside it cannot change a run's readout. A stand-in for the module that the
    old code imported makes the check work on a machine without it.
    """
    stand_ins = {
        "gbstats": types.ModuleType("gbstats"),
        "gbstats.frequentist": types.ModuleType("gbstats.frequentist"),
        "gbstats.frequentist.tests": types.ModuleType("gbstats.frequentist.tests"),
    }
    stand_ins["gbstats.frequentist.tests"].__dict__["TwoSidedTTest"] = object
    for name, module in stand_ins.items():
        monkeypatch.setitem(sys.modules, name, module)
    readout = default_readout()
    assert isinstance(readout, TwoProportionReadout)
    assert readout.engine == "two-proportion"


def test_an_experiment_that_flags_no_control_reports_none(staged: Staged) -> None:
    compiled = experiment(staged)
    uncontrolled = type(compiled)(
        experiment_id=compiled.experiment_id,
        variants=tuple(type(v)(**{**vars(v), "is_control": False}) for v in compiled.variants),
        assignment_seed=compiled.assignment_seed,
        stream_ordinal=compiled.stream_ordinal,
    )
    assert uncontrolled.control is None


def test_an_arm_with_no_trials_contributes_no_variance() -> None:
    """The ``trials > 0`` guard is what keeps an empty arm from dividing by zero."""
    row = TwoProportionReadout().compare(
        ArmCounts("c", 0, 0), ArmCounts("t", 40, 10), confidence=Decimal("0.95")
    )
    assert row.control_rate == Decimal(0)
    assert row.interval_low < row.interval_high
