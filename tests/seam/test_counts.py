# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's counts entries (ADR 0016 section 1): declared in the FLUX bundle, bound into ``H``.

The bundle is ``tests/fixtures/exact-counts`` (product, account, invoice and invoice_line). A bundle
that declares no entry hashes as it always did; an entry moves ``H`` of its emit and neither ``G``
nor any other emit's ``H``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.counts import (
    ChildShape,
    CountsEntry,
    bound_into_h,
    count_entries_of,
    read_counts_block,
)
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from tests.stages.test_counts_run import FIXTURE

pytestmark = pytest.mark.unit

SIMULATION = "sim-sales"
ANCHOR = "  com.agentics.flux/1:\n"
SHAPE = '{ levels: ["1", "3"], levelWeights: ["0.5", "0.5"] }'
ENTRY = f"counts:\n  - {{ emit: invoice_line, relationship: invoice, children: {SHAPE} }}"


def _with_counts(tmp_path: Path, counts: str, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    assert text.count(ANCHOR) == 1
    block = "".join(f"    {line}\n" for line in counts.splitlines())
    path.write_text(text.replace(ANCHOR, ANCHOR + block, 1), "utf-8")
    return target


def _digests(bundle: Path) -> Dict[str, Tuple[str, str]]:
    from flux_engine.seam.emit import seam_digests

    found = {}
    for resolved in resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION):
        digests = seam_digests(
            resolved.contract_document,
            resolved.emit,
            resolved.compile().column_names,
            resolved.extension,
        )
        found[resolved.expose_id] = (digests.h.value, digests.g.value)
    return found


@pytest.fixture(scope="module")
def plain() -> Dict[str, Tuple[str, str]]:
    return _digests(FIXTURE)


def test_a_bundle_that_declares_no_entry_keeps_its_digests(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    assert _digests(_with_counts(tmp_path, "", "again")) == plain


def test_an_entry_moves_h_of_its_emit_and_neither_g_nor_another_emit(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    moved = _digests(_with_counts(tmp_path, ENTRY, "counted"))
    for expose_id, (h, g) in plain.items():
        if expose_id == "invoice_line":
            assert moved[expose_id][0] != h
        else:
            assert moved[expose_id][0] == h
        assert moved[expose_id][1] == g


def test_an_edit_of_the_shape_or_the_tolerance_moves_h_again_and_never_g(tmp_path: Path) -> None:
    base = _digests(_with_counts(tmp_path, ENTRY, "base"))
    shape = _digests(_with_counts(tmp_path, ENTRY.replace('"3"', '"4"'), "shape"))
    tolerance = _digests(_with_counts(tmp_path, ENTRY[:-2] + ', tolerance: "0.2" }', "t"))
    bare = _digests(
        _with_counts(tmp_path, "counts:\n  - { emit: invoice_line, relationship: invoice }", "bare")
    )
    seen = {base["invoice_line"][0], shape["invoice_line"][0], tolerance["invoice_line"][0]}
    assert len(seen | {bare["invoice_line"][0]}) == 4
    assert {run["invoice_line"][1] for run in (base, shape, tolerance, bare)} == {
        base["invoice_line"][1]
    }


def test_a_minimum_is_read_bound_into_h_and_left_out_of_g(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    bare = "counts:\n  - { emit: invoice_line, relationship: invoice%s }"
    runs = {
        text: _digests(_with_counts(tmp_path, bare % text, f"m{i}"))
        for i, text in enumerate(("", ", minChildren: 1", ", minChildren: '2'", ", minChildren: 0"))
    }
    heads = {run["invoice_line"][0] for run in runs.values()}
    assert len(heads) == 4  # a declared 0 is a declaration: it is in H
    assert {run["invoice_line"][1] for run in runs.values()} == {plain["invoice_line"][1]}
    assert runs[", minChildren: 1"] != runs[", minChildren: '2'"]


def test_a_parent_condition_is_read_bound_into_h_and_left_out_of_g(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    bare = "counts:\n  - { emit: invoice_line, relationship: invoice%s }"
    runs = [
        _digests(_with_counts(tmp_path, bare % text, f"w{i}"))
        for i, text in enumerate(
            ("", ", parentWhen: \"status <> 'void'\"", ", parentWhen: \"status = 'paid'\"")
        )
    ]
    assert len({run["invoice_line"][0] for run in runs}) == 3
    assert {run["invoice_line"][1] for run in runs} == {plain["invoice_line"][1]}


def _refused(tmp_path: Path, counts: str) -> str:
    bundle = _with_counts(tmp_path, counts, "bad")
    with pytest.raises(FluxError) as refused:
        resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    assert refused.value.code == FLX_3105
    return str(refused.value)


@pytest.mark.parametrize(
    ("counts", "problem"),
    [
        ("counts: {}", "is a list"),
        ("counts:\n  - nope", "is a mapping"),
        ("counts:\n  - { emit: invoice_line, relationship: invoice, extra: 1 }", "['extra']"),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice, parentWhen: 5 }",
            "predicate text",
        ),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice, parentWhen: '  ' }",
            "predicate text",
        ),
        ("counts:\n  - { relationship: invoice }", "emit is a non-empty string"),
        ("counts:\n  - { emit: invoice_line }", "relationship is a non-empty string"),
        ("counts:\n  - { emit: nowhere, relationship: invoice }", "does not emit"),
        ("counts:\n  - { emit: invoice_line, relationship: nowhere }", "does not declare"),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice, children: [] }",
            "mapping of levels",
        ),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice, children: { levels: ['1'] } }",
            "exactly",
        ),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice,"
            " children: { levels: ['1'], binWeights: ['1'] } }",
            "exactly",
        ),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice,"
            " children: { levels: [1], levelWeights: ['1'] } }",
            "not a string",
        ),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice,"
            " children: { levels: [], levelWeights: [] } }",
            "non-empty list",
        ),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice, tolerance: true }",
            "never a float",
        ),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice, tolerance: '1.5' }",
            "between 0 and 1",
        ),
        (
            "counts:\n  - { emit: invoice_line, relationship: invoice }\n"
            "  - { emit: invoice_line, relationship: invoice }",
            "twice",
        ),
    ],
)
def test_a_broken_declaration_is_refused_before_any_row(
    tmp_path: Path, counts: str, problem: str
) -> None:
    assert problem in _refused(tmp_path, counts)


