# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Reading the flux-expr/2 vector files: values, rows, and expected results.

Three files, two shapes. ``fluid-sql-vectors.json`` is forge-cli's FLUID
SQL/1 conformance file, vendored byte for byte (RFC-data-rules.md §2.9), and
``expr2-core-vectors.json`` holds flux-engine's further core vectors in the
same shape: a vector names its model (an expose of the file's ``models``),
its ``site`` and, for a derive, its target, and a refused vector gives a
reason from the file's closed set. flux-engine's file also gives the FLX
code it reports; for forge-cli's, :data:`REASON_CODES` says which codes a
reason may be reported under. ``expr2-vectors.json`` holds the extensions
in the shape of ``expr-vectors.json`` (EXPR-SPEC §5).

No file carries a JSON number (KERNEL-SPEC §2.1): a number is a decimal
string, or ``"p/q"`` for a rational with no finite decimal expansion, a date
is ``YYYY-MM-DD`` and a timestamp ISO 8601, with an offset for a timestamp
with time zone. A row value is decoded by its column's declared type, so a
float column's ``"0.1"`` becomes the double 0.1 and the evaluator reads it
back as the shortest decimal (RFC-data-rules.md §2.3).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, FrozenSet, Hashable, List, Mapping, Optional

from flux_engine.expr2 import Expose, Kind, Model, Rows, Side, model_from_json

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_VECTORS = REPO_ROOT / "tests" / "vectors" / "fluid-sql-vectors.json"
CORE_VECTORS = REPO_ROOT / "tests" / "vectors" / "expr2-core-vectors.json"
EXTENSION_VECTORS = REPO_ROOT / "tests" / "vectors" / "expr2-vectors.json"

#: Where the vendored copy comes from, and its bytes' digest. Re-vendor by
#: copying the file from forge-cli unchanged and updating both.
SHARED_SOURCE = (
    "Agenticstiger/forge-cli fluid_build/fluid_sql/fluid-sql-vectors.json, "
    "vectorsVersion 1.1.0, at commit 1d7f6dc269795a8d7bbabdfe6f3f6e6c2ac43867"
)
SHARED_SHA256 = "ba7a6211f60d2c7a8a596b7f0e1bc8e8b6d9e020e3beb4b4e538cf9a7cb91d56"

#: The one model of a file in fluid-sql-vectors.json's shape, whose exposes
#: are the file's ``models``.
SHARED_MODEL = "fluid-sql"

#: The FLX codes each reason of fluid-sql-vectors.json is reported under. A
#: reason maps to one code but two: FLUX reports a keyword where a name
#: belongs as ``FLX-6104`` and a predicate that is not bool as ``FLX-6206``,
#: finer than the file's ``syntax`` and ``type-mismatch``.
REASON_CODES: Dict[str, FrozenSet[str]] = {
    "too-long": frozenset({"FLX-6103"}),
    "comment-or-semicolon": frozenset({"FLX-6103"}),
    "syntax": frozenset({"FLX-6103", "FLX-6104"}),
    "too-deep": frozenset({"FLX-6103"}),
    "round-places": frozenset({"FLX-6103"}),
    "bad-date": frozenset({"FLX-6109"}),
    "multi-link-path": frozenset({"FLX-6103"}),
    "unknown-column": frozenset({"FLX-6113"}),
    "unknown-relationship": frozenset({"FLX-6113"}),
    "validity-window": frozenset({"FLX-6106"}),
    "untyped-column": frozenset({"FLX-6111"}),
    "type-mismatch": frozenset({"FLX-6109", "FLX-6206"}),
    "null-literal": frozenset({"FLX-6104"}),
    "enum-literal": frozenset({"FLX-6109"}),
    "derive-target": frozenset({"FLX-6110"}),
    "derive-type": frozenset({"FLX-6110", "FLX-6206"}),
    "derive-scale": frozenset({"FLX-6110"}),
}

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def load(path: Path) -> Dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def is_shared_shape(document: Mapping[str, Any]) -> bool:
    """Whether *document* is in fluid-sql-vectors.json's shape."""
    return document.get("language") == "FLUID SQL/1"


def model_from_shared(models: Mapping[str, Any]) -> Model:
    """The model of a file in fluid-sql-vectors.json's shape.

    Its ``models`` are exposes with ``columns``, ``relationships`` (a name
    and the expose it reaches) and ``validityWindow``. A core text reads a
    relationship's target and nothing of its keys, so each link gets
    placeholder key columns and a validity window its first column.
    """
    exposes: Dict[str, Any] = {}
    for name, body in models.items():
        links = {
            relationship: {
                "to": target["to"],
                "fromColumns": [f"{relationship} key"],
                "toColumns": ["key"],
            }
            for relationship, target in body.get("relationships", {}).items()
        }
        fragment: Dict[str, Any] = {"columns": body["columns"], "links": links}
        if body.get("validityWindow"):
            first = next(iter(body["columns"]))
            fragment["versions"] = {"entity": [first], "start": first}
        exposes[name] = fragment
    return model_from_json({"exposes": exposes})


def models_of(document: Mapping[str, Any]) -> Dict[str, Model]:
    if is_shared_shape(document):
        return {SHARED_MODEL: model_from_shared(document["models"])}
    return {name: model_from_json(blob) for name, blob in document["models"].items()}


def _instant(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def decode(value: Any, expose: Expose, column: str) -> Any:
    """A JSON row value as the column stores it."""
    if value is None:
        return None
    declared = expose.columns[column]
    if declared.kind is Kind.NUMBER:
        return float(value) if declared.is_float else Decimal(value)
    if declared.kind is Kind.DATE:
        return date.fromisoformat(value)
    if declared.kind in (Kind.TIMESTAMP, Kind.TIMESTAMPTZ):
        return datetime.fromisoformat(value)
    return value


def decode_row(values: Mapping[str, Any], expose: Expose) -> Dict[str, Any]:
    return {column: decode(value, expose, column) for column, value in values.items()}


class DictRow:
    """A core vector's row: its own values, and one row per relationship it names."""

    def __init__(
        self, model: Model, expose: str, values: Mapping[str, Any], links: Mapping[str, Any]
    ) -> None:
        scope = model.exposes[expose]
        self._values = decode_row(values, scope)
        self._links: Dict[str, Optional[DictRow]] = {}
        for name, linked in links.items():
            target = scope.links[name].target
            self._links[name] = None if linked is None else DictRow(model, target, linked, {})
        self.key: Hashable = ("vector", id(self))

    def get(self, column: str) -> Any:
        return self._values.get(column)

    def follow(self, relationship: str) -> Optional["DictRow"]:
        return self._links.get(relationship)

    def previous(self) -> None:
        raise AssertionError("a core text has no previous")

    def children(self, expose: str, relationship: str) -> List[Any]:
        raise AssertionError("a core text has no children")

    def siblings(self, relationship: str) -> List[Any]:
        raise AssertionError("a core text has no siblings")


def rows_of(document: Mapping[str, Any], models: Mapping[str, Model], name: str) -> Rows:
    data = document["data"][name]
    model = models[data["model"]]
    tables = {
        expose: [decode_row(row, model.exposes[expose]) for row in rows]
        for expose, rows in data["rows"].items()
    }
    return Rows(model, tables)


def matches(expected: Any, actual: Any) -> bool:
    """Whether an evaluated value is the vector's expected result."""
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(actual, Fraction):
        return isinstance(expected, str) and Fraction(expected) == actual
    if isinstance(actual, date):
        return expected == actual.isoformat()
    if isinstance(actual, int) and isinstance(expected, str):
        # A timestamp evaluates to its milliseconds; the file writes the instant.
        return (_instant(expected) - _EPOCH) // timedelta(milliseconds=1) == actual
    return bool(expected == actual)


def normal(vector: Mapping[str, Any]) -> Dict[str, Any]:
    """One vector of any of the three files, as the runner reads it.

    ``refused`` is the set of codes the refusal may be reported under (one
    code, except for a reason of forge-cli's file that maps to two), or
    ``None``. ``step`` is ``"parse"`` or ``"check"`` where the file says
    which step refuses the text, and ``None`` where it does not.
    """
    expect = vector["expect"]
    out: Dict[str, Any] = {"id": vector["id"], "refused": None, "step": None, "tree": None}
    if "site" in vector:
        out.update(
            text=vector["text"],
            side=Side.CORE,
            model=SHARED_MODEL,
            expose=vector["model"],
            position=vector["site"],
            target=vector.get("derive"),
        )
        if expect["verdict"] == "refused":
            allowed = REASON_CODES[expect["reason"]]
            if "code" in expect:
                assert expect["code"] in allowed, vector["id"]
                allowed = frozenset({expect["code"]})
            out["refused"] = allowed
            return out
        scale = expect.get("scale")
        out.update(
            tree=expect["tree"],
            type=expect["type"],
            scale=None if scale == "unbounded" else scale,
            canonical=expect["canonical"],
            eval=expect["eval"],
        )
        return out
    out.update(
        text=vector["expr"],
        side=Side(vector["side"]),
        model=vector["model_fragment"],
        expose=vector["expose"],
        position=vector["position"],
        target=vector.get("target"),
    )
    if isinstance(expect["parse"], str):
        out.update(refused=frozenset({expect["parse"]}), step="parse")
        return out
    out["tree"] = expect["parse"]
    if expect["check"] != "ok":
        out.update(refused=frozenset({expect["check"]}), step="check")
        return out
    out.update(
        type=expect["type"],
        scale=expect.get("scale"),
        canonical=expect["unparse"],
        eval=expect["eval"],
    )
    return out
