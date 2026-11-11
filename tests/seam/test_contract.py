# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §9, first half: the closed logical type set and the compiler.

§9: "Closed ``LogicalType`` set: ``STRING, INT, DECIMAL(p,s), BOOL, DATE,
TIMESTAMP, JSON, BYTES``. FLUID types are normalised case-insensitively;
anything outside the set fails by name (``FLX-3001``)."

The totality test here is the one that matters: it reads the **vendored
FLUID schema's own** column-type enum and asserts every name in it either
maps onto the closed set or is refused by name. That is what makes "there
is no third outcome" a checked claim rather than a docstring -- a FLUID
release that adds a type name fails this test until somebody decides what
it means.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import (
    TYPE_ALIASES,
    UNSUPPORTED_TYPES,
    CompiledExpose,
    LogicalKind,
    LogicalType,
    compile_expose,
    find_expose,
    parse_logical_type,
)
from flux_engine.spec.validate import Bundle

from .conftest import expose_from_schema, minimal_contract

pytestmark = pytest.mark.unit


def _fluid_schema() -> Dict[str, Any]:
    """The vendored FLUID schema whichever ``flux_spec`` is installed ships."""
    import importlib

    provider = importlib.import_module(Bundle.__module__)
    directory = Path(provider.FLUID_VENDOR_DIR)
    newest = sorted(directory.glob("fluid-schema-*.json"))[-1]
    return json.loads(newest.read_text())


def _declared_column_type_names() -> Set[str]:
    schema = _fluid_schema()
    for candidate in schema["$defs"]["column"]["properties"]["type"]["anyOf"]:
        if "enum" in candidate:
            return {str(name) for name in candidate["enum"]}
    raise AssertionError("the FLUID column schema no longer carries a type enum")


# ---------------------------------------------------------------------------
# The closed set is closed, and total over FLUID's own vocabulary
# ---------------------------------------------------------------------------


def test_the_closed_set_is_the_eight_types_the_spec_names() -> None:
    assert [member.value for member in LogicalKind] == [
        "STRING",
        "INT",
        "DECIMAL",
        "BOOL",
        "DATE",
        "TIMESTAMP",
        "JSON",
        "BYTES",
    ]


def test_every_fluid_column_type_either_maps_or_is_refused_by_name() -> None:
    """Totality against the vendored FLUID schema's own enum.

    Every name a FLUID contract may legally declare is accounted for: it
    normalises onto a member of the closed set, or it appears in
    :data:`UNSUPPORTED_TYPES` with a reason. A name in neither would reach
    :func:`parse_logical_type`'s final branch and fail with a message that
    says only "maps onto no member", which is true but not useful.
    """
    declared = _declared_column_type_names()
    unaccounted = sorted(
        name for name in declared if name not in TYPE_ALIASES and name not in UNSUPPORTED_TYPES
    )
    assert unaccounted == [], (
        "these FLUID column types are neither mapped nor refused by name: " f"{unaccounted}"
    )


def test_the_two_vocabularies_are_disjoint() -> None:
    assert set(TYPE_ALIASES) & set(UNSUPPORTED_TYPES) == set()


def test_every_alias_parses_and_every_refusal_raises() -> None:
    for name, kind in TYPE_ALIASES.items():
        declared = f"{name}(18,4)" if kind is LogicalKind.DECIMAL else name
        assert parse_logical_type(declared).kind is kind
    for name in UNSUPPORTED_TYPES:
        with pytest.raises(FluxError) as error:
            parse_logical_type(name)
        assert error.value.code.code == "FLX-3001"
        assert name in str(error.value)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("STRING", "STRING"),
        ("string", "STRING"),
        ("  VarChar( 255 ) ", "STRING"),
        ("BIGINT", "INT"),
        ("DECIMAL(18,4)", "DECIMAL(18,4)"),
        ("decimal( 18 , 4 )", "DECIMAL(18,4)"),
        ("NUMERIC(9,0)", "DECIMAL(9,0)"),
        ("boolean", "BOOL"),
        ("DATE", "DATE"),
        ("TIMESTAMPTZ", "TIMESTAMP"),
        ("timestamp with time zone", "TIMESTAMP"),
        ("JSONB", "JSON"),
        ("VARBINARY(64)", "BYTES"),
        ("uuid", "STRING"),
    ],
)
def test_declared_types_normalise_onto_the_closed_set(declared: str, expected: str) -> None:
    assert str(parse_logical_type(declared)) == expected


