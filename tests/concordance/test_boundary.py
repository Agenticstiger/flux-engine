# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §4: the committed state, ``C1``, ``C2``, the store.

The tests that matter most here are the ones about what does *not* cross:
the published committed state carries no record and no blinding value, and
the check counter cannot be lowered. The commitment tests check that the
tree distinguishes every change it is meant to -- a present zero from a
null, a null from padding, one order from another.
"""

from __future__ import annotations

import json
from typing import List

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.concordance.boundary.merkle import (
    EMPTY_NODE,
    LeafStatus,
    Sha256Leaf,
    category_body,
    commit_column,
    leaf_preimage,
    merkle_root,
    order_commitments,
    units_body,
    units_body_width,
)
from flux_engine.concordance.boundary.slice import (
    MINIMUM_BLINDING_BYTES,
    SliceReference,
    commit_state,
    commitment_c2,
    read_slice,
)
from flux_engine.concordance.boundary.store import BoundaryStore
from flux_engine.core.errors import FluxError
from flux_engine.core.fixedpoint import Grid, Interval
from tests.concordance.conftest import build_committed, build_compiled, write_slice

pytestmark = pytest.mark.unit

BLINDING = bytes(range(32))
OTHER_BLINDING = bytes(range(1, 33))


# ---------------------------------------------------------------------------
# The Merkle commitment
# ---------------------------------------------------------------------------


def _leaves(bodies: List[bytes | None], width: int) -> List[str]:
    function = Sha256Leaf()
    return [
        function(leaf_preimage(LeafStatus.PRESENT if b is not None else LeafStatus.NULL, b, width))
        for b in bodies
    ]


def test_a_present_zero_a_null_and_a_padding_position_differ(document) -> None:
    width = 4
    present = leaf_preimage(LeafStatus.PRESENT, units_body(0, width), width)
    null = leaf_preimage(LeafStatus.NULL, None, width)
    padding = leaf_preimage(LeafStatus.PADDING, None, width)
    assert len({present, null, padding}) == 3


def test_a_leaf_carries_a_validity_flag(document) -> None:
    width = 2
    present = leaf_preimage(LeafStatus.PRESENT, units_body(1, width), width)
    null = leaf_preimage(LeafStatus.NULL, None, width)
    assert present[-width - 1 : -width] == b"\x01"
    assert null[-width - 1 : -width] == b"\x00"


def test_a_leaf_of_the_wrong_width_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        leaf_preimage(LeafStatus.PRESENT, b"\x00", 4)
    assert caught.value.code.code == "FLX-5035"


def test_a_present_position_without_a_value_is_refused() -> None:
    with pytest.raises(FluxError):
        leaf_preimage(LeafStatus.PRESENT, None, 4)


def test_a_null_position_with_a_value_is_refused() -> None:
    with pytest.raises(FluxError):
        leaf_preimage(LeafStatus.NULL, b"\x00", 1)


def test_a_value_wider_than_the_committed_width_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        units_body(1 << 20, 1)
    assert caught.value.code.code == "FLX-5035"


def test_the_committed_width_covers_the_declared_range() -> None:
    grid = Grid(4)
    width = units_body_width(grid, Interval.of(-8, 8))
    assert units_body(grid.quantise(8), width)
    assert units_body(grid.quantise(-8), width)


def test_a_negative_grid_value_commits_in_two_s_complement() -> None:
    assert units_body(-1, 2) == b"\xff\xff"
    assert units_body(1, 2) == b"\x00\x01"


def test_an_empty_column_has_the_reserved_empty_root() -> None:
    assert merkle_root([], Sha256Leaf()) == EMPTY_NODE


def test_the_root_depends_on_the_order_of_the_leaves() -> None:
    leaves = _leaves([b"\x01", b"\x02", b"\x03", b"\x04"], 1)
    assert merkle_root(leaves, Sha256Leaf()) != merkle_root(list(reversed(leaves)), Sha256Leaf())


def test_padding_to_records_max_makes_the_root_independent_of_the_record_count() -> None:
    """Two states of different sizes commit trees of the same shape."""
    short = commit_column(column="c", bodies=[b"\x01"], padded_to=8, width=1)
    long = commit_column(column="c", bodies=[b"\x01"] * 4, padded_to=8, width=1)
    assert short.leaf_count == long.leaf_count == 8
    assert short.root != long.root


def test_a_null_and_a_present_zero_give_different_roots() -> None:
    with_zero = commit_column(column="c", bodies=[b"\x00"], padded_to=4, width=1)
    with_null = commit_column(column="c", bodies=[None], padded_to=4, width=1)
    assert with_zero.root != with_null.root
    assert with_zero.present_count == 1 and with_zero.null_count == 0
    assert with_null.present_count == 0 and with_null.null_count == 1


def test_a_null_and_a_padding_position_give_different_roots() -> None:
    """Extending a state with nulls must not leave its root unchanged."""
    padded = commit_column(column="c", bodies=[b"\x01"], padded_to=4, width=1)
    nulled = commit_column(column="c", bodies=[b"\x01", None], padded_to=4, width=1)
    assert padded.root != nulled.root


def test_more_records_than_records_max_are_refused() -> None:
    with pytest.raises(FluxError) as caught:
        commit_column(column="c", bodies=[b"\x01"] * 5, padded_to=4, width=1)
    assert caught.value.code.code == "FLX-5035"


def test_changing_one_value_changes_the_root() -> None:
    base = commit_column(column="c", bodies=[b"\x01", b"\x02"], padded_to=4, width=1)
    moved = commit_column(column="c", bodies=[b"\x01", b"\x03"], padded_to=4, width=1)
    assert base.root != moved.root


def test_a_categorical_body_is_a_fixed_width_digest_of_its_nfc_bytes() -> None:
    composed = category_body("café")
    decomposed = category_body("café")
    assert composed == decomposed, "NFC first, so two spellings commit alike"
    assert len(composed) == 32
    assert composed != category_body("cafe")


def test_the_commitment_document_names_the_leaf_function() -> None:
    document = commit_column(column="c", bodies=[b"\x01"], padded_to=2, width=1).to_document()
    assert document["leafFunction"] == "sha256"
    assert document["root"].startswith("sha256:")
    assert set(document) == {"column", "root", "leafCount", "leafFunction"}


def test_a_commitment_document_carries_no_null_count() -> None:
    """§4's list of what leaves the boundary does not include one."""
    commitment = commit_column(column="c", bodies=[b"\x01", None], padded_to=4, width=1)
    assert commitment.null_count == 1
    assert "nullCount" not in commitment.to_document()


