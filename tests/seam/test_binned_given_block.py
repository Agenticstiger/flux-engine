# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column drawn given a column drawn from bins (ADR 0036 decision 3): what the plan accepts.

The bundle is ``tests/fixtures/copula`` with ``balance`` given ``amount``'s bin. A bundle that
names no binned parent hashes as it always did; the declaration is part of the fidelity block, so
it moves both digests and the plan's ``G`` is the one ``ship`` recompiles. Kernel 0.1.0 refuses it
and names the kernel; anything else a binned parent or its child gets wrong is ``FLX-3402``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1
from flux_engine.seam.emit import seam_digests, seam_reusable_digest
from flux_engine.stages.pipeline import compile_declared_fidelity
from flux_engine.stages.validate import validate
from tests.seam.test_copula_block import (
    PLAIN_G,
    PLAIN_H,
    _digests,
    _plan,
    _refused,
    _resolved,
    bundle_with,
)
from tests.stages.test_binned_given_run import BALANCE, REGION, binned_bundle

pytestmark = pytest.mark.unit

ONE = (BALANCE,)

AMOUNT = (
    "            amount:\n"
    "              range: { min: '0', max: '1000' }\n"
    "              bins: ['0', '100', '400', '1000']\n"
    "              binWeights: ['0.5', '0.3', '0.2']\n"
)


def _with(tmp_path: Path, old: str, new: str, name: str = "edited") -> Path:
    bundle = binned_bundle(tmp_path, name, edits=ONE)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count(old) >= 1, old
    simulation.write_text(text.replace(old, new, 1), "utf-8")
    return bundle


def test_a_bundle_that_names_no_binned_parent_keeps_its_digests(tmp_path: Path) -> None:
    assert _digests(bundle_with(tmp_path, None)) == (PLAIN_H, PLAIN_G)


def test_a_binned_parent_moves_both_digests_and_an_edit_of_its_table_moves_them_again(
    tmp_path: Path,
) -> None:
    h, g = _digests(binned_bundle(tmp_path, "one", edits=ONE))
    assert h != PLAIN_H and g != PLAIN_G
    moved = _digests(_with(tmp_path, "['0.8', '0.1', '0.1']", "['0.7', '0.2', '0.1']", "moved"))
    assert moved[0] != h and moved[1] != g


def test_the_plans_g_is_the_g_that_ship_recompiles(tmp_path: Path) -> None:
    resolved = _resolved(binned_bundle(tmp_path, "g", edits=ONE))
    columns = resolved.compile().column_names
    planned = seam_digests(resolved.contract_document, resolved.emit, columns, resolved.extension)
    reusable = seam_reusable_digest(
        resolved.contract_document, resolved.emit, columns, extension=resolved.extension
    )
    compiled = compile_declared_fidelity(resolved)
    assert compiled is not None
    assert planned.g.value == reusable.value == compiled.g.value
    assert planned.h.value == compiled.h.value


def test_kernel_0_1_0_refuses_a_binned_parent_and_names_the_kernel(tmp_path: Path) -> None:
    refusal = _refused(binned_bundle(tmp_path, "k1", edits=ONE), KERNEL_V1)
    assert refusal.code.code == "FLX-3105"
    assert "kernel 0.2.0" in str(refusal) and "kernel 0.1.0" in str(refusal)
    assert "balance" in str(refusal) and "amount" in str(refusal)


def test_the_plan_accepts_a_binned_parent_and_validate_agrees(tmp_path: Path) -> None:
    bundle = binned_bundle(tmp_path, "ok", edits=ONE)
    assert _plan(bundle).column_plan("balance").statistics.parents == ("amount",)
    assert validate(bundle).exit_code == 0


def _refused_3402(bundle: Path, *words: str) -> FluxError:
    refusal = _refused(bundle)
    assert refusal.code.code == "FLX-3402", refusal
    for word in words:
        assert word in str(refusal), (word, str(refusal))
    return refusal


