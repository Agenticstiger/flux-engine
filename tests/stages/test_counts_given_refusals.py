# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a ``given`` and its ``childrenGiven`` may not say, refused before any row (``FLX-3403``).

ADR 0037 decision 4: a ``given`` the parent does not have, a key that is not a class of the given
column, a list of another length than the classes or with no weight, ``childrenGiven`` without
``given``, ``given`` without ``children``, a column that is no category or bin column, a column with a
``nullFraction``, and a column the layout cannot read from a final row. A column drawn from bins is
a class by bin, and its entry is keyed by the text of each lower edge.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator.families import Family
from flux_engine.generator.relations import _check_given_column
from flux_engine.stages import pipeline

from .test_counts_given_run import BASE_ENTRY, given_bundle
from .test_counts_run import INVOICES, bundle_with

pytestmark = pytest.mark.integration


def _refused(bundle: Path) -> str:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403", str(caught.value)
    return str(caught.value)


@pytest.mark.parametrize(
    "given, lists, problem",
    [
        ("region", {"north": ["0.5", "0.25", "0.25"]}, "does not have as a column"),
        ("segment", {"wholesale": ["0.5", "0.25", "0.25"]}, "the class 'wholesale'"),
        ("segment", {"retail": ["0.5", "0.5"]}, "2 weights; children has 3 classes"),
        ("segment", {"retail": []}, "has no weight"),
        ("segment", {"retail": ["0.5", "0.5", "0.5"]}, "sum to"),
        ("segment", {"retail": ["-0.5", "1", "0.5"]}, "no weight of 0 or more"),
        ("account_id", {}, "no category column"),
    ],
)
def test_a_declaration_the_parent_cannot_serve_is_refused_naming_where(
    tmp_path: Path, given: str, lists: Dict[str, List[str]], problem: str
) -> None:
    message = _refused(given_bundle(tmp_path, given=given, lists=lists))
    assert problem in message and "invoice" in message and "account" in message


def test_childrenGiven_without_given_is_refused(tmp_path: Path) -> None:
    target = bundle_with(tmp_path, "no-given")
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    path.write_text(
        text.replace(BASE_ENTRY, BASE_ENTRY + ', childrenGiven: { retail: ["1", "0", "0"] }'),
        "utf-8",
    )
    assert "has childrenGiven and no given column" in _refused(target)


def test_given_without_children_is_refused(tmp_path: Path) -> None:
    target = bundle_with(tmp_path, "no-children")
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    entry = "emit: invoice, relationship: account"
    assert text.count(entry) == 1
    start = text.index(entry)
    end = text.index("}", text.index("children:", start)) + 1
    path.write_text(text[:start] + entry + ", given: segment" + text[end:], "utf-8")
    assert "a given column and no children shape" in _refused(target)


# -- a column drawn from bins is classed by its bin ---------------------------------------------

DATED = "issued_on: { range: { min: '2026-07-01', max: '2026-09-30' } }"
BINNED = (
    "issued_on: { range: { min: '2026-07-01', max: '2026-09-30' },"
    " bins: ['2026-07-01', '2026-08-01', '2026-09-01', '2026-09-30'],"
    " binWeights: ['0.5', '0.3', '0.2'] }"
)
LINE_ENTRY = "emit: invoice_line, relationship: invoice,"


def _binned_bundle(tmp_path: Path, lists: str) -> Path:
    target = bundle_with(tmp_path, "binned")
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    assert text.count(DATED) == 1 and text.count(LINE_ENTRY) == 1
    text = text.replace(DATED, BINNED)
    start = text.index(LINE_ENTRY)
    end = text.index("}", text.index("children:", start)) + 1
    text = text[:end] + f", given: issued_on, childrenGiven: {lists} " + text[end:]
    path.write_text(text, "utf-8")
    return target


