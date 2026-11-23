# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's link entries (ADR 0022 section 5): declared in the FLUX bundle, bound into ``H``.

The bundle is the billing star of ``tests/stages/test_shared_columns_run.py`` (customer, account
and invoice). A bundle that declares no entry hashes as it always did; an entry moves ``H`` of its
emit and neither ``G`` nor any other emit's digests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.links import LinkEntry, bound_into_h, link_entries_of, read_links_block
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from tests.stages.test_shared_columns_run import _bundle

pytestmark = pytest.mark.unit

NAMESPACE = "  com.agentics.flux/1:\n"
SIMULATION = "sim-store-star"
CONDITION = 'links:\n  - { emit: invoice, relationship: account, when: "i_amount > 250" }'


def _with_links(tmp_path: Path, links: str, name: str) -> Path:
    bundle = _bundle(tmp_path, rows=200, name=name)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count(NAMESPACE) == 1
    block = "".join(f"    {line}\n" for line in links.splitlines())
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + block, 1), "utf-8")
    return bundle


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
    return _digests(_bundle(tmp_path_factory.mktemp("plain"), rows=200, name="plain"))


def test_a_bundle_that_declares_no_link_keeps_its_digests(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    assert _digests(_bundle(tmp_path, rows=200, name="again")) == plain


def test_an_entry_moves_h_of_its_emit_and_neither_g_nor_another_emit(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    moved = _digests(_with_links(tmp_path, CONDITION, "cond"))
    for expose_id, (h, g) in plain.items():
        if expose_id == "invoice":
            assert moved[expose_id][0] != h
        else:
            assert moved[expose_id][0] == h
        assert moved[expose_id][1] == g


def test_a_sentinel_alone_is_an_entry_and_moves_h(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    sentinel = "links:\n  - { emit: invoice, relationship: account, sentinels: { a_score: '-1' } }"
    moved = _digests(_with_links(tmp_path, sentinel, "sentinel"))
    assert moved["invoice"][0] != plain["invoice"][0]
    assert moved["invoice"][1] == plain["invoice"][1]
    other = _digests(_with_links(tmp_path, sentinel.replace("-1", "-2"), "other"))
    assert other["invoice"][0] != moved["invoice"][0]


def test_an_edit_of_the_condition_moves_h_again(tmp_path: Path) -> None:
    low = _digests(_with_links(tmp_path, CONDITION, "low"))
    high = _digests(_with_links(tmp_path, CONDITION.replace("250", "300"), "high"))
    assert low["invoice"][0] != high["invoice"][0]
    assert low["invoice"][1] == high["invoice"][1]


def _refused(tmp_path: Path, links: str) -> str:
    bundle = _with_links(tmp_path, links, "bad")
    with pytest.raises(FluxError) as refused:
        resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    assert refused.value.code == FLX_3105
    return str(refused.value)


@pytest.mark.parametrize(
    ("links", "problem"),
    [
        ("links: {}", "is a list"),
        ("links:\n  - nope", "is a mapping"),
        ("links:\n  - { emit: invoice, relationship: account, when: x, extra: 1 }", "['extra']"),
        ("links:\n  - { relationship: account, when: x }", "emit is a non-empty string"),
        ("links:\n  - { emit: invoice, when: x }", "relationship is a non-empty string"),
        ("links:\n  - { emit: nowhere, relationship: account, when: x }", "does not emit"),
        (
            "links:\n  - { emit: invoice, relationship: nowhere, when: x }",
            "does not declare",
        ),
        ("links:\n  - { emit: invoice, relationship: account }", "configures nothing"),
        ("links:\n  - { emit: invoice, relationship: account, when: 5 }", "predicate text"),
        (
            "links:\n  - { emit: invoice, relationship: account, weight: 1 }",
            "weight is a non-empty string",
        ),
        (
            "links:\n  - { emit: invoice, relationship: account, weight: '' }",
            "weight is a non-empty string",
        ),
        (
            "links:\n  - { emit: invoice, relationship: account, sentinels: {} }",
            "non-empty mapping",
        ),
        (
            "links:\n  - { emit: invoice, relationship: account, sentinels: { a_score: 0 } }",
            "write it as a string",
        ),
        (
            "links:\n  - { emit: invoice, relationship: account, when: a }\n"
            "  - { emit: invoice, relationship: account, when: b }",
            "twice",
        ),
    ],
)
def test_a_broken_declaration_is_refused_before_any_row(
    tmp_path: Path, links: str, problem: str
) -> None:
    assert problem in _refused(tmp_path, links)


def test_the_member_holds_the_entries_of_its_emit_in_relationship_order() -> None:
    entries = [
        LinkEntry("a", "z", "x > 1"),
        LinkEntry("a", "b", "x > 2"),
        LinkEntry("c", "b", "x > 3"),
    ]
    member = bound_into_h(entries, "a")
    assert bound_into_h([LinkEntry("a", "r", None, (("c", "0"), ("b", "1")))], "a") == [
        {"relationship": "r", "sentinels": {"b": "1", "c": "0"}}
    ]
    assert member == [
        {"relationship": "b", "when": "x > 2"},
        {"relationship": "z", "when": "x > 1"},
    ]
    weighted = LinkEntry("a", "w", None, (), "a_weight")
    assert bound_into_h([weighted], "a") == [{"relationship": "w", "weight": "a_weight"}]
    assert link_entries_of({"links": [{"relationship": "w", "weight": "a_weight"}]}, "a") == (
        weighted,
    )
    assert bound_into_h(entries, "d") is None
    assert link_entries_of({"links": member}, "a") == (
        LinkEntry("a", "b", "x > 2"),
        LinkEntry("a", "z", "x > 1"),
    )
    assert link_entries_of(None, "a") == ()


def test_names_are_read_in_nfc_and_a_list_past_its_bound_is_refused() -> None:
    (entry,) = read_links_block(
        [{"emit": "café", "relationship": "r", "when": "x"}],
        emits=frozenset({"café"}),
        where="links",
        code=FLX_3105,
    )
    assert entry.emit == "café"
    entries = [{"emit": "a", "relationship": f"r{i}", "when": "x"} for i in range(4097)]
    with pytest.raises(FluxError):
        read_links_block(entries, emits=frozenset({"a"}), where="links", code=FLX_3105)
