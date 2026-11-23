# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's facts (ADR 0018): declared in the FLUX bundle, and bound into ``H`` by their tolerance.

The bundle is ``tests/fixtures/related-star`` with two rules on ``date_dim``
(``tests/stages/test_rules_run.py``). A fact with a tolerance moves ``H`` of its emit and not
``G``; a bundle that declares no fact, or a fact with no tolerance, hashes as it always did.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Dict, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.facts import Fact, bound_into_h, read_facts_block
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from tests.stages.test_rules_run import _bundle

pytestmark = pytest.mark.unit

NAMESPACE = "  com.agentics.flux/1:\n"


def _with_facts(tmp_path: Path, facts: str, name: str) -> Path:
    bundle = _bundle(tmp_path, name=name)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count(NAMESPACE) == 1
    block = "".join(f"    {line}\n" for line in facts.splitlines())
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + block, 1), "utf-8")
    return bundle


def _digests(bundle: Path) -> Dict[str, Tuple[str, str]]:
    from flux_engine.seam.emit import seam_digests

    loaded = load_bundle(bundle)
    found = {}
    for resolved in resolve_all_emits(loaded, simulation_id="sim-store-star"):
        digests = seam_digests(
            resolved.contract_document,
            resolved.emit,
            resolved.compile().column_names,
            resolved.extension,
        )
        found[resolved.expose_id] = (digests.h.value, digests.g.value)
    return found


def _tolerance(value: str) -> str:
    return f'facts:\n  - {{ emit: date_dim, rule: date_of_key, tolerance: "{value}" }}'


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Tuple[str, str]]:
    return _digests(_bundle(tmp_path_factory.mktemp("plain"), name="plain"))


def test_a_bundle_that_declares_no_fact_keeps_its_digests(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    assert _digests(_bundle(tmp_path, name="again")) == plain


def test_a_fact_without_a_tolerance_is_not_part_of_h(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    bundle = _with_facts(tmp_path, "facts:\n  - { emit: date_dim, rule: date_of_key }", "bare")
    assert _digests(bundle) == plain


def test_a_tolerance_moves_h_of_its_emit_and_not_g(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    moved = _digests(_with_facts(tmp_path, _tolerance("0.02"), "tol"))
    for expose_id, (h, g) in plain.items():
        if expose_id == "date_dim":
            assert moved[expose_id][0] != h
        else:
            assert moved[expose_id][0] == h
        assert moved[expose_id][1] == g


def test_a_tolerance_edit_moves_h_again_and_g_not_at_all(tmp_path: Path) -> None:
    low = _digests(_with_facts(tmp_path, _tolerance("0.02"), "low"))
    high = _digests(_with_facts(tmp_path, _tolerance("0.05"), "high"))
    assert low["date_dim"][0] != high["date_dim"][0]
    assert low["date_dim"][1] == high["date_dim"][1]


def test_two_spellings_of_one_tolerance_are_one_h(tmp_path: Path) -> None:
    quoted = _digests(_with_facts(tmp_path, _tolerance("0.02"), "quoted"))
    bare = _digests(
        _with_facts(
            tmp_path, "facts:\n  - { emit: date_dim, rule: date_of_key, tolerance: 0.02 }", "bare"
        )
    )
    assert quoted == bare


def _refused(tmp_path: Path, facts: str) -> str:
    bundle = _with_facts(tmp_path, facts, "bad")
    with pytest.raises(FluxError) as refused:
        resolve_all_emits(load_bundle(bundle), simulation_id="sim-store-star")
    assert refused.value.code == FLX_3105
    return str(refused.value)


@pytest.mark.parametrize(
    ("facts", "problem"),
    [
        ("facts: {}", "is a list"),
        ("facts:\n  - nope", "is a mapping"),
        ("facts:\n  - { emit: date_dim, rule: date_of_key, extra: 1 }", "declares ['extra']"),
        ("facts:\n  - { emit: nowhere, rule: date_of_key }", "does not emit"),
        ("facts:\n  - { emit: date_dim, rule: nothing }", "does not hold"),
        ("facts:\n  - { emit: date_dim }", "rule is a non-empty string"),
        (
            "facts:\n  - { emit: date_dim, rule: date_of_key }\n"
            "  - { emit: date_dim, rule: date_of_key }",
            "twice",
        ),
        ("facts:\n  - { emit: date_dim, rule: date_of_key, tolerance: 1.5 }", "between 0 and 1"),
        ("facts:\n  - { emit: date_dim, rule: date_of_key, tolerance: true }", "never a float"),
        ('facts:\n  - { emit: date_dim, rule: date_of_key, tolerance: "-0.1" }', "no sign"),
    ],
)
def test_a_broken_declaration_is_refused_before_any_row(
    tmp_path: Path, facts: str, problem: str
) -> None:
    assert problem in _refused(tmp_path, facts)


def test_the_member_is_the_facts_with_a_tolerance_only() -> None:
    facts = [
        Fact("a", "r2", None, None),
        Fact("a", "r1", Fraction(1, 50), "0.02"),
        Fact("b", "r1", Fraction(1, 10), "0.1"),
    ]
    assert bound_into_h(facts, "a") == {"r1": {"tolerance": "0.02"}}
    assert bound_into_h(facts, "c") is None
    assert bound_into_h([Fact("a", "r2")], "a") is None


def test_a_list_past_its_bound_is_refused() -> None:
    entries = [{"emit": "a", "rule": f"r{i}"} for i in range(4097)]
    with pytest.raises(FluxError):
        read_facts_block(entries, emits=frozenset({"a"}), where="facts", code=FLX_3105)


@pytest.mark.integration
def test_a_run_with_a_tolerance_binds_it_before_generation(tmp_path: Path) -> None:
    from flux_engine.stages import pipeline

    plain = pipeline.execute(_bundle(tmp_path, name="plain"))
    bound = pipeline.execute(_with_facts(tmp_path, _tolerance("0.02"), "bound"))
    by_id = {emit.resolved.expose_id: emit for emit in bound.emits}
    before = {emit.resolved.expose_id: emit for emit in plain.emits}
    assert by_id["date_dim"].digests.h != before["date_dim"].digests.h
    assert by_id["date_dim"].digests.g == before["date_dim"].digests.g
    # H seeds every stream of the run (KERNEL-SPEC section 3), so the rows move with it.
    assert by_id["store_sales"].rows != before["store_sales"].rows