def test_every_bin_needs_an_entry_and_a_key_is_the_text_of_a_lower_edge(tmp_path: Path) -> None:
    _refused_3402(
        _with(tmp_path, "                '100': ['0.1', '0.8', '0.1']\n", "", "short"),
        "binWeightsGiven",
    )
    _refused_3402(
        _with(tmp_path, "'400': ['0.1', '0.1', '0.8']", "'1000': ['0.1', '0.1', '0.8']", "top"),
        "binWeightsGiven",
    )
    _refused_3402(_with(tmp_path, "'100': [", "'100.0': [", "spelled"), "binWeightsGiven")


def test_a_binned_parent_declares_no_null_fraction(tmp_path: Path) -> None:
    bundle = _with(tmp_path, AMOUNT, AMOUNT + "              nullFraction: '0.1'\n", "nullable")
    _refused_3402(bundle, "nullFraction", "amount")


def test_a_column_drawn_given_a_binned_parent_cannot_be_allocated_exactly(tmp_path: Path) -> None:
    exact = "              given: amount\n"
    bundle = _with(tmp_path, exact, exact + "              allocation: exact\n", "exact")
    _refused_3402(bundle, "allocation exact", "amount")
    both = _with(
        tmp_path,
        AMOUNT,
        AMOUNT + "              allocation: exact\n",
        "both",
    )
    simulation = both / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    simulation.write_text(
        text.replace(exact, exact + "              allocation: exact\n", 1), "utf-8"
    )
    _refused_3402(both, "allocation exact", "amount")


def test_a_key_or_a_column_a_rule_sets_is_not_a_binned_parent(tmp_path: Path) -> None:
    bundle = binned_bundle(tmp_path, "keyed", edits=ONE)
    assert _plan(bundle) is not None
    with pytest.raises(FluxError) as caught:
        _plan(bundle, related=frozenset({"amount"}))
    assert caught.value.code.code == "FLX-3402"
    assert "amount" in str(caught.value) and "key" in str(caught.value)


def test_a_copula_column_is_not_a_binned_parent_or_child(tmp_path: Path) -> None:
    copula = (
        "copula:\n  columns: [amount, opened_at]\n  rankCorrelations:\n    measure: kendall\n"
        "    pairs: [ { columns: [amount, opened_at], value: '0.5' } ]\n"
    )
    bundle = binned_bundle(tmp_path, "parent", edits=ONE)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    marker = "          properties:\n"
    block = "".join(f"          {line}\n" for line in copula.splitlines())
    simulation.write_text(text.replace(marker, block + marker, 1), "utf-8")
    _refused_3402(bundle, "amount", "another is drawn given")
    child = copula.replace("amount, opened_at]", "balance, opened_at]").replace(
        "[amount, opened_at]", "[balance, opened_at]"
    )
    other = binned_bundle(tmp_path, "child", edits=ONE)
    path = other / "simulation.flux.yml"
    block = "".join(f"          {line}\n" for line in child.splitlines())
    path.write_text(path.read_text("utf-8").replace(marker, block + marker, 1), "utf-8")
    _refused_3402(other, "balance", "drawn given another")


def test_a_chain_of_binned_columns_and_a_categorical_leaf_is_planned(tmp_path: Path) -> None:
    plan_: Any = _plan(binned_bundle(tmp_path, "chain", edits=(BALANCE, REGION)))
    assert plan_.column_plan("region").statistics.parents == ("balance",)
    assert plan_.column_plan("balance").statistics.parents == ("amount",)


def test_a_cycle_through_bins_is_refused(tmp_path: Path) -> None:
    cycle: Tuple[str, str] = (
        "            amount:\n              range: { min: '0', max: '1000' }\n"
        "              bins: ['0', '100', '400', '1000']\n              binWeights: ['0.5', '0.3', '0.2']\n",
        "            amount:\n              range: { min: '0', max: '1000' }\n"
        "              bins: ['0', '100', '400', '1000']\n              given: balance\n"
        "              binWeightsGiven:\n"
        "                '0': ['0.5', '0.3', '0.2']\n"
        "                '50': ['0.5', '0.3', '0.2']\n"
        "                '200': ['0.5', '0.3', '0.2']\n",
    )
    bundle = binned_bundle(tmp_path, "cycle", edits=(BALANCE, cycle))
    _refused_3402(bundle, "cycle")
