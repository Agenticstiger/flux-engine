# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Drive the flux-expr/2 vector files (EXPR-SPEC-2 §11).

``fluid-sql-vectors.json`` is forge-cli's FLUID SQL/1 conformance file,
vendored unchanged: the vectors define the subset, neither parser does
(RFC-data-rules.md §2.9), so flux-expr/2 passes every one.
``expr2-core-vectors.json`` holds flux-engine's further core vectors in the
same shape, each refusal with the FLX code FLUX reports. ``expr2-vectors.json``
holds the four extensions, in the shape of ``expr-vectors.json`` (``expr``,
``model_fragment``, ``expect.parse``/``check``/``unparse``/``eval``), which
also pins whether the parser or the check refuses a text. For every vector:
the verdict and code, the syntax tree, the type and scale, the canonical
text, that the canonical text parses back to the same tree, and every value.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Tuple

import pytest

from flux_engine.expr2 import MAX_DEPTH, MAX_ROUND_SCALE, Expr2Error, check, parse, to_json
from flux_engine.expr2.ast import CORE_FUNCTIONS, CORE_KEYWORDS
from flux_engine.expr2.errors import CATALOG
from flux_engine.expr2.lexer import MAX_LENGTH

from .support import (
    CORE_VECTORS,
    EXTENSION_VECTORS,
    REASON_CODES,
    SHARED_SHA256,
    SHARED_VECTORS,
    DictRow,
    load,
    matches,
    models_of,
    normal,
    rows_of,
)

SHARED = load(SHARED_VECTORS)
CORE = load(CORE_VECTORS)
EXTENSIONS = load(EXTENSION_VECTORS)
DOCUMENTS = {"shared": SHARED, "core": CORE, "extensions": EXTENSIONS}
MODELS = {name: models_of(document) for name, document in DOCUMENTS.items()}

#: The core's node kinds, as forge-cli's file names them in its ``nodes``.
CORE_NODES = {
    "number",
    "string",
    "bool",
    "null",
    "date",
    "column",
    "neg",
    "arith",
    "compare",
    "in",
    "between",
    "is_null",
    "distinct_from",
    "not",
    "and",
    "or",
    "case",
    "coalesce",
    "round",
    "date_diff",
}


def _all_vectors() -> List[Tuple[str, Dict[str, Any]]]:
    return [
        (name, normal(vector))
        for name, document in DOCUMENTS.items()
        for vector in document["vectors"]
    ]


ALL = _all_vectors()


def _check(name: str, vector: Mapping[str, Any], parsed: Any) -> Any:
    return check(
        parsed,
        MODELS[name][vector["model"]],
        vector["expose"],
        position=vector["position"],
        target=vector["target"],
    )


def _evaluate(name: str, vector: Mapping[str, Any], checked: Any) -> None:
    document = DOCUMENTS[name]
    model = MODELS[name][vector["model"]]
    for case in vector["eval"]:
        if "data" in case:
            row = rows_of(document, MODELS[name], case["data"]).row(
                vector["expose"], int(case["row"])
            )
        else:
            row = DictRow(model, vector["expose"], case["row"], case.get("links", {}))
        got = checked.evaluate(row)
        assert matches(case["result"], got), (case, got)


@pytest.mark.golden
@pytest.mark.parametrize(
    ("name", "vector"), [pytest.param(n, v, id=f"{n}:{v['id']}") for n, v in ALL]
)
def test_vector(name: str, vector: Dict[str, Any]) -> None:
    side = vector["side"]
    if vector["step"] == "parse":
        with pytest.raises(Expr2Error) as raised:
            parse(vector["text"], side=side)
        assert raised.value.code.code in vector["refused"]
        return
    if vector["refused"] is not None:
        with pytest.raises(Expr2Error) as raised:
            parsed = parse(vector["text"], side=side)
            if vector["tree"] is not None:
                assert to_json(parsed.tree) == vector["tree"]
            _check(name, vector, parsed)
        assert raised.value.code.code in vector["refused"], raised.value
        return
    parsed = parse(vector["text"], side=side)
    assert to_json(parsed.tree) == vector["tree"]
    checked = _check(name, vector, parsed)
    assert (None if checked.type.kind is None else checked.type.kind.value) == vector["type"]
    if vector["type"] == "number":
        scale = checked.type.scale
        assert (None if scale is None else str(scale)) == vector["scale"]
    assert checked.canonical == vector["canonical"]
    again = parse(checked.canonical, side=side)
    assert again.tree == parsed.tree
    assert _check(name, vector, again).canonical == checked.canonical
    _evaluate(name, vector, checked)


