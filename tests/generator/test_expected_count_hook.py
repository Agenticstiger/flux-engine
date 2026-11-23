# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The expected-count hook's plumbing (ADR 0016 section 3).

A registered hook gives the counted layout a parent's expected count, row by row. A stub stands in
for the module that will own one; the fixture unregisters it. What is pinned, on kernel 0.2.0:

* **No hook, no change.** A bundle whose hooks all return None plans, generates and digests as with
  none registered.
* **The allotment.** Without a shape the children follow the expected counts by the largest
  remainder, total the child rows and honour ``minChildren``; with a shape the histogram is the
  shape's, class for class, a parent's count never falls as its expected count rises, and equal
  expected counts are ordered by the layout's permutation and not by row.
* **A condition.** ``expected`` is called once per eligible parent, in rank order, and for no other.
* **The plan.** The member moves the child's digest only, and the child's digest holds the parent's.
* **The refusals**: two hooks, a value that is not an exact number or is negative, and a member on a
  relationship the layout cannot place; kernel 0.1.0 refuses a member with ``FLX-3105``.
* **Two paths, one answer.** The column path's rows equal the per-row path's, and the slices of a
  table, each in a process that works the layout out afresh, are the whole table.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator import columns, relations
from flux_engine.generator.counts import (
    class_counts,
    classes_of,
    count_key,
    counted_layout,
    expected_count_hooks,
    register_expected_count_hook,
    sigma_of,
    slot_values,
)
from flux_engine.generator.rows import GeneratorPlan, generate_row_slice, generate_rows
from flux_engine.seam.links import LinkEntry

from .test_counted_picks import (
    ACCOUNTS,
    INVOICES,
    SHAPE,
    SIM_ID,
    entry,
    plans_of,
    refused,
    root_for,
    world,
)
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

MEMBER = {"from": "a_weight"}
NAMESPACE = {"expectedFrom": "a_weight"}


def weight(row: Tuple[Any, ...], plan: GeneratorPlan) -> int:
    names = [column_plan.column.name for column_plan in plan.columns]
    return int(row[names.index("a_weight")]) if "a_weight" in names else 0


class Stub:
    """A hook for ``invoice.account`` whose expected count is the parent's ``a_weight``."""

    def __init__(
        self,
        value: Optional[Callable[[int, int], Any]] = None,
        member: Optional[Mapping[str, Any]] = MEMBER,
        relationship: str = "account",
    ) -> None:
        self.value = value or (lambda row, held: held)
        self.declared = member
        self.relationship = relationship
        self.namespaces: List[Mapping[str, Any]] = []
        self.asked: List[Tuple[str, str]] = []
        self.rows: List[int] = []
        self.relationships: List[str] = []

    def member(
        self, namespace: Mapping[str, Any], child: GeneratorPlan, relationship: str
    ) -> Optional[Mapping[str, Any]]:
        self.namespaces.append(namespace)
        self.asked.append((child.expose.expose_id, relationship))
        if child.expose.expose_id != "invoice" or relationship != self.relationship:
            return None
        if self.declared is MEMBER and "expectedFrom" in namespace:
            return {"from": namespace["expectedFrom"]}
        return self.declared

    def expected(
        self, parent: GeneratorPlan, row: int, final_row: Tuple[Any, ...], relationship: str
    ) -> Any:
        self.rows.append(row)
        self.relationships.append(relationship)
        return self.value(row, weight(final_row, parent))


def _test_hooks() -> Tuple[Any, ...]:
    """The registered hooks other than the engine's own exposure hook (ADR 0025 section 4)."""
    from flux_engine.generator.exposure import EXPOSURE_HOOK

    return tuple(hook for hook in expected_count_hooks() if hook is not EXPOSURE_HOOK)


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Stub]]:
    """Register stubs for one test and unregister them after it; layouts are worked out afresh."""
    monkeypatch.setattr(relations, "_LAYOUTS", {})
    undo: List[Callable[[], None]] = []

    def add(stub: Optional[Stub] = None, **options: Any) -> Stub:
        stub = stub or Stub(**options)
        undo.append(register_expected_count_hook(stub))
        return stub

    yield add
    for unregister in undo:
        unregister()
    assert _test_hooks() == ()