def test_commitments_are_ordered_by_column_name() -> None:
    made = [
        commit_column(column=name, bodies=[b"\x01"], padded_to=2, width=1)
        for name in ("z", "a", "m")
    ]
    assert [entry.column for entry in order_commitments(made)] == ["a", "m", "z"]


@given(
    bodies=st.lists(st.one_of(st.none(), st.binary(min_size=1, max_size=1)), max_size=8),
    padded_to=st.integers(min_value=8, max_value=16),
)
@pytest.mark.property
def test_a_commitment_is_a_pure_function_of_its_leaves(bodies, padded_to) -> None:
    first = commit_column(column="c", bodies=bodies, padded_to=padded_to, width=1)
    second = commit_column(column="c", bodies=list(bodies), padded_to=padded_to, width=1)
    assert first == second
    assert first.present_count + first.null_count == len(bodies)


# ---------------------------------------------------------------------------
# Reading a file:// slice
# ---------------------------------------------------------------------------


def test_a_slice_reads_as_records_and_a_sequence_boundary(tmp_path, committed_rows) -> None:
    uri = write_slice(tmp_path / "s.ndjson", committed_rows)
    rows, reference = read_slice(uri)
    assert rows == committed_rows
    assert reference.first_line == 1
    assert reference.last_line == len(committed_rows)
    assert reference.to_document()["uri"] == uri


def test_blank_lines_do_not_consume_a_sequence_position(tmp_path, committed_rows) -> None:
    path = tmp_path / "s.ndjson"
    path.write_text(
        "\n".join(json.dumps(row) for row in committed_rows) + "\n\n\n", encoding="utf-8"
    )
    rows, reference = read_slice(path.as_uri())
    assert len(rows) == len(committed_rows)
    assert reference.last_line == len(committed_rows)


@pytest.mark.parametrize("uri", ["kafka://billing.events.v1", "/tmp/slice.ndjson", "https://x/y"])
def test_a_scheme_other_than_file_is_refused(uri) -> None:
    with pytest.raises(FluxError) as caught:
        read_slice(uri)
    assert caught.value.code.code == "FLX-5035"


def test_a_remote_host_is_refused() -> None:
    with pytest.raises(FluxError):
        read_slice("file://elsewhere/tmp/slice.ndjson")


def test_an_unreadable_slice_is_refused(tmp_path) -> None:
    with pytest.raises(FluxError) as caught:
        read_slice((tmp_path / "missing.ndjson").as_uri())
    assert caught.value.code.code == "FLX-5035"


def test_a_binary_float_in_the_slice_is_refused(tmp_path) -> None:
    path = tmp_path / "s.ndjson"
    path.write_text('{"amount": 1.5}\n', encoding="utf-8")
    with pytest.raises(FluxError) as caught:
        read_slice(path.as_uri())
    assert caught.value.code.code == "FLX-5035"
    assert "floating-point" in caught.value.detail