@pytest.mark.golden
def test_the_shared_file_is_forge_clis_copy_unchanged() -> None:
    # A re-vendored file updates SHARED_SOURCE and SHARED_SHA256 in support.py.
    assert hashlib.sha256(SHARED_VECTORS.read_bytes()).hexdigest() == SHARED_SHA256
    assert SHARED["language"] == "FLUID SQL/1"


@pytest.mark.golden
def test_the_shared_file_describes_this_parser() -> None:
    assert set(SHARED["keywords"]) == CORE_KEYWORDS
    assert set(SHARED["functions"]) == CORE_FUNCTIONS
    limits = SHARED["limits"]
    assert (limits["maxCodePoints"], limits["maxDepth"], limits["maxRoundPlaces"]) == (
        str(MAX_LENGTH),
        str(MAX_DEPTH),
        str(MAX_ROUND_SCALE),
    )
    assert set(SHARED["nodes"]) == CORE_NODES
    assert set(SHARED["reasonCodes"]) == set(REASON_CODES)
    # flux-engine's own core file speaks the same closed set of reasons.
    reasons = {
        v["expect"]["reason"] for v in CORE["vectors"] if v["expect"]["verdict"] == "refused"
    }
    assert reasons <= set(SHARED["reasonCodes"])


@pytest.mark.golden
def test_every_vector_id_is_unique() -> None:
    # Within each file, and across flux-engine's own two. forge-cli's file is a
    # namespace of its own, which a re-vendored copy may grow at any time.
    for name in DOCUMENTS:
        ids = [vector["id"] for n, vector in ALL if n == name]
        assert len(ids) == len(set(ids)), name
    own = [vector["id"] for n, vector in ALL if n != "shared"]
    assert len(own) == len(set(own)), sorted({i for i in own if own.count(i) > 1})


@pytest.mark.golden
@pytest.mark.parametrize(
    "path", [SHARED_VECTORS, CORE_VECTORS, EXTENSION_VECTORS], ids=lambda p: p.name
)
def test_no_json_number_appears(path: Any) -> None:
    def walk(value: Any) -> None:
        assert not isinstance(value, float)
        assert isinstance(value, bool) or not isinstance(value, int), value
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.golden
def test_every_language_code_has_a_refused_vector() -> None:
    # flux-engine's own files give each refusal's code exactly.
    raised = {
        code
        for name, vector in ALL
        if name != "shared" and vector["refused"] is not None
        for code in vector["refused"]
    }
    # FLX-6101, FLX-6102, FLX-6201 and FLX-6209 are about rule entries, not texts
    # (test_rules.py), FLX-6112 about writing a row (test_check.py), and FLX-6202 to
    # FLX-6204, FLX-6208 and FLX-6210 about what a rule generates, which a plan decides
    # (tests/generator/test_rules.py).
    not_text = {"FLX-6101", "FLX-6102", "FLX-6112", "FLX-6201", "FLX-6209"}
    not_text |= {"FLX-6202", "FLX-6203", "FLX-6204", "FLX-6208", "FLX-6210"}
    text_codes = set(CATALOG) - not_text
    assert text_codes <= raised, sorted(text_codes - raised)


@pytest.mark.golden
def test_the_vectors_cover_every_construct() -> None:
    nodes = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "node" in value:
                nodes.add(value["node"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for _, vector in ALL:
        walk(vector["tree"])
    assert nodes == CORE_NODES | {"path", "children", "aggregate", "window"}


@pytest.mark.golden
def test_every_function_and_window_name_has_an_accepted_vector() -> None:
    texts = " ".join(vector["text"].upper() for _, vector in ALL if vector["refused"] is None)
    for word in (
        "ROUND",
        "HALF_TO_EVEN",
        "COALESCE",
        "DAYS_BETWEEN",
        "MONTHS_BETWEEN",
        "YEARS_BETWEEN",
        "COUNT",
        "SUM",
        "MIN",
        "MAX",
        "AVG",
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "FIRST_VALUE",
        "LAST_VALUE",
        "LAG",
        "LEAD",
        "PREVIOUS.",
        "NULLS FIRST",
        "DESC",
    ):
        assert re.search(re.escape(word), texts), word