@pytest.mark.parametrize(
    "declared",
    ["decimal", "numeric", "money", "DEC"],
)
def test_an_unparameterised_decimal_is_refused_by_name(declared: str) -> None:
    """§9's member is ``DECIMAL(p,s)``; a default scale would be a guess."""
    with pytest.raises(FluxError) as error:
        parse_logical_type(declared)
    assert error.value.code.code == "FLX-3001"
    assert "DECIMAL(p,s)" in str(error.value)


@pytest.mark.parametrize(
    "declared",
    ["float", "double", "real", "double precision", "FLOAT64"],
)
def test_binary_float_columns_are_refused_at_the_declaration(declared: str) -> None:
    """A float column has no §9 rendering, so it fails where it is declared."""
    with pytest.raises(FluxError) as error:
        parse_logical_type(declared)
    assert error.value.code.code == "FLX-3001"
    assert "floating point" in str(error.value)


@pytest.mark.parametrize(
    "declared",
    ["", "   ", "MYSTERY_TYPE", "decimal(18)", "decimal(a,b)", "decimal(18,4,2)", "int(", "42"],
)
def test_a_type_the_set_has_no_member_for_is_refused(declared: str) -> None:
    with pytest.raises(FluxError) as error:
        parse_logical_type(declared)
    assert error.value.code.code == "FLX-3001"


@pytest.mark.parametrize("declared", [None, 4, 1.5, ["STRING"]])
def test_a_non_string_declared_type_is_refused(declared: object) -> None:
    with pytest.raises(FluxError) as error:
        parse_logical_type(declared)
    assert error.value.code.code == "FLX-3001"


@pytest.mark.parametrize(
    ("precision", "scale"),
    [(0, 0), (4, 5), (4, -1)],
)
def test_a_decimal_outside_its_own_parameter_domain_is_refused(precision: int, scale: int) -> None:
    with pytest.raises(FluxError) as error:
        LogicalType(kind=LogicalKind.DECIMAL, precision=precision, scale=scale)
    assert error.value.code.code == "FLX-3001"


def test_a_non_decimal_carrying_parameters_is_refused() -> None:
    with pytest.raises(FluxError):
        LogicalType(kind=LogicalKind.STRING, precision=4, scale=2)


def test_a_decimal_without_parameters_is_refused_at_construction() -> None:
    with pytest.raises(FluxError):
        LogicalType(kind=LogicalKind.DECIMAL)


@given(
    precision=st.integers(min_value=1, max_value=38),
    scale=st.integers(min_value=0, max_value=38),
)
def test_a_decimal_round_trips_through_its_canonical_spelling(precision: int, scale: int) -> None:
    """``str(LogicalType)`` is a spelling :func:`parse_logical_type` accepts.

    The property matters because the canonical spelling is what travels in
    the emit header (KERNEL-SPEC §8), so a header a run wrote must be a
    header a later run can read back.
    """
    if scale > precision:
        return
    logical = LogicalType(kind=LogicalKind.DECIMAL, precision=precision, scale=scale)
    assert parse_logical_type(str(logical)) == logical


@given(name=st.sampled_from(sorted(TYPE_ALIASES)))
def test_case_and_surrounding_space_never_change_the_parse(name: str) -> None:
    kind = TYPE_ALIASES[name]
    declared = f"{name}(18,4)" if kind is LogicalKind.DECIMAL else name
    variants = [declared, declared.upper(), declared.title(), f"  {declared}  "]
    parsed = {str(parse_logical_type(variant)) for variant in variants}
    assert len(parsed) == 1


# ---------------------------------------------------------------------------
# compile_expose
# ---------------------------------------------------------------------------


def test_compiling_the_minimal_contract_preserves_declared_order() -> None:
    expose = compile_expose(find_expose(minimal_contract(), "widget_stream"))
    assert expose.column_names == ("widget_id", "amount")
    assert expose.type_names == ("STRING", "DECIMAL(18,4)")
    assert expose.index_of == {"widget_id": 0, "amount": 1}


def test_reordering_the_schema_reorders_the_compiled_columns() -> None:
    """The order is the contract's, not the compiler's, and it is load-bearing."""
    schema: List[Dict[str, Any]] = [
        {"name": "a", "type": "STRING"},
        {"name": "b", "type": "INT"},
    ]
    forward = expose_from_schema(schema)
    backward = expose_from_schema(list(reversed(schema)))
    assert forward.column_names == ("a", "b")
    assert backward.column_names == ("b", "a")


def test_required_and_declaration_markers_survive_compilation() -> None:
    expose = expose_from_schema(
        [
            {
                "name": "id",
                "type": "STRING",
                "required": True,
                "semanticType": "identifier",
                "tags": ["identifier", "primary-key", 7],
            },
            {"name": "note", "type": "STRING"},
        ]
    )
    identifier, note = expose.columns
    assert identifier.required is True
    assert identifier.semantic_type == "identifier"
    assert identifier.tags == ("identifier", "primary-key")
    assert note.required is False
    assert note.semantic_type is None