def test_the_member_holds_the_entries_of_its_emit_in_relationship_order() -> None:
    shape = ChildShape("bins", ("1", "4", "9"), ("0.5", "0.5"))
    entries = [
        CountsEntry("a", "z"),
        CountsEntry("a", "b", shape, 2, "x > 1", "0.05"),
        CountsEntry("c", "b"),
    ]
    member = bound_into_h(entries, "a")
    assert member == [
        {
            "relationship": "b",
            "minChildren": 2,
            "parentWhen": "x > 1",
            "children": {"bins": ["1", "4", "9"], "binWeights": ["0.5", "0.5"]},
            "tolerance": "0.05",
        },
        {"relationship": "z"},
    ]
    assert bound_into_h(entries, "d") is None
    assert count_entries_of({"counts": member}, "a") == (
        CountsEntry("a", "b", shape, 2, "x > 1", "0.05"),
        CountsEntry("a", "z"),
    )
    levels = CountsEntry("a", "r", ChildShape("levels", ("1",), ("1",)))
    assert count_entries_of({"counts": bound_into_h([levels], "a")}, "a") == (levels,)
    assert count_entries_of(None, "a") == ()
    assert count_entries_of({"counts": "nope"}, "a") == ()


def test_names_are_read_in_nfc_and_a_list_past_its_bound_is_refused() -> None:
    (entry,) = read_counts_block(
        [{"emit": "café", "relationship": "r", "parentWhen": "caf\u0065\u0301 = 1"}],
        emits=frozenset({"café"}),
        where="counts",
        code=FLX_3105,
    )
    assert entry.emit == "café" and entry.parent_when == "café = 1"
    entries = [{"emit": "a", "relationship": f"r{i}"} for i in range(4097)]
    with pytest.raises(FluxError):
        read_counts_block(entries, emits=frozenset({"a"}), where="counts", code=FLX_3105)


def test_the_pointer_names_the_entry_and_the_key(tmp_path: Path) -> None:
    message = _refused(tmp_path, "counts:\n  - { emit: invoice_line, relationship: invoice, x: 1 }")
    assert "com.agentics.flux/1" in message and "counts[0]" in message