def test_a_column_drawn_from_bins_is_classed_by_its_bin_and_keyed_by_its_lower_edge(
    tmp_path: Path,
) -> None:
    lists = '{ "2026-07-01": ["1", "0", "0"], "2026-09-01": ["0", "0", "1"] }'
    outcome = pipeline.execute(_binned_bundle(tmp_path, lists), kernel=KERNEL_V2)
    invoice, lines = outcome.emit("invoice"), outcome.emit("invoice_line")
    names = list(invoice.expose.column_names)
    held: Dict[Any, int] = {}
    for row in lines.rows:
        key = row[list(lines.expose.column_names).index("invoice_id")]
        held[key] = held.get(key, 0) + 1
    edges = ("2026-08-01", "2026-09-01")
    counts: Dict[str, List[int]] = {"2026-07-01": [], "2026-08-01": [], "2026-09-01": []}
    for row in invoice.rows:
        issued = str(row[names.index("issued_on")])
        bin_ = (
            "2026-07-01" if issued < edges[0] else "2026-08-01" if issued < edges[1] else edges[1]
        )
        counts[bin_].append(held.get(row[names.index("invoice_id")], 0))
    assert INVOICES == sum(len(each) for each in counts.values())
    # the first bin's invoices take the lowest count class (1 line), the last bin's the highest
    assert set(counts["2026-07-01"]) <= {1, 2}
    assert min(counts["2026-09-01"]) >= 5
    assert counts["2026-08-01"]


def test_a_key_that_is_not_the_text_of_a_lower_edge_is_refused(tmp_path: Path) -> None:
    message = _refused(_binned_bundle(tmp_path, '{ "2026-07-02": ["1", "0", "0"] }'))
    assert "the class '2026-07-02'" in message


# -- the column the layout reads from a final row ----------------------------------------------


def _held(name: str = "segment", **statistics: Any) -> Any:
    stats = SimpleNamespace(
        null_fraction=None,
        given_link=None,
        bins=None,
        levels=("a", "b"),
        support=None,
        level_weights_given=None,
        **statistics,
    )
    return SimpleNamespace(
        column=SimpleNamespace(name=name), family=Family.CATEGORICAL, statistics=stats
    )


def _parent(*rules: Any, summarised: frozenset = frozenset()) -> Any:
    return SimpleNamespace(
        rules=SimpleNamespace(summarised=summarised, rules=tuple(rules)), expose=None
    )


def _rule(**held: Any) -> Any:
    base = dict(determines=("segment",), paths=(), previous=False, windowed=False, summary=None)
    return SimpleNamespace(**{**base, **held})


@pytest.mark.parametrize(
    "rule, summarised, problem",
    [
        (_rule(paths=(("account",),)), frozenset(), "from a link, a previous version"),
        (_rule(previous=True), frozenset(), "from a link, a previous version"),
        (_rule(windowed=True), frozenset(), "from a link, a previous version"),
        (_rule(summary=("invoice", "account")), frozenset(), "from a link, a previous version"),
        (None, frozenset({"segment"}), "a summary over the children"),
    ],
)
def test_a_given_column_that_reads_beyond_its_row_or_a_summary_sets_is_refused(
    rule: Any, summarised: frozenset, problem: str
) -> None:
    parent = _parent(*([] if rule is None else [rule]), summarised=summarised)
    from flux_engine.core.errors import FluxError as Refusal

    with pytest.raises(Refusal) as caught:
        _check_given_column("where", "account", parent, _held())
    assert caught.value.code.code == "FLX-3403" and problem in str(caught.value)


def test_a_given_column_with_a_null_fraction_is_refused() -> None:
    held = _held()
    held.statistics.null_fraction = "0.1"
    with pytest.raises(FluxError) as caught:
        _check_given_column("where", "account", _parent(), held)
    assert caught.value.code.code == "FLX-3403" and "declares a nullFraction" in str(caught.value)


def test_a_given_column_drawn_given_a_linked_row_is_refused() -> None:
    held = _held()
    held.statistics.given_link = ("account", "segment")
    with pytest.raises(FluxError) as caught:
        _check_given_column("where", "account", _parent(), held)
    assert caught.value.code.code == "FLX-3403" and "linked row" in str(caught.value)


def test_a_category_column_a_rule_sets_from_its_own_row_is_read_as_it_is() -> None:
    _check_given_column("where", "account", _parent(_rule()), _held())
