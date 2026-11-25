# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's association tables (ADR 0039): declared in the FLUX bundle, bound into ``H``.

The bundle is the membership table of ``tests/stages/test_associations_run.py``. An association
moves ``H`` of its emit and neither ``G`` nor any other emit's digests; its order and ``distinct``
are part of ``H``; a broken declaration is ``FLX-3105`` before any row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.associations import (
    AssociationEntry,
    association_entries_of,
    bound_into_h,
    read_associations_block,
)
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from tests.seam.test_bridges import SIMULATION, _digests
from tests.stages.test_associations_run import ASSOCIATION, COUNTS, _bundle

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def declared(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Tuple[str, str]]:
    return _digests(_bundle(tmp_path_factory.mktemp("declared"), "declared", rows=200))


def test_the_association_moves_h_of_its_emit_and_neither_g_nor_another_emit(
    tmp_path: Path, declared: Dict[str, Tuple[str, str]]
) -> None:
    for change in (
        ASSOCIATION.replace("[member, club]", "[club, member]"),
        ASSOCIATION.replace("[member, club] }", "[member, club], distinct: false }"),
    ):
        moved = _digests(_bundle(tmp_path / str(len(change)), "moved", change + COUNTS, rows=200))
        for expose_id, (h, g) in declared.items():
            assert (moved[expose_id][0] != h) is (expose_id == "membership")
            assert moved[expose_id][1] == g


def test_distinct_true_is_the_default_and_hashes_as_declared(
    tmp_path: Path, declared: Dict[str, Tuple[str, str]]
) -> None:
    spelled = ASSOCIATION.replace("[member, club] }", "[member, club], distinct: true }")
    assert _digests(_bundle(tmp_path, "spelled", spelled + COUNTS, rows=200)) == declared


@pytest.mark.parametrize(
    ("block", "problem"),
    [
        ("    associations: {}\n", "is a list"),
        ("    associations:\n      - nope\n", "is a mapping"),
        (
            "    associations:\n      - { emit: membership, relationships: [member, club], x: 1 }\n",
            "['x']",
        ),
        ("    associations:\n      - { relationships: [member, club] }\n", "emit is a non-empty"),
        (
            "    associations:\n      - { emit: nowhere, relationships: [member, club] }\n",
            "does not emit",
        ),
        ("    associations:\n      - { emit: membership, relationships: [member] }\n", "two"),
        (
            "    associations:\n      - { emit: membership, relationships: [member, member] }\n",
            "twice",
        ),
        (
            "    associations:\n      - { emit: membership, relationships: [member, club], "
            "distinct: 1 }\n",
            "true or false",
        ),
        (
            "    associations:\n      - { emit: membership, relationships: [member, nowhere] }\n",
            "does not declare",
        ),
        (
            "    associations:\n      - { emit: membership, relationships: [member, club] }\n"
            "      - { emit: membership, relationships: [club, member] }\n",
            "second association",
        ),
    ],
)
def test_a_broken_declaration_is_refused_before_any_row(
    tmp_path: Path, block: str, problem: str
) -> None:
    bundle = _bundle(tmp_path, "bad", block + COUNTS, rows=200)
    with pytest.raises(FluxError) as refused:
        resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    assert refused.value.code == FLX_3105 and problem in str(refused.value)


def test_the_member_holds_the_emit_s_association_and_reads_back() -> None:
    entries = [AssociationEntry("a", ("z", "y"), False), AssociationEntry("d", ("b", "c"))]
    member = bound_into_h(entries, "a")
    assert member == [{"relationships": ["z", "y"], "distinct": False}]
    assert bound_into_h(entries, "e") is None
    assert association_entries_of({"associations": member}, "a") == (entries[0],)
    assert association_entries_of(None, "a") == ()


def test_a_list_past_its_bound_is_refused() -> None:
    entries = [{"emit": "a", "relationships": ["r", "s"]} for _ in range(4097)]
    with pytest.raises(FluxError):
        read_associations_block(entries, emits=frozenset({"a"}), where="x", code=FLX_3105)
