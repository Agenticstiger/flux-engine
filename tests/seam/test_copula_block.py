# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An emit's copula (ADR 0035 decision 4): declared in the fidelity block, bound into ``H`` and ``G``.

The bundle is ``tests/fixtures/copula``: one table of accounts with four number or timestamp
columns drawn from histograms. A bundle that declares no copula hashes as it always did; the key
lives in the fidelity block, so it moves both digests, and the plan's ``G`` and the one ``ship``
recomputes are the same function of it. What the declaration alone says is read with the columns
unknown, and what the emit's columns can hold is checked where they are known. Kernel 0.1.0
refuses the key and names the kernel.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator.copula import read_copula
from flux_engine.generator.rows import GENERATOR_VERSION_2, build_plan
from flux_engine.seam.copula import fidelity_without_copula
from flux_engine.seam.emit import seam_digests, seam_reusable_digest
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from flux_engine.stages import pipeline
from flux_engine.stages.pipeline import compile_declared_fidelity, refuse_copula_combinations
from flux_engine.stages.validate import validate

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "copula"
SIMULATION = "sim-ledger"
EMIT = "accounts"
MARKER = "          properties:\n"

#: ``H`` and ``G`` of the fixture's emit as it is written, with no copula: computed with the code
#: this change was made on, before it.
PLAIN_H = "sha256:c8e8b0666c8ae620af6b8294e8ebe9f2129bdde325d157b35847d239ce59086d"
PLAIN_G = "sha256:bc0da0be26187e415eec76ee968cd0eef42eccd68cddb1ba42fe5b1e30512f21"

PAIR = "{ columns: [amount, balance], value: '0.5' }"
COPULA = (
    "copula:\n"
    "  columns: [amount, balance, opened_at]\n"
    "  rankCorrelations:\n"
    "    measure: kendall\n"
    f"    pairs: [ {PAIR}, {{ columns: [amount, opened_at], value: '-0.25' }} ]\n"
)


