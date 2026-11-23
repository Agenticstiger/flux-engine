# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's bridges (ADR 0022 section 8): declared in the FLUX bundle, bound into ``H``.

The bundle is the enrolment star of ``tests/stages/test_bridges_run.py``. A bundle that declares
no bridge hashes as it always did; a bridge moves ``H`` of its emit and neither ``G`` nor any
other emit's digests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.bridges import (
    BridgeEntry,
    bound_into_h,
    bridge_entries_of,
    read_bridges_block,
)
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from tests.stages.test_bridges_run import BRIDGES, _bundle

pytestmark = pytest.mark.unit

SIMULATION = "sim-store-star"


def _digests(bundle: Path) -> Dict[str, Tuple[str, str]]:
    from flux_engine.seam.emit import seam_digests

    loaded = load_bundle(bundle)
    found = {}
    for resolved in resolve_all_emits(loaded, simulation_id=SIMULATION):
        digests = seam_digests(
            resolved.contract_document,
            resolved.emit,
            resolved.compile().column_names,
            resolved.extension,
        )
        found[resolved.expose_id] = (digests.h.value, digests.g.value)
    return found


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Tuple[str, str]]:
    return _digests(_bundle(tmp_path_factory.mktemp("plain"), "plain", bridges="", rows=200))


def test_a_bundle_that_declares_no_bridge_keeps_its_digests(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    assert _digests(_bundle(tmp_path, "again", bridges="", rows=200)) == plain


def test_a_bridge_moves_h_of_its_emit_and_neither_g_nor_another_emit(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    moved = _digests(_bundle(tmp_path, "bridge", bridges=BRIDGES, rows=200))
    for expose_id, (h, g) in plain.items():
        if expose_id == "enrolment":
            assert moved[expose_id][0] != h
        else:
            assert moved[expose_id][0] == h
        assert moved[expose_id][1] == g


def test_the_order_of_the_two_relationships_is_part_of_h(tmp_path: Path) -> None:
    forward = _digests(_bundle(tmp_path, "forward", bridges=BRIDGES, rows=200))
    reverse = _digests(
        _bundle(
            tmp_path,
            "reverse",
            bridges=BRIDGES.replace("student, course", "course, student"),
            rows=200,
        )
    )
    assert forward["enrolment"][0] != reverse["enrolment"][0]
    assert forward["enrolment"][1] == reverse["enrolment"][1]


def _refused(tmp_path: Path, bridges: str) -> str:
    bundle = _bundle(tmp_path, "bad", bridges=bridges, rows=200)
    with pytest.raises(FluxError) as refused:
        resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    assert refused.value.code == FLX_3105
    return str(refused.value)


@pytest.mark.parametrize(
    ("bridges", "problem"),
    [
        ("    bridges: {}\n", "is a list"),
        ("    bridges:\n      - nope\n", "is a mapping"),
        (
            "    bridges:\n      - { emit: enrolment, relationships: [student, course], extra: 1 }\n",
            "['extra']",
        ),
        ("    bridges:\n      - { relationships: [student, course] }\n", "emit is a non-empty"),
        (
            "    bridges:\n      - { emit: nowhere, relationships: [student, course] }\n",
            "does not emit",
        ),
        (
            "    bridges:\n      - { emit: enrolment, relationships: [student] }\n",
            "two relationship",
        ),
        (
            "    bridges:\n      - { emit: enrolment, relationships: [student, course, x] }\n",
            "two relationship",
        ),
        ("    bridges:\n      - { emit: enrolment, relationships: student }\n", "two relationship"),
        (
            "    bridges:\n      - { emit: enrolment, relationships: [student, student] }\n",
            "twice",
        ),
        (
            "    bridges:\n      - { emit: enrolment, relationships: [student, nowhere] }\n",
            "does not declare",
        ),
        (
            "    bridges:\n      - { emit: enrolment, relationships: [student, course] }\n"
            "      - { emit: enrolment, relationships: [student, course] }\n",
            "twice",
        ),
    ],
)
def test_a_broken_declaration_is_refused_before_any_row(
    tmp_path: Path, bridges: str, problem: str
) -> None:
    assert problem in _refused(tmp_path, bridges)


def test_the_member_holds_the_bridges_of_its_emit_in_declared_order_of_names() -> None:
    entries = [
        BridgeEntry("a", ("z", "y")),
        BridgeEntry("a", ("b", "c")),
        BridgeEntry("d", ("b", "c")),
    ]
    member = bound_into_h(entries, "a")
    assert member == [{"relationships": ["b", "c"]}, {"relationships": ["z", "y"]}]
    assert bound_into_h(entries, "e") is None
    assert bridge_entries_of({"bridges": member}, "a") == (
        BridgeEntry("a", ("b", "c")),
        BridgeEntry("a", ("z", "y")),
    )
    assert bridge_entries_of(None, "a") == ()


def test_a_list_past_its_bound_is_refused() -> None:
    entries = [{"emit": "a", "relationships": [f"r{i}", f"s{i}"]} for i in range(4097)]
    with pytest.raises(FluxError):
        read_bridges_block(entries, emits=frozenset({"a"}), where="bridges", code=FLX_3105)
