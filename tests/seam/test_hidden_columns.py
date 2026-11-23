# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An emit's ``hiddenColumns``: the reader and everything it refuses (ADR 0024 section 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.emit import seam_digest, seam_reusable_digest
from flux_engine.seam.hidden import (
    HIDDEN_COLUMNS_KEY,
    HiddenColumn,
    fidelity_without_hidden,
    hidden_columns_of,
    read_hidden_columns,
    with_hidden_columns,
    with_hidden_in_schema,
)
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from tests.stages.test_hidden_columns_run import _rewrite, make_bundle

pytestmark = pytest.mark.unit

_CONTRACT = ("cu_id", "cu_age")


def _read(block: Any) -> Any:
    return read_hidden_columns(block, contract_columns=_CONTRACT, where="block", code=FLX_3105)


def test_the_entries_are_read_in_the_order_listed() -> None:
    read = _read([{"name": "signup", "type": "DATE"}, {"name": "tier", "type": "decimal(9,2)"}])
    assert read == (HiddenColumn("signup", "DATE"), HiddenColumn("tier", "decimal(9,2)"))


@pytest.mark.parametrize(
    "block",
    [
        {"name": "signup", "type": "DATE"},  # not a list
        "signup",
        [],  # empty
        ["signup"],  # an entry that is not a mapping
        [{"name": "signup"}],  # a missing key
        [{"type": "DATE"}],
        [{"name": "signup", "type": "DATE", "required": True}],  # an unknown key
        [{"name": "", "type": "DATE"}],  # an empty name
        [{"name": "  ", "type": "DATE"}],
        [{"name": 3, "type": "DATE"}],
        [{"name": "signup", "type": "DATE"}, {"name": "signup", "type": "INT"}],  # a duplicate
        [{"name": "signup", "type": "DATE"}, {"name": "signup", "type": "DATE"}],
        [{"name": "cu_age", "type": "INT"}],  # a contract column's name
        [{"name": "signup", "type": "WIDGET"}],  # a type that is not read
        [{"name": "signup", "type": ""}],
        [{"name": "signup", "type": 7}],
        [{"name": f"c{index}", "type": "INT"} for index in range(1025)],  # past the bound
    ],
)
def test_every_malformed_block_is_refused_with_flx_3105(block: Any) -> None:
    with pytest.raises(FluxError) as caught:
        _read(block)
    assert caught.value.code.code == "FLX-3105"
    assert "block" in str(caught.value)


def test_the_bound_is_inclusive() -> None:
    assert len(_read([{"name": f"c{index}", "type": "INT"} for index in range(1024)])) == 1024


def test_a_name_is_compared_in_nfc() -> None:
    with pytest.raises(FluxError):
        read_hidden_columns(
            [{"name": "café", "type": "INT"}],
            contract_columns=["café"],
            where="block",
            code=FLX_3105,
        )


def test_the_extension_hands_back_what_the_block_holds() -> None:
    extension = {HIDDEN_COLUMNS_KEY: [{"name": "signup", "type": "DATE"}], "rows": 3}
    assert hidden_columns_of(extension) == (HiddenColumn("signup", "DATE"),)
    assert hidden_columns_of({"rows": 3}) == () == hidden_columns_of(None)


def test_the_planned_expose_has_the_hidden_columns_after_the_written_ones() -> None:
    from flux_engine.seam.contract import compile_expose

    expose = compile_expose(
        {
            "exposeId": "t",
            "contract": {"schema": [{"name": "a", "type": "INT"}, {"name": "b", "type": "STRING"}]},
        }
    )
    planned = with_hidden_columns(expose, (HiddenColumn("h", "DATE"), HiddenColumn("g", "INT")))
    assert planned.column_names == ("a", "b", "h", "g")
    assert [column.hidden for column in planned.columns] == [False, False, True, True]
    assert planned.hidden_count == 2
    assert planned.written == expose and expose.hidden_count == 0
    assert with_hidden_columns(expose, ()) is expose


def test_the_contract_the_rules_read_holds_the_hidden_columns() -> None:
    contract = {
        "exposes": [
            {"exposeId": "t", "contract": {"schema": [{"name": "a", "type": "INT"}]}},
            {"exposeId": "u", "contract": {"schema": [{"name": "b", "type": "INT"}]}},
        ]
    }
    document = with_hidden_in_schema(contract, {"t": (HiddenColumn("h", "DATE"),)})
    assert [c["name"] for c in document["exposes"][0]["contract"]["schema"]] == ["a", "h"]
    assert [c["name"] for c in document["exposes"][1]["contract"]["schema"]] == ["b"]
    assert [c["name"] for c in contract["exposes"][0]["contract"]["schema"]] == ["a"]  # not edited


