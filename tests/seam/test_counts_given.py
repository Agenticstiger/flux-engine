# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``counts[].given`` and ``counts[].childrenGiven`` as a bundle declares them (ADR 0037 decision 4).

The entry is read, bound into ``H`` as the emit's ``extension`` member, and left out of ``G`` with the
rest of the ``counts`` member: :func:`flux_engine.seam.emit.reusable_extension`,
:func:`flux_engine.seam.emit.seam_reusable_digest` and the ``G`` ``seam_digests`` takes agree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.counts import (
    ChildShape,
    CountsEntry,
    bound_into_h,
    count_entries_of,
    read_counts_block,
)
from flux_engine.seam.emit import reusable_extension, strip_excluded
from flux_engine.seam.resolve import FLX_3105

from .test_counts import SHAPE, _digests, _with_counts

pytestmark = pytest.mark.unit

GIVEN = (
    "counts:\n"
    f"  - {{ emit: invoice, relationship: account, children: {SHAPE}, given: segment,"
    ' childrenGiven: { retail: ["0.9", "0.1"], trade: ["0.1", "0.9"] } }'
)
PLAIN = f"counts:\n  - {{ emit: invoice, relationship: account, children: {SHAPE} }}"


def _read(entry: Dict[str, Any]) -> Tuple[CountsEntry, ...]:
    return read_counts_block(
        [{"emit": "invoice", "relationship": "account", **entry}],
        emits=frozenset({"invoice"}),
        where="counts",
        code=FLX_3105,
    )


def test_the_entry_is_read_and_its_member_reads_back_as_the_entry() -> None:
    shape = ChildShape("levels", ("1", "3"), ("0.5", "0.5"))
    (entry,) = _read(
        {
            "children": {"levels": ["1", "3"], "levelWeights": ["0.5", "0.5"]},
            "given": "segment",
            "childrenGiven": {"trade": ["0.1", "0.9"], "retail": ["0.9", "0.1"]},
        }
    )
    assert entry.given == "segment"
    assert entry.children_given == (("retail", ("0.9", "0.1")), ("trade", ("0.1", "0.9")))
    member = bound_into_h([entry], "invoice")
    assert member is not None and member[0]["given"] == "segment"
    assert member[0]["childrenGiven"] == {"retail": ["0.9", "0.1"], "trade": ["0.1", "0.9"]}
    assert count_entries_of({"counts": member}, "invoice") == (entry,)
    assert entry.children == shape


def test_an_entry_without_the_new_keys_has_the_member_it_always_had() -> None:
    (entry,) = _read({"children": {"levels": ["1"], "levelWeights": ["1"]}})
    assert entry.given is None and entry.children_given is None
    assert bound_into_h([entry], "invoice") == [
        {"relationship": "account", "children": {"levels": ["1"], "levelWeights": ["1"]}}
    ]


@pytest.mark.parametrize(
    "entry, problem",
    [
        ({"given": ""}, "given is the name of one column"),
        ({"given": ["segment"]}, "given is the name of one column"),
        ({"childrenGiven": ["0.5"]}, "childrenGiven is a mapping"),
        ({"childrenGiven": {"a": "0.5"}}, "non-empty list of decimal strings"),
        ({"childrenGiven": {"": ["1"]}}, "not a non-empty string"),
    ],
)
def test_a_malformed_declaration_is_refused_naming_where(
    entry: Dict[str, Any], problem: str
) -> None:
    with pytest.raises(FluxError) as caught:
        _read(entry)
    assert problem in str(caught.value) and "counts[0]" in str(caught.value)


def test_a_class_with_an_empty_list_is_read_and_left_to_the_plan_to_refuse() -> None:
    (entry,) = _read({"childrenGiven": {"a": []}})
    assert entry.children_given == (("a", ()),)


def test_the_entry_moves_h_and_never_g(tmp_path: Path) -> None:
    plain = _digests(_with_counts(tmp_path, PLAIN, "plain"))
    given = _digests(_with_counts(tmp_path, GIVEN, "given"))
    other = _digests(_with_counts(tmp_path, GIVEN.replace('"0.9", "0.1"', '"0.8", "0.2"'), "other"))
    assert given["invoice"][0] != plain["invoice"][0]
    assert other["invoice"][0] != given["invoice"][0]
    assert {run["invoice"][1] for run in (plain, given, other)} == {plain["invoice"][1]}
    for expose_id in given:
        if expose_id != "invoice":
            assert given[expose_id] == plain[expose_id]


def test_g_leaves_the_member_out_at_every_place_it_is_read() -> None:
    entry = {
        "counts": bound_into_h(_read({"given": "segment"}), "invoice"),
        "other": "kept",
    }
    assert "counts" not in (strip_excluded(entry) or {})
    assert reusable_extension({"counts": entry["counts"]}) is None
    assert reusable_extension(entry) == {"other": "kept"}