def test_a_decimal_string_in_the_slice_is_read_exactly(tmp_path) -> None:
    path = tmp_path / "s.ndjson"
    path.write_text('{"amount": "1.5"}\n', encoding="utf-8")
    rows, _ = read_slice(path.as_uri())
    assert rows[0]["amount"] == "1.5"


@pytest.mark.parametrize("line", ["[1, 2]", '"a string"', "not json", "7"])
def test_a_line_that_is_not_one_object_is_refused(tmp_path, line) -> None:
    path = tmp_path / "s.ndjson"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(FluxError) as caught:
        read_slice(path.as_uri())
    assert caught.value.code.code == "FLX-5035"


def test_an_empty_slice_is_refused(tmp_path) -> None:
    path = tmp_path / "s.ndjson"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(FluxError):
        read_slice(path.as_uri())


def test_a_slice_above_records_max_is_refused(tmp_path, committed_rows) -> None:
    uri = write_slice(tmp_path / "s.ndjson", committed_rows)
    with pytest.raises(FluxError) as caught:
        read_slice(uri, maximum=len(committed_rows) - 1)
    assert caught.value.code.code == "FLX-5035"


# ---------------------------------------------------------------------------
# The committed state
# ---------------------------------------------------------------------------


def test_what_leaves_the_boundary_is_the_list_the_spec_names(committed) -> None:
    published = committed.to_document()
    assert set(published) == {
        "exposeId",
        "stateOrdinal",
        "G",
        "C1",
        "C2",
        "S",
        "recordCount",
        "committedAt",
        "slice",
    }


def test_the_published_state_carries_no_record_and_no_blinding_value(
    committed, committed_rows
) -> None:
    rendered = json.dumps(committed.to_document(), sort_keys=True)
    for row in committed_rows:
        assert row["subscriber_id"] not in rendered
    assert BLINDING.hex() not in rendered
    assert "blinding" not in rendered


def test_the_published_c2_is_the_one_the_boundary_took(committed, document) -> None:
    assert committed.c2 == document["expect"]["C2"]


def test_a_different_blinding_value_gives_a_different_commitment(committed, document) -> None:
    other = commitment_c2(committed.statistic, OTHER_BLINDING)
    assert other != committed.c2


def test_a_short_blinding_value_is_refused(committed) -> None:
    with pytest.raises(FluxError) as caught:
        commitment_c2(committed.statistic, b"\x00" * (MINIMUM_BLINDING_BYTES - 1))
    assert caught.value.code.code == "FLX-5035"


def test_every_declared_column_is_committed(committed, block) -> None:
    assert {entry.column for entry in committed.c1} == set(block.columns)
    assert [entry.column for entry in committed.c1] == sorted(block.columns)


def test_a_column_root_is_reachable_by_name(committed, block) -> None:
    name = sorted(block.columns)[0]
    assert committed.column_root(name) is not None
    assert committed.column_root("no-such-column") is None


def test_committing_the_same_records_twice_gives_one_state(document) -> None:
    compiled = build_compiled(document)
    first = build_committed(document, compiled)
    second = build_committed(document, compiled)
    assert first.to_document() == second.to_document()


def test_a_negative_state_ordinal_is_refused(document, compiled) -> None:
    with pytest.raises(FluxError) as caught:
        commit_state(
            rows=document["committedSlice"],
            compiled=compiled,
            ordinal=-1,
            committed_at=document["committedAt"],
            blinding=BLINDING,
            slice_reference=SliceReference(uri="file:///s", first_line=1, last_line=1),
        )
    assert caught.value.code.code == "FLX-5035"


def test_a_null_in_a_fail_column_refuses_the_commitment(document, compiled) -> None:
    rows = [dict(row) for row in document["committedSlice"]]
    rows[0]["market"] = None
    with pytest.raises(FluxError) as caught:
        commit_state(
            rows=rows,
            compiled=compiled,
            ordinal=1,
            committed_at=document["committedAt"],
            blinding=BLINDING,
            slice_reference=SliceReference(uri="file:///s", first_line=1, last_line=1),
        )
    assert caught.value.code.code == "FLX-5035"


def test_an_absent_column_refuses_the_commitment(document, compiled) -> None:
    rows = [{k: v for k, v in row.items() if k != "amount"} for row in document["committedSlice"]]
    with pytest.raises(FluxError) as caught:
        commit_state(
            rows=rows,
            compiled=compiled,
            ordinal=1,
            committed_at=document["committedAt"],
            blinding=BLINDING,
            slice_reference=SliceReference(uri="file:///s", first_line=1, last_line=1),
        )
    assert caught.value.code.code == "FLX-5035"


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def _store(committed, *, budget: int = 3) -> BoundaryStore:
    store = BoundaryStore(max_checks_per_state=budget)
    store.put(committed, BLINDING)
    return store