def bundle_with(tmp_path: Path, copula: Optional[str], name: str = "bundle") -> Path:
    """A copy of the fixture whose emit's fidelity block declares *copula* (the text of the key)."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    if copula is not None:
        simulation = target / "simulation.flux.yml"
        text = simulation.read_text("utf-8")
        assert text.count(MARKER) == 1
        block = "".join(f"          {line}\n" for line in copula.splitlines())
        simulation.write_text(text.replace(MARKER, block + MARKER, 1), "utf-8")
    return target


def _resolved(bundle: Path) -> Any:
    (found,) = resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    return found


def _digests(bundle: Path) -> Tuple[str, str]:
    resolved = _resolved(bundle)
    digests = seam_digests(
        resolved.contract_document,
        resolved.emit,
        resolved.compile().column_names,
        resolved.extension,
    )
    return digests.h.value, digests.g.value


def test_a_bundle_that_declares_no_copula_keeps_its_digests(tmp_path: Path) -> None:
    assert _digests(bundle_with(tmp_path, None)) == (PLAIN_H, PLAIN_G)


def test_the_copula_moves_both_digests(tmp_path: Path) -> None:
    h, g = _digests(bundle_with(tmp_path, COPULA))
    assert h != PLAIN_H and g != PLAIN_G


@pytest.mark.parametrize(
    "edit",
    [
        ("kendall", "spearman"),
        ("'0.5'", "'0.4'"),
        ("[amount, balance, opened_at]", "[balance, amount, opened_at]"),
        ("[amount, balance], value", "[balance, amount], value"),
    ],
)
def test_an_edit_of_the_measure_a_value_or_an_order_moves_both_digests_again(
    tmp_path: Path, edit: Tuple[str, str]
) -> None:
    base = _digests(bundle_with(tmp_path, COPULA, "base"))
    old, new = edit
    assert COPULA.count(old) == 1
    moved = _digests(bundle_with(tmp_path, COPULA.replace(old, new), "moved"))
    assert moved[0] != base[0] and moved[1] != base[1]


def test_the_plans_g_is_the_g_that_ship_recompiles(tmp_path: Path) -> None:
    resolved = _resolved(bundle_with(tmp_path, COPULA))
    columns = resolved.compile().column_names
    planned = seam_digests(resolved.contract_document, resolved.emit, columns, resolved.extension)
    reusable = seam_reusable_digest(
        resolved.contract_document, resolved.emit, columns, extension=resolved.extension
    )
    compiled = compile_declared_fidelity(resolved)
    assert compiled is not None
    assert planned.g.value == reusable.value == compiled.g.value
    assert planned.h.value == compiled.h.value


def test_the_block_the_fidelity_check_reads_has_no_copula(tmp_path: Path) -> None:
    resolved = _resolved(bundle_with(tmp_path, COPULA))
    assert "copula" in resolved.fidelity
    assert "copula" not in resolved.written_fidelity
    assert fidelity_without_copula(resolved.fidelity) == resolved.written_fidelity
    assert fidelity_without_copula({"columns": {}}) == {"columns": {}}
    assert fidelity_without_copula(None) is None


def test_the_declaration_is_read_with_its_pairs_in_the_order_written(tmp_path: Path) -> None:
    declaration = read_copula(_resolved(bundle_with(tmp_path, COPULA)).fidelity)
    assert declaration is not None
    assert declaration.columns == ("amount", "balance", "opened_at")
    assert declaration.measure == "kendall"
    assert [(a, b, str(v)) for a, b, v in declaration.pairs] == [
        ("amount", "balance", "0.5"),
        ("amount", "opened_at", "-0.25"),
    ]
    assert read_copula(None) is None and read_copula({"columns": {}}) is None


def _refused(bundle: Path, kernel: Any = KERNEL_V2) -> FluxError:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=kernel)
    return caught.value


def _declared(columns: str, pairs: str = f"[ {PAIR} ]", measure: str = "kendall") -> str:
    return (
        f"copula:\n  columns: {columns}\n  rankCorrelations:\n    measure: {measure}\n"
        f"    pairs: {pairs}\n"
    )


NAMES_65 = "[" + ", ".join(f"c{i}" for i in range(65)) + "]"

DECLARATIONS: Dict[str, Tuple[str, str]] = {
    "a list": ("copula: [amount, balance]\n", "is a mapping"),
    "an extra key": (_declared("[amount, balance]") + "  extra: 1\n", "not one of"),
    "no rank correlations": ("copula:\n  columns: [amount, balance]\n", "no rankCorrelations"),
    "one column": (_declared("[amount]"), "2 to 64"),
    "sixty-five columns": (_declared(NAMES_65), "2 to 64"),
    "a repeated column": (_declared("[amount, amount]"), "each column once"),
    "a column that is not a list": (_declared("amount"), "list of column names"),
    "an unknown measure": (_declared("[amount, balance]", measure="pearson"), "kendall or"),
    "a pair of one column": (
        _declared("[amount, balance]", "[ { columns: [amount, amount], value: '0.1' } ]"),
        "two different columns",
    ),
    "a pair outside the copula": (
        _declared("[amount, balance]", "[ { columns: [amount, opened_at], value: '0.1' } ]"),
        "does not hold",
    ),
    "a pair listed twice in either order": (
        _declared(
            "[amount, balance]",
            "[ { columns: [amount, balance], value: '0.1' }, "
            "{ columns: [balance, amount], value: '0.2' } ]",
        ),
        "earlier pair",
    ),
    "a value above one": (
        _declared("[amount, balance]", "[ { columns: [amount, balance], value: '1.5' } ]"),
        "[-1, 1]",
    ),
    "a value that is not a decimal": (
        _declared("[amount, balance]", "[ { columns: [amount, balance], value: 'high' } ]"),
        "[-1, 1]",
    ),
    "a column the emit lacks": (_declared("[amount, missing]", "[]"), "not a column of the emit"),
    "an identifier": (_declared("[amount, acct_id]", "[]"), "not a number or timestamp"),
    "a categorical column": (_declared("[amount, region]", "[]"), "not a number or timestamp"),
}


@pytest.mark.parametrize("name", sorted(DECLARATIONS))
def test_a_declaration_the_emit_cannot_draw_is_refused(tmp_path: Path, name: str) -> None:
    text, fragment = DECLARATIONS[name]
    refusal = _refused(bundle_with(tmp_path, text))
    assert refusal.code.code == "FLX-3402", str(refusal)
    assert fragment in str(refusal), str(refusal)
    assert "fidelity.copula" in str(refusal)


def test_a_column_drawn_from_a_range_alone_is_refused(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, _declared("[amount, balance]", "[]"))
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    old = "bins: ['0', '50', '200', '500']\n              binWeights: ['0.2', '0.5', '0.3']"
    assert text.count(old) == 1
    simulation.write_text(text.replace(old, "mean: '100'"), "utf-8")
    refusal = _refused(bundle)
    assert refusal.code.code == "FLX-3402" and "bins and binWeights" in str(refusal)


def test_a_column_given_another_is_not_a_copula_column(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, _declared("[amount, limit_raised]", "[]"))
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    old = "binWeights: ['0.7', '0.3']"
    assert text.count(old) == 1
    simulation.write_text(
        text.replace(
            old,
            "given: region\n              binWeightsGiven: { north: ['0.7', '0.3'], south: ['0.2', '0.8'] }",
        ),
        "utf-8",
    )
    refusal = _refused(bundle)
    assert refusal.code.code == "FLX-3402" and "one joint" in str(refusal)


def test_a_column_another_is_given_is_not_a_copula_column(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, _declared("[amount, balance]", "[]"))
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    old = "            region: { levels: { north: '0.5', south: '0.5' } }\n"
    assert text.count(old) == 1
    simulation.write_text(
        text.replace(
            old,
            "            region:\n"
            "              given: amount\n"
            "              levelWeightsGiven:\n"
            "                '0': { north: '1', south: '1' }\n",
        ),
        "utf-8",
    )
    refusal = _refused(bundle)
    assert refusal.code.code == "FLX-3402"
    assert "amount" in str(refusal)


def test_an_exactly_allocated_column_is_not_a_copula_column(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, _declared("[amount, balance]", "[]"))
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    old = "binWeights: ['0.2', '0.5', '0.3']"
    assert text.count(old) == 1
    simulation.write_text(
        text.replace(old, old[:-1] + "]\n              allocation: exact"), "utf-8"
    )
    refusal = _refused(bundle)
    assert refusal.code.code == "FLX-3402" and "allocated" in str(refusal)


def test_a_column_that_declares_a_null_fraction_is_a_copula_column(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, _declared("[amount, balance]", "[]"))
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    old = "binWeights: ['0.2', '0.5', '0.3']"
    simulation.write_text(text.replace(old, old + "\n              nullFraction: '0.1'"), "utf-8")
    assert pipeline.execute(bundle, kernel=KERNEL_V2).emit(EMIT).rows


def _plan(bundle: Path, **overrides: Any) -> Any:
    resolved = _resolved(bundle)
    arguments: Dict[str, Any] = dict(
        sim_id=SIMULATION,
        seam_digest="sha256:" + "0" * 64,
        rows=10,
        fidelity=resolved.fidelity,
        generator_version=GENERATOR_VERSION_2,
    )
    arguments.update(overrides)
    return build_plan(resolved.compile_planned(), **arguments)


def test_a_key_or_a_column_of_a_relationship_is_not_a_copula_column(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, _declared("[amount, balance]", "[]"))
    assert _plan(bundle) is not None
    with pytest.raises(FluxError) as caught:
        _plan(bundle, related=frozenset({"balance"}))
    assert caught.value.code.code == "FLX-3402"
    assert "key" in str(caught.value) and "balance" in str(caught.value)


def test_the_plan_refuses_a_copula_on_kernel_0_1_0_and_names_the_kernel(tmp_path: Path) -> None:
    refusal = _refused(bundle_with(tmp_path, COPULA), KERNEL_V1)
    assert refusal.code is FLX_3105 or refusal.code.code == "FLX-3105"
    assert "kernel 0.2.0" in str(refusal) and "kernel 0.1.0" in str(refusal)


@pytest.mark.parametrize("key", ["history", "projects", "snapshots"])
def test_a_history_or_a_projection_does_not_hold_a_copula(tmp_path: Path, key: str) -> None:
    resolved = _resolved(bundle_with(tmp_path, COPULA))
    moved = replace(resolved, extension={**resolved.extension, key: {"x": 1}})
    with pytest.raises(FluxError) as caught:
        refuse_copula_combinations(moved, where=SIMULATION)
    assert caught.value.code.code == "FLX-3402" and "copula" in str(caught.value)
    refuse_copula_combinations(resolved, where=SIMULATION)


def test_validate_refuses_what_plan_refuses(tmp_path: Path) -> None:
    good = validate(bundle_with(tmp_path, COPULA, "good"))
    assert good.exit_code == 0, good.stderr
    bad = validate(bundle_with(tmp_path, DECLARATIONS["one column"][0], "bad"))
    assert bad.exit_code == 1 and bad.error_code == "FLX-3402"
    assert "fidelity.copula" in str(bad.artifacts["problems"])
