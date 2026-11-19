# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Drive every published vector in ``tests/vectors/expr-vectors.json``.

EXPR-SPEC §5 makes the vectors file the pinned, publishable statement of what
``flux-expr/1`` does: "Each vector: ``{expr, scope, model_fragment, expect:
{parse: ast|error_code, eval?: {row, result}}}``. The v0.1 set covers: the
four shipped example expressions; each operator; precedence; each method; each
error code; null rules; NFC/NFD identifier equivalence ...; the reserved
``matches``." The final test in this module asserts that coverage claim
mechanically, so the file cannot quietly lose a category.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Dict

import pytest

from flux_engine.expr import compile_expr, make_view, parse, to_json, unparse
from flux_engine.expr.ast import COMPARISON_OPERATORS, METHOD_NAMES
from flux_engine.expr.check import check
from flux_engine.expr.errors import CATALOG, FluxExprError

from .conftest import VECTORS_PATH, decode_value, model_for, vector_cases

VECTORS = list(vector_cases())
PARAMETERS = [pytest.param(vector, id=vector_id) for vector_id, vector in VECTORS]


@pytest.mark.golden
@pytest.mark.parametrize("vector", PARAMETERS)
def test_vector(vector: Dict[str, Any]) -> None:
    """Each vector: parse, canonical text, static check, then every eval case."""
    model = model_for(vector)
    expect = vector["expect"]
    expected_parse = expect["parse"]

    if isinstance(expected_parse, str):
        # A parse-time failure: the code is the whole expectation.
        with pytest.raises(FluxExprError) as raised:
            parse(vector["expr"])
        assert raised.value.code == expected_parse
        return

    node = parse(vector["expr"])
    assert to_json(node) == expected_parse

    expected_check = expect["check"]
    if expected_check != "ok":
        with pytest.raises(FluxExprError) as raised:
            check(node, model)
        assert raised.value.code == expected_check
        return

    assert unparse(node) == expect["unparse"]
    predicate = compile_expr(node, model)

    for case in expect.get("eval", []):
        row = {
            name: decode_value(value, model.attributes[_resolve(model, name)])
            for name, value in case["row"].items()
        }
        view = make_view(model, row)
        assert predicate(view) is case["result"], case


def _resolve(model: Any, name: str) -> str:
    canonical = model.resolve(name)
    assert canonical is not None, f"vector row names {name!r}, which the model does not declare"
    return str(canonical)


@pytest.mark.golden
def test_every_vector_id_is_unique() -> None:
    ids = [vector_id for vector_id, _ in VECTORS]
    assert len(ids) == len(set(ids))


@pytest.mark.golden
def test_the_file_carries_no_json_numbers() -> None:
    """KERNEL-SPEC §2.1: no ``float`` anywhere in a document that gets hashed.

    Row values and literals are encoded as strings, so a JSON number appearing
    in this file at all would mean an encoding slipped.
    """

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        assert not isinstance(node, (int, float)), f"JSON number in the vectors file: {node!r}"
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(VECTORS_PATH.read_text(encoding="utf-8")))


@pytest.mark.golden
def test_the_published_set_covers_every_category_the_spec_names() -> None:
    """EXPR-SPEC §5's coverage list, asserted rather than assumed."""
    ids = {vector_id for vector_id, _ in VECTORS}
    expressions = {vector["expr"] for _, vector in VECTORS}

    shipped = {vector_id for vector_id in ids if vector_id.startswith("shipped-")}
    assert len(shipped) == 4, "the four shipped example expressions"

    for operator in COMPARISON_OPERATORS + ("&&", "||", "!", "in"):
        assert any(
            operator in expression for expression in expressions
        ), f"no vector uses {operator}"

    for method in METHOD_NAMES:
        assert any(f".{method}(" in expression for expression in expressions), method

    covered_codes = set()
    for _, vector in VECTORS:
        expect = vector["expect"]
        for value in (expect["parse"], expect.get("check")):
            if isinstance(value, str) and value.startswith("FLX-"):
                covered_codes.add(value)
    assert covered_codes == set(CATALOG), "every FLX-6nnn code needs a vector"

    assert any(vector_id.startswith("precedence-") for vector_id in ids)
    assert any(vector_id.startswith("null-") for vector_id in ids)
    assert any(vector_id.startswith("unicode-") for vector_id in ids)
    assert any("matches" in vector["expr"] for _, vector in VECTORS)


@pytest.mark.golden
def test_the_nfc_pair_is_a_real_composed_decomposed_pair() -> None:
    """The NFC/NFD vectors must actually differ in their bytes, or they pin nothing."""
    by_id = {vector_id: vector for vector_id, vector in VECTORS}
    composed = by_id["unicode-identifier-composed"]["expr"]
    decomposed = by_id["unicode-identifier-decomposed-names-the-same-path"]["expr"]

    assert composed != decomposed
    assert unicodedata.normalize("NFC", decomposed) == composed
    assert by_id["unicode-identifier-composed"]["expect"]["parse"] == (
        by_id["unicode-identifier-decomposed-names-the-same-path"]["expect"]["parse"]
    )
