# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The type system and the AST's own guards.

These are the invariants the rest of the package is allowed to assume: a
``Type`` cannot be malformed, a ``ScopeModel``'s sugar cannot shadow a
canonical path, and an AST node cannot be built with an operator or a method
name outside the closed sets. Nothing here goes through the parser -- the
point is that the data structures defend themselves even when a caller builds
them by hand.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from flux_engine.expr.ast import (
    And,
    BoolLit,
    Compare,
    ListLit,
    Method,
    Node,
    Not,
    NullLit,
    NumberLit,
    Path,
    StringLit,
    to_json,
    unparse,
)
from flux_engine.expr.check import check, operand_type
from flux_engine.expr.compile import compile_expr
from flux_engine.expr.errors import FluxExprError
from flux_engine.expr.types import (
    BOOL,
    NUMBER,
    SET_STRING,
    STRING,
    Scope,
    ScopeModel,
    Type,
    TypeKind,
    enum_of,
    nfc,
)

# --- Type ---------------------------------------------------------------------


@pytest.mark.unit
def test_the_five_types_are_the_whole_set() -> None:
    """EXPR-SPEC §2: "Five static types"."""
    assert {kind.value for kind in TypeKind} == {
        "bool",
        "number",
        "string",
        "set<string>",
        "enum",
    }


@pytest.mark.unit
def test_an_enum_needs_levels_and_nothing_else_may_have_them() -> None:
    with pytest.raises(ValueError):
        Type(TypeKind.ENUM)
    with pytest.raises(ValueError):
        Type(TypeKind.STRING, ("a",))


@pytest.mark.unit
def test_enum_levels_are_nfc_deduplicated_and_sorted() -> None:
    declared = enum_of(["b", "a", "b", "café"])
    assert declared.levels == ("a", "b", "café")
    assert enum_of(["a", "b"]) == enum_of(["b", "a"]), "level order is not significant"


@pytest.mark.unit
def test_a_type_renders_its_name_for_an_error_message() -> None:
    assert str(NUMBER) == "number"
    assert str(SET_STRING) == "set<string>"
    assert str(enum_of(["b", "a"])) == "enum<a|b>"
    assert BOOL.to_json() == {"type": "bool"}
    assert enum_of(["a"]).to_json() == {"type": "enum", "levels": ["a"]}


@pytest.mark.unit
def test_nfc_is_the_one_normalisation() -> None:
    assert nfc("café") == "café"


# --- ScopeModel ---------------------------------------------------------------


@pytest.mark.unit
def test_an_alias_must_point_at_a_declared_path() -> None:
    with pytest.raises(ValueError) as raised:
        ScopeModel(Scope.SUBJECT, {"a": NUMBER}, {"b": "nope"})
    assert "unknown path" in str(raised.value)


@pytest.mark.unit
def test_an_alias_may_not_shadow_a_canonical_path() -> None:
    with pytest.raises(ValueError) as raised:
        ScopeModel(Scope.SUBJECT, {"a": NUMBER, "b": STRING}, {"a": "b"})
    assert "shadows" in str(raised.value)


@pytest.mark.unit
def test_a_scope_models_mappings_are_read_only_and_nfc_keyed() -> None:
    model = ScopeModel(Scope.SUBJECT, {"café": NUMBER})
    assert list(model.attributes) == ["café"]
    with pytest.raises(TypeError):
        model.attributes["x"] = NUMBER  # type: ignore[index]


@pytest.mark.unit
def test_resolve_prefers_a_canonical_path_over_sugar() -> None:
    model = ScopeModel(Scope.SUBJECT, {"subject.a": NUMBER}, {"a": "subject.a"})
    assert model.resolve("subject.a") == "subject.a"
    assert model.resolve("a") == "subject.a"
    assert model.resolve("nope") is None
    assert model.type_of("nope") is None


@pytest.mark.unit
def test_a_scope_model_json_fragment_is_sorted() -> None:
    model = ScopeModel(Scope.EVENT, {"b": NUMBER, "a": STRING}, {"c": "a"})
    fragment = model.to_json()
    assert list(fragment["paths"]) == ["a", "b"]
    assert fragment["aliases"] == {"c": "a"}


# --- AST guards ----------------------------------------------------------------


@pytest.mark.unit
def test_a_path_has_at_least_one_part() -> None:
    with pytest.raises(ValueError):
        Path(())


@pytest.mark.unit
def test_an_operator_outside_the_closed_set_cannot_be_built() -> None:
    with pytest.raises(ValueError) as raised:
        Compare("=~", Path(("a",)), NumberLit(Decimal("1")))
    assert "comparison operator" in str(raised.value)


@pytest.mark.unit
def test_a_method_name_outside_the_closed_set_cannot_be_built() -> None:
    with pytest.raises(ValueError) as raised:
        Method(Path(("a",)), "matches", (StringLit("x"),))
    assert "not a flux-expr/1 method" in str(raised.value)


@pytest.mark.unit
def test_to_json_and_unparse_refuse_a_node_they_do_not_know() -> None:
    class Alien(Node):
        pass

    with pytest.raises(TypeError):
        to_json(Alien())
    with pytest.raises(TypeError):
        unparse(Alien())


@pytest.mark.unit
def test_the_leaf_nodes_round_trip_through_json() -> None:
    assert to_json(NullLit()) == {"node": "null"}
    assert to_json(BoolLit(False)) == {"node": "bool", "value": False}
    assert to_json(NumberLit(Decimal("1.50"))) == {"node": "number", "value": "1.50"}
    assert to_json(ListLit(())) == {"node": "list", "items": []}


# --- hand-built ASTs the grammar cannot produce ----------------------------------


@pytest.mark.unit
def test_the_checker_refuses_an_operand_where_a_predicate_belongs() -> None:
    """A hand-built AST is still typed: ``check`` is the gate, not the parser."""
    model = ScopeModel(Scope.SUBJECT, {"n": NUMBER, "flag": BOOL})
    for node in (Path(("n",)), NumberLit(Decimal("1")), StringLit("a"), NullLit()):
        with pytest.raises(FluxExprError) as raised:
            check(node, model)  # type: ignore[arg-type]
        assert raised.value.code == "FLX-6005"


@pytest.mark.unit
def test_a_boolean_connective_over_a_non_predicate_is_flx_6005() -> None:
    model = ScopeModel(Scope.SUBJECT, {"n": NUMBER})
    hand_built = And(Path(("n",)), BoolLit(True))  # type: ignore[arg-type]
    with pytest.raises(FluxExprError) as raised:
        check(hand_built, model)
    assert raised.value.code == "FLX-6005"

    negated = Not(Path(("n",)))  # type: ignore[arg-type]
    with pytest.raises(FluxExprError) as raised:
        check(negated, model)
    assert raised.value.code == "FLX-6005"


@pytest.mark.unit
def test_the_compiler_refuses_what_the_checker_would_have_refused() -> None:
    model = ScopeModel(Scope.SUBJECT, {"n": NUMBER})
    with pytest.raises(FluxExprError):
        compile_expr(Path(("n",)), model)  # type: ignore[arg-type]


@pytest.mark.unit
def test_operand_type_refuses_something_that_is_not_an_operand() -> None:
    model = ScopeModel(Scope.SUBJECT, {"n": NUMBER})
    with pytest.raises(FluxExprError) as raised:
        operand_type(Method(Path(("n",)), "isNull", ()), model)  # type: ignore[arg-type]
    assert raised.value.code == "FLX-6005"