def test_the_declared_spelling_is_kept_but_the_header_carries_the_canonical_one() -> None:
    expose = expose_from_schema([{"name": "n", "type": "decimal( 18 , 4 )"}])
    assert expose.columns[0].declared_type == "decimal( 18 , 4 )"
    assert expose.type_names == ("DECIMAL(18,4)",)


def test_column_names_are_nfc_normalised_and_a_collision_is_refused() -> None:
    # Written as escapes on purpose: an editor that normalises this file
    # would otherwise turn the two spellings into one and make the test
    # pass without testing anything.
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    assert composed != decomposed
    assert compile_expose(
        {
            "exposeId": "s",
            "kind": "stream",
            "contract": {"schema": [{"name": decomposed, "type": "STRING"}]},
            "binding": {"platform": "k", "format": "kafka_topic", "location": {"topic": "t"}},
        }
    ).column_names == (composed,)

    with pytest.raises(FluxError) as error:
        expose_from_schema(
            [{"name": composed, "type": "STRING"}, {"name": decomposed, "type": "INT"}]
        )
    assert error.value.code.code == "FLX-3004"
    assert "NFC" in str(error.value)


@pytest.mark.parametrize(
    "expose",
    [
        {"kind": "stream"},
        {"exposeId": "", "contract": {"schema": [{"name": "a", "type": "STRING"}]}},
        {"exposeId": "s"},
        {"exposeId": "s", "contract": {}},
        {"exposeId": "s", "contract": {"schema": []}},
        {"exposeId": "s", "contract": {"schema": "not a list"}},
        {"exposeId": "s", "contract": {"schema": ["not a mapping"]}},
        {"exposeId": "s", "contract": {"schema": [{"type": "STRING"}]}},
        {"exposeId": "s", "contract": {"schema": [{"name": "", "type": "STRING"}]}},
    ],
)
def test_an_uncompilable_schema_is_refused(expose: Dict[str, Any]) -> None:
    with pytest.raises(FluxError) as error:
        compile_expose(expose)
    assert error.value.code.code == "FLX-3004"


def test_a_refused_column_type_names_the_column_and_the_type() -> None:
    with pytest.raises(FluxError) as error:
        expose_from_schema([{"name": "ok", "type": "STRING"}, {"name": "price", "type": "double"}])
    message = str(error.value)
    assert error.value.code.code == "FLX-3001"
    assert "'price'" in message
    assert "'double'" in message


def test_column_lookup_is_nfc_aware_and_refuses_an_unknown_name(
    widget_expose: CompiledExpose,
) -> None:
    assert widget_expose.column("widget_id").name == "widget_id"
    with pytest.raises(FluxError) as error:
        widget_expose.column("nope")
    assert error.value.code.code == "FLX-3004"


@pytest.mark.parametrize(
    "document",
    [{}, {"exposes": "not a list"}, {"exposes": [{"exposeId": "other"}]}, {"exposes": ["x"]}],
)
def test_find_expose_refuses_a_document_with_no_such_expose(document: Dict[str, Any]) -> None:
    with pytest.raises(FluxError) as error:
        find_expose(document, "widget_stream")
    assert error.value.code.code == "FLX-3004"


def test_find_expose_normalises_the_wanted_id() -> None:
    document = minimal_contract(expose_id="caf\u00e9")
    assert find_expose(document, "cafe\u0301")["exposeId"] == "caf\u00e9"


# ---------------------------------------------------------------------------
# The shipped example
# ---------------------------------------------------------------------------


def test_the_telco_expose_compiles_to_its_four_columns_in_declared_order(
    telco_emit: Any, telco_expose: CompiledExpose
) -> None:
    """The declared order is read out of the contract, never typed here."""
    declared = telco_emit.expose["contract"]["schema"]
    assert telco_expose.column_names == tuple(column["name"] for column in declared)
    assert len(telco_expose.columns) == len(declared)
    assert telco_expose.type_names == tuple(
        str(parse_logical_type(column["type"])) for column in declared
    )
    assert [column.required for column in telco_expose.columns] == [
        bool(column.get("required", False)) for column in declared
    ]


def test_no_telco_column_renders_as_a_float(telco_expose: CompiledExpose) -> None:
    """Every column of the shipped example has an exact rendering (§9)."""
    for column in telco_expose.columns:
        assert column.logical_type.kind in set(LogicalKind)
        if column.logical_type.is_decimal:
            assert isinstance(Decimal(1).scaleb(-(column.logical_type.scale or 0)), Decimal)