def test_the_fidelity_the_rows_are_checked_against_has_no_hidden_entry() -> None:
    block = {"columns": {"a": {"mean": "1"}, "h": {"range": {"min": "1", "max": "2"}}}}
    extension = {HIDDEN_COLUMNS_KEY: [{"name": "h", "type": "INT"}], "fidelity": block}
    assert fidelity_without_hidden(block, extension) == {"columns": {"a": {"mean": "1"}}}
    assert fidelity_without_hidden(block, {"fidelity": block}) is block


# ---------------------------------------------------------------------------
# Through a bundle
# ---------------------------------------------------------------------------


def _resolved(bundle: Path) -> List[Any]:
    loaded = load_bundle(bundle)
    return list(resolve_all_emits(loaded, simulation_id=sorted(loaded.simulation_ids())[0]))


def test_a_bundle_declaring_the_key_resolves_with_the_columns(tmp_path: Path) -> None:
    customer = _resolved(make_bundle(tmp_path))[0]
    assert customer.hidden_columns == (HiddenColumn("signup", "DATE"),)
    assert customer.compile().column_names == ("cu_id", "cu_signup", "cu_age")
    assert customer.compile_planned().column_names == ("cu_id", "cu_signup", "cu_age", "signup")
    assert customer.rules_document is not None


def test_a_bundle_without_the_key_resolves_as_it_did(tmp_path: Path) -> None:
    for emit in _resolved(make_bundle(tmp_path, hidden=False)):
        assert emit.hidden_columns == ()
        assert emit.rules_document is None and emit.planning_document is emit.contract_document
        assert emit.compile_planned() == emit.compile()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("{ name: signup, type: DATE }", "{ name: cu_age, type: DATE }"),
        ("{ name: signup, type: DATE }", "{ name: signup, type: WIDGET }"),
        ("{ name: signup, type: DATE }", "{ name: signup, type: DATE, nullable: true }"),
        ("{ name: signup, type: DATE }", "{ name: signup }"),
    ],
)
def test_a_bundle_the_reader_refuses_is_flx_3105(tmp_path: Path, old: str, new: str) -> None:
    bundle = make_bundle(tmp_path)
    _rewrite(bundle, old, new)
    with pytest.raises(FluxError) as caught:
        _resolved(bundle)
    assert caught.value.code.code == "FLX-3105"
    assert "hiddenColumns" in str(caught.value)


def test_the_block_that_holds_a_hidden_column_hashes_into_h_and_not_into_g(tmp_path: Path) -> None:
    emit = _resolved(make_bundle(tmp_path))[0]
    plain = _resolved(make_bundle(tmp_path, hidden=False, name="plain"))[0]
    names = emit.compile().column_names
    with_h = seam_digest(emit.contract_document, emit.emit, names, extension=emit.extension)
    assert with_h != seam_digest(
        plain.contract_document, plain.emit, names, extension=plain.extension
    )
    # Without the member, and with the one statistic it adds taken out, the two are one document.
    stripped = {k: v for k, v in dict(emit.extension).items() if k != HIDDEN_COLUMNS_KEY}
    assert seam_reusable_digest(
        emit.contract_document, emit.emit, names, extension=emit.extension
    ) == seam_reusable_digest(emit.contract_document, emit.emit, names, extension=stripped)


@pytest.mark.parametrize("value", [False, None, "true", 1, {}, []])
def test_hidden_takes_only_true(value: Any) -> None:
    from flux_engine.seam.hidden import read_hidden_value

    assert read_hidden_value(True, where="block", code=FLX_3105) is True
    with pytest.raises(FluxError) as caught:
        read_hidden_value(value, where="block", code=FLX_3105)
    assert caught.value.code.code == "FLX-3105"


def test_g_leaves_the_hidden_member_out_and_h_keeps_it(tmp_path: Path) -> None:
    emit = _resolved(make_bundle(tmp_path, hidden=False))[0]
    names = emit.compile().column_names
    extension = dict(emit.extension or {})
    hidden = {**extension, "hidden": True}
    assert seam_digest(emit.contract_document, emit.emit, names, extension=hidden) != seam_digest(
        emit.contract_document, emit.emit, names, extension=extension
    )
    assert seam_reusable_digest(
        emit.contract_document, emit.emit, names, extension=hidden
    ) == seam_reusable_digest(emit.contract_document, emit.emit, names, extension=extension)