def digests(plans: Mapping[str, GeneratorPlan]) -> Dict[str, str]:
    return {name: plan.digest().value for name, plan in plans.items()}


def no_shape(minimum: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    return {"invoice": {"account": entry(shape=None, minimum=minimum)}}


def counts_of(data: Mapping[str, List[Dict[str, Any]]]) -> List[int]:
    held = Counter(row["i_account"] for row in data["invoice"])
    return [held[row["a_id"]] for row in data["account"]]


def weights_of(data: Mapping[str, List[Dict[str, Any]]]) -> List[int]:
    return [int(row["a_weight"]) for row in data["account"]]


def oracle(children: int, expected: Sequence[int], minimum: int) -> List[int]:
    """The largest remainder allotment of step 4, on exact fractions, written out apart from the layout."""
    spare = children - minimum * len(expected)
    spread = [Fraction(max(value - minimum, 0)) for value in expected]
    if spare > 0 and not any(spread):
        spread = [Fraction(1)] * len(expected)
    total = sum(spread)
    floors = [int(spare * value / total) for value in spread]
    left = spare - sum(floors)
    remainders = [
        spare * value / total - floor for value, floor in zip(spread, floors, strict=True)
    ]
    first = sorted(range(len(expected)), key=lambda rank: (-remainders[rank], rank))[:left]
    return [minimum + floor + (rank in first) for rank, floor in enumerate(floors)]


# -- the registry -----------------------------------------------------------------------------


def test_this_version_registers_no_hook() -> None:
    assert _test_hooks() == ()


def test_a_hook_registers_once_and_its_unregister_function_removes_it() -> None:
    stub = Stub()
    first = register_expected_count_hook(stub)
    second = register_expected_count_hook(stub)
    assert _test_hooks() == (stub,)
    first()
    assert _test_hooks() == ()
    second()  # a hook that is gone is left gone
    assert _test_hooks() == ()


# -- no hook, no change -----------------------------------------------------------------------


def test_a_hook_that_returns_none_for_every_relationship_changes_nothing(
    hooks: Callable[..., Stub],
) -> None:
    before = plans_of()
    plain = world(before)
    silent = hooks(member=None)
    after = plans_of(namespace=NAMESPACE)
    assert silent.asked, "every relationship the expose declares is asked about"
    assert digests(after) == digests(before)
    assert after["invoice"].as_document() == before["invoice"].as_document()
    assert world(after) == plain
    assert silent.rows == []  # nothing is read from the parent for a member that is not there


def test_the_hook_is_asked_about_every_relationship_with_the_simulations_namespace(
    hooks: Callable[..., Stub],
) -> None:
    stub = hooks(member=None)
    plans_of(namespace=NAMESPACE)
    assert ("invoice", "account") in stub.asked and ("invoice", "tag") in stub.asked
    assert all(namespace is NAMESPACE for namespace in stub.namespaces)


# -- the allotment ----------------------------------------------------------------------------


@pytest.mark.parametrize("minimum", [None, 1, 2])
def test_without_a_shape_the_counts_follow_the_expected_counts_by_the_largest_remainder(
    hooks: Callable[..., Stub], minimum: Optional[int]
) -> None:
    hooks()
    data = world(plans_of(counts=no_shape(minimum)))
    counts = counts_of(data)
    assert sum(counts) == INVOICES
    assert counts == oracle(INVOICES, weights_of(data), minimum or 0)
    assert min(counts) >= (minimum or 0)
    assert len(set(counts)) > 2  # the expected counts differ, so do the counts


def test_a_hook_alone_puts_the_relationship_on_the_counted_layout(
    hooks: Callable[..., Stub],
) -> None:
    hooks()
    plans = plans_of(counts={})  # no counts entry at all
    driver = plans["invoice"].relations.driver("i_account")  # type: ignore[union-attr]
    assert driver.pick == "counted" and driver.counted.expected_count == MEMBER
    data = world(plans)
    assert counts_of(data) == oracle(INVOICES, weights_of(data), 0)


def test_a_links_when_or_weight_on_a_relationship_a_hook_counts_names_the_expected_count(
    hooks: Callable[..., Stub],
) -> None:
    hooks()
    when = {"invoice": [LinkEntry("invoice", "account", when="i_amount > 100")]}
    for build in (
        lambda: plans_of(counts={}, links=when),
        lambda: plans_of(counts={}, weights={"invoice": {"account": "a_weight"}}),
    ):
        with pytest.raises(FluxError) as caught:
            build()
        message = str(caught.value)
        assert caught.value.code.code == "FLX-3403"
        assert "is on the counted layout (the expected count a hook declares)" in message


def test_the_layout_is_the_same_as_the_layout_function_given_those_expected_counts(
    hooks: Callable[..., Stub],
) -> None:
    hooks()
    data = world(plans_of(counts={"invoice": {"account": entry(shape=SHAPE)}}))
    layout = counted_layout(
        count_key(root_for(), SIM_ID, "invoice", "account"),
        ACCOUNTS,
        INVOICES,
        shape=SHAPE,
        expected=[Fraction(value) for value in weights_of(data)],
    )
    assert counts_of(data) == layout.counts.tolist()


@pytest.mark.parametrize(
    "shape",
    [
        SHAPE,
        {"bins": ["1", "3", "7", "12"], "binWeights": ["0.3", "0.5", "0.2"]},
    ],
)
def test_with_a_shape_the_histogram_is_the_no_hook_histogram_class_for_class(
    hooks: Callable[..., Stub], shape: Mapping[str, Sequence[str]]
) -> None:
    counted = {"invoice": {"account": entry(shape=shape)}}
    rows = int(slot_values(ACCOUNTS, classes_of(shape)).sum())
    plain = counts_of(world(plans_of(counts=counted, rows=rows)))
    hooks()
    hooked = counts_of(world(plans_of(counts=counted, rows=rows)))
    assert hooked != plain  # the parents took other counts
    assert sorted(hooked) == sorted(plain)  # and the histogram is the same
    held = Counter(hooked)
    classes = classes_of(shape)
    assert sum(held.values()) == ACCOUNTS and sum(hooked) == rows
    if shape is SHAPE:
        assert [held[level] for level in (1, 3, 6)] == class_counts(ACCOUNTS, classes)


def test_counts_do_not_decrease_as_expected_counts_rise_and_ties_follow_the_permutation(
    hooks: Callable[..., Stub],
) -> None:
    hooks()
    data = world(plans_of(counts={"invoice": {"account": entry(shape=SHAPE)}}))
    counts, expected = counts_of(data), weights_of(data)
    sigma = sigma_of(count_key(root_for(), SIM_ID, "invoice", "account"), ACCOUNTS)
    inverted_by_row = 0
    for a in range(ACCOUNTS):
        for b in range(ACCOUNTS):
            if expected[a] < expected[b]:
                assert counts[a] <= counts[b], (a, b)
            if expected[a] == expected[b] and sigma[a] < sigma[b]:
                assert counts[a] <= counts[b], (a, b)
                inverted_by_row += a > b and counts[a] < counts[b]
    # Equal expected counts are not ordered by row: a later row takes the larger count somewhere.
    assert inverted_by_row > 0


def test_a_shape_with_a_minimum_keeps_the_minimum_under_a_hook(hooks: Callable[..., Stub]) -> None:
    shape = {"levels": ["0", "2", "5"], "levelWeights": ["0.25", "0.5", "0.25"]}
    hooks()
    data = world(plans_of(counts={"invoice": {"account": entry(shape=shape, minimum=1)}}))
    assert min(counts_of(data)) >= 1 and sum(counts_of(data)) == INVOICES


# -- a condition ------------------------------------------------------------------------------


def test_expected_is_called_once_per_eligible_parent_in_rank_order_and_for_no_other(
    hooks: Callable[..., Stub],
) -> None:
    stub = hooks()
    counts = {"invoice": {"account": entry(shape=None, when="a_weight > 5")}}
    plans = plans_of(counts=counts)
    data = world(plans)
    eligible = [row for row, held in enumerate(weights_of(data)) if held > 5]
    assert 0 < len(eligible) < ACCOUNTS
    assert stub.rows == eligible
    assert set(stub.relationships) == {"account"}
    held = counts_of(data)
    assert [held[row] for row in range(ACCOUNTS) if row not in set(eligible)] == [0] * (
        ACCOUNTS - len(eligible)
    )
    assert [held[row] for row in eligible] == oracle(
        INVOICES, [weights_of(data)[row] for row in eligible], 0
    )


# -- the plan ---------------------------------------------------------------------------------


def test_the_member_is_in_the_childs_document_and_moves_the_childs_digest_only(
    hooks: Callable[..., Stub],
) -> None:
    plain = plans_of()
    hooks()
    hooked = plans_of()
    document = hooked["invoice"].as_document()["relations"]["i_account"]["counts"]
    assert document["expectedCount"] == MEMBER and document["children"] == SHAPE
    assert "expectedCount" not in plain["invoice"].as_document()["relations"]["i_account"]["counts"]
    assert digests(hooked)["invoice"] != digests(plain)["invoice"]
    for name in ("account", "tag"):
        assert digests(hooked)[name] == digests(plain)[name], name
    # The member is read off the namespace, and a changed one moves the child's digest.
    other = plans_of(namespace={"expectedFrom": "a_name"})["invoice"].digest()
    named = plans_of(namespace=NAMESPACE)["invoice"].digest()
    assert other != named


def test_the_childs_digest_holds_the_parents_because_the_hook_reads_its_final_rows(
    hooks: Callable[..., Stub],
) -> None:
    plain = plans_of()["invoice"]
    hooks()
    hooked = plans_of()["invoice"]
    assert sorted(hooked.as_document()["reads"]) == ["account"]
    assert "reads" not in plain.as_document()
    moved = plans_of(account={"a_weight": {"range": {"min": "1", "max": "8"}}})["invoice"]
    assert moved.digest() != hooked.digest()


def test_a_plan_without_a_member_keeps_its_bytes_whatever_hook_is_registered(
    hooks: Callable[..., Stub],
) -> None:
    before = plans_of()["invoice"]
    hooks(relationship="tag", member=None)
    after = plans_of()["invoice"]
    assert after.as_document() == before.as_document() and after.digest() == before.digest()


# -- the refusals -----------------------------------------------------------------------------


def test_two_hooks_that_both_declare_a_relationship_are_refused(hooks: Callable[..., Stub]) -> None:
    hooks()
    hooks(member={"from": "other"})
    assert "more than one hook" in refused(lambda: plans_of())


def test_two_hooks_are_fine_when_they_declare_different_relationships(
    hooks: Callable[..., Stub],
) -> None:
    hooks()
    hooks(relationship="tag", member={"tags": True})
    plans = plans_of(counts={"invoice": {"account": entry(shape=None)}})
    data = world(plans)
    assert sum(counts_of(data)) == INVOICES


@pytest.mark.parametrize(
    "bad",
    [1.5, Decimal("2"), -1, Fraction(-1, 2), True, "3", None],
    ids=["float", "decimal", "negative-int", "negative-fraction", "bool", "text", "none"],
)
def test_a_value_that_is_not_an_exact_number_or_is_negative_is_refused_when_laid_out(
    hooks: Callable[..., Stub], bad: Any
) -> None:
    hooks(value=lambda row, held: bad)
    plans = plans_of(counts=no_shape())
    message = refused(lambda: generate_rows(plans["invoice"], root=root_for()))
    assert "expected-count hook" in message


@pytest.mark.parametrize("good", [3, Fraction(7, 2), 0])
def test_an_int_and_a_fraction_are_accepted(hooks: Callable[..., Stub], good: Any) -> None:
    hooks(value=lambda row, held: good)
    plans = plans_of(counts=no_shape())
    assert len(generate_rows(plans["invoice"], root=root_for())) == INVOICES


def test_every_refusal_of_a_counts_entry_applies_to_a_member(hooks: Callable[..., Stub]) -> None:
    hooks()
    message = refused(lambda: plans_of(counts={}, invoice={"i_account": {"nullFraction": "0.1"}}))
    assert "nullFraction" in message
    hooks(relationship="tag", member={"tag": 1})
    # A member beside a counts entry takes the entry's minimum: more than the rows allow is refused.
    refused(lambda: plans_of(counts=no_shape(minimum=3)))


def test_kernel_0_1_0_refuses_a_member_and_names_the_kernel(hooks: Callable[..., Stub]) -> None:
    hooks()
    message = refused(lambda: plans_of(counter_based=False, counts={}), code="FLX-3105")
    assert "0.1.0" in message


# -- the layout is the rows -------------------------------------------------------------------


def test_the_layout_is_worked_out_once_with_each_parent_read_once(
    hooks: Callable[..., Stub],
) -> None:
    stub = hooks()
    plans = plans_of(counts=no_shape())
    generate_rows(plans["invoice"], root=root_for())
    generate_row_slice(plans["invoice"], 3, 30, root=root_for())
    assert stub.rows == list(range(ACCOUNTS))


def test_a_cached_layout_does_not_outlive_the_hook_that_made_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(relations, "_LAYOUTS", {})
    root = root_for()
    found, kept = [], []
    for value in (lambda row, held: held, lambda row, held: 10 - held):
        unregister = register_expected_count_hook(Stub(value=value))
        try:
            # The same member and the same plans but for the hook; the cache is not cleared.
            plans = plans_of(counts=no_shape())
            kept.append(plans)
            found.append(generate_rows(plans["invoice"], root=root))
        finally:
            unregister()
    assert found[0] != found[1]


@pytest.mark.parametrize("chunk", [5, 64])
@pytest.mark.parametrize("shaped", [False, True])
def test_the_column_path_equals_the_per_row_path_under_a_hook(
    monkeypatch: pytest.MonkeyPatch, hooks: Callable[..., Stub], chunk: int, shaped: bool
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    hooks()
    counts = {"invoice": {"account": entry(shape=SHAPE if shaped else None, minimum=1)}}
    plans = plans_of(counts=counts)
    root = root_for()
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_rows(plans["invoice"], root=root)
    )
    assert by_columns == by_rows and taken and all(taken)


def test_the_slices_of_the_table_are_the_whole_table_each_in_a_process_of_its_own(
    monkeypatch: pytest.MonkeyPatch, hooks: Callable[..., Stub]
) -> None:
    hooks()
    plans = plans_of(counts={"invoice": {"account": entry(shape=SHAPE)}})
    root = root_for()
    whole = generate_rows(plans["invoice"], root=root)
    joined: List[Tuple[Any, ...]] = []
    for start in range(0, INVOICES, 37):
        # Another process has no layout yet: work it out afresh for each slice.
        monkeypatch.setattr(relations, "_LAYOUTS", {})
        joined.extend(
            generate_row_slice(plans["invoice"], start, min(start + 37, INVOICES), root=root)
        )
    assert tuple(joined) == tuple(whole)