def test_the_check_counter_refuses_beyond_the_declared_budget(committed) -> None:
    store = _store(committed, budget=2)
    assert store.record_check(committed.ordinal, g=committed.g) == 1
    assert store.record_check(committed.ordinal, g=committed.g) == 2
    with pytest.raises(FluxError) as caught:
        store.record_check(committed.ordinal, g=committed.g)
    assert caught.value.code.code == "FLX-5010"


def test_the_check_counter_cannot_be_lowered(committed) -> None:
    """There is no accessor that sets it: only ``record_check`` moves it."""
    store = _store(committed)
    store.record_check(committed.ordinal, g=committed.g)
    assert store.checks_used(committed.ordinal) == 1
    setters = [name for name in dir(store) if name.startswith(("reset", "set_", "clear"))]
    assert setters == []


def test_a_check_under_a_different_g_is_refused(committed) -> None:
    store = _store(committed)
    with pytest.raises(FluxError) as caught:
        store.record_check(committed.ordinal, g="sha256:" + "0" * 64)
    assert caught.value.code.code == "FLX-5011"


def test_a_budget_below_one_check_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        BoundaryStore(max_checks_per_state=0)
    assert caught.value.code.code == "FLX-5010"


def test_a_state_ordinal_must_increment(committed) -> None:
    store = _store(committed)
    with pytest.raises(FluxError) as caught:
        store.put(committed, BLINDING)
    assert caught.value.code.code == "FLX-5035"


def test_a_lower_state_ordinal_is_refused(committed, document, compiled) -> None:
    store = _store(committed)
    earlier = commit_state(
        rows=document["committedSlice"],
        compiled=compiled,
        ordinal=committed.ordinal - 1,
        committed_at=document["committedAt"],
        blinding=BLINDING,
        slice_reference=SliceReference(uri="file:///s", first_line=1, last_line=1),
    )
    with pytest.raises(FluxError):
        store.put(earlier, BLINDING)


def test_a_short_blinding_value_is_refused_by_the_store(committed) -> None:
    store = BoundaryStore(max_checks_per_state=1)
    with pytest.raises(FluxError):
        store.put(committed, b"\x00")


def test_an_unknown_ordinal_is_refused(committed) -> None:
    store = _store(committed)
    for call in (
        lambda: store.get(99),
        lambda: store.record_check(99, g=committed.g),
        lambda: store.confirm_commitment(99, committed.c2),
        lambda: store.checks_used(99),
    ):
        with pytest.raises(FluxError) as caught:
            call()
        assert caught.value.code.code == "FLX-5035"


def test_the_store_confirms_a_commitment_without_handing_back_the_blinding_value(
    committed,
) -> None:
    store = _store(committed)
    assert store.confirm_commitment(committed.ordinal, committed.c2) is True
    assert store.confirm_commitment(committed.ordinal, "sha256:" + "0" * 64) is False


def test_the_store_repr_names_no_blinding_value(committed) -> None:
    rendered = repr(_store(committed))
    assert BLINDING.hex() not in rendered
    assert str(list(BLINDING)) not in rendered
    assert str(committed.ordinal) in rendered


def test_the_store_reports_its_ordinals_and_the_latest_state(committed) -> None:
    store = BoundaryStore(max_checks_per_state=1)
    assert store.latest is None
    assert store.ordinals == ()
    store.put(committed, BLINDING)
    assert store.ordinals == (committed.ordinal,)
    assert store.latest is committed
    assert store.snapshot() == {committed.ordinal: 0}


def test_a_state_past_its_declared_maximum_age_is_refused(committed, block) -> None:
    store = _store(committed)
    max_age = block.budget.max_state_age_ms
    assert max_age is not None
    assert (
        store.require_fresh(
            committed.ordinal, now_ms=committed.committed_at_ms + max_age, max_age_ms=max_age
        )
        == max_age
    )
    with pytest.raises(FluxError) as caught:
        store.require_fresh(
            committed.ordinal,
            now_ms=committed.committed_at_ms + max_age + 1,
            max_age_ms=max_age,
        )
    assert caught.value.code.code == "FLX-5012"


def test_a_state_dated_after_the_instant_it_is_checked_at_is_refused(committed) -> None:
    store = _store(committed)
    with pytest.raises(FluxError) as caught:
        store.require_fresh(
            committed.ordinal, now_ms=committed.committed_at_ms - 1, max_age_ms=None
        )
    assert caught.value.code.code == "FLX-5012"


def test_no_declared_maximum_age_means_no_age_refusal(committed) -> None:
    store = _store(committed)
    assert (
        store.require_fresh(
            committed.ordinal, now_ms=committed.committed_at_ms + 10**12, max_age_ms=None
        )
        == 10**12
    )
