# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``tests/vectors/projection-vectors.json``: expose schema + row -> NDJSON line.

The file is a published artifact, not a snapshot. Every ``line`` in it was
written by hand from KERNEL-SPEC §8 and §9; nothing in it was captured
from the implementation. So a disagreement is a real disagreement -- the
implementation is wrong, or the vector is, and either way somebody has to
decide which.

Two guards keep the file honest as it grows:

* a coverage test asserts the file exercises every logical type of the
  closed set and every rejection code the seam can raise;
* a self-consistency test asserts every ``line`` is itself well-formed
  canonical JSON of an array of the declared length, so a vector cannot
  quietly pin a malformed line.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from flux_engine.seam.conform import render_row
from flux_engine.seam.contract import LogicalKind, compile_expose
from flux_engine.seam.emit import row_line

pytestmark = pytest.mark.unit

VECTORS_PATH = Path(__file__).resolve().parents[1] / "vectors" / "projection-vectors.json"
VECTORS: Dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def _decode(value: Any) -> Any:
    """Decode the file's ``$``-tagged value encoding into a Python value.

    A single-key mapping whose key starts with ``$`` is a tag; anything
    else is the JSON value itself, decoded recursively so a tag nested
    inside a ``JSON`` column's payload is decoded too.
    """
    if isinstance(value, Mapping):
        if len(value) == 1:
            key, item = next(iter(value.items()))
            if key == "$decimal":
                return Decimal(str(item))
            if key == "$date":
                return date.fromisoformat(str(item))
            if key == "$datetime":
                return datetime.fromisoformat(str(item))
            if key == "$bytes":
                return base64.b64decode(str(item))
            if key == "$ms":
                return int(item)
            if key == "$tuple":
                return tuple(_decode(entry) for entry in item)
            if key.startswith("$"):
                raise AssertionError(f"unknown value tag {key!r} in the vector file")
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _expose(schema: List[Mapping[str, Any]], expose_id: str = "vector") -> Any:
    return compile_expose(
        {
            "exposeId": expose_id,
            "kind": "stream",
            "contract": {"schema": list(schema)},
            "binding": {"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}},
        }
    )


def _ids(entries: List[Dict[str, Any]]) -> List[str]:
    return [str(entry["name"]) for entry in entries]


POSITIVE: List[Dict[str, Any]] = VECTORS["vectors"]
ROW_REJECTIONS: List[Dict[str, Any]] = VECTORS["rejections"]
SCHEMA_REJECTIONS: List[Dict[str, Any]] = VECTORS["schemaRejections"]


# ---------------------------------------------------------------------------
# The vectors themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vector", POSITIVE, ids=_ids(POSITIVE))
def test_a_row_projects_to_its_declared_line(vector: Dict[str, Any]) -> None:
    expose = _expose(vector["schema"])
    rendered = render_row(_decode(vector["row"]), expose, row_index=0)
    assert row_line(rendered) == vector["line"].encode("utf-8")


@pytest.mark.parametrize("vector", POSITIVE, ids=_ids(POSITIVE))
def test_the_declared_types_are_the_compiled_types(vector: Dict[str, Any]) -> None:
    """The vector's ``types`` pin the type normalisation, not just the values."""
    assert list(_expose(vector["schema"]).type_names) == vector["types"]


@pytest.mark.parametrize("vector", ROW_REJECTIONS, ids=_ids(ROW_REJECTIONS))
def test_a_rejected_row_raises_its_declared_code(vector: Dict[str, Any]) -> None:
    expose = _expose(vector["schema"])
    with pytest.raises(FluxError) as error:
        render_row(_decode(vector["row"]), expose, row_index=0)
    assert error.value.code.code == vector["code"]


@pytest.mark.parametrize("vector", SCHEMA_REJECTIONS, ids=_ids(SCHEMA_REJECTIONS))
def test_a_rejected_schema_raises_its_declared_code(vector: Dict[str, Any]) -> None:
    with pytest.raises(FluxError) as error:
        _expose(vector["schema"])
    assert error.value.code.code == vector["code"]


# ---------------------------------------------------------------------------
# The file stays honest
# ---------------------------------------------------------------------------


def test_no_line_was_captured_from_a_float() -> None:
    """The vector file carries no JSON float anywhere.

    §2.1's rule applies to the vectors too: a float in the file would be a
    number the file itself could not reproduce across platforms.
    """

    def floats(value: Any, path: str = "") -> List[str]:
        if isinstance(value, float):
            return [path]
        if isinstance(value, Mapping):
            return [p for k, v in value.items() for p in floats(v, f"{path}.{k}")]
        if isinstance(value, list):
            return [p for i, v in enumerate(value) for p in floats(v, f"{path}[{i}]")]
        return []

    raw = json.loads(VECTORS_PATH.read_text(encoding="utf-8"), parse_float=float)
    # The one legitimate float in the file is the rejection vector that
    # *is* a float; everything else must be exact.
    offenders = [path for path in floats(raw) if ".rejections" not in path]
    assert offenders == []


@pytest.mark.parametrize("vector", POSITIVE, ids=_ids(POSITIVE))
def test_every_declared_line_is_itself_canonical_json(vector: Dict[str, Any]) -> None:
    """A vector cannot pin a line that is not what §8 would ever emit."""
    line = vector["line"]
    assert line.endswith("\n")
    assert line.count("\n") == 1
    body = json.loads(line)
    assert isinstance(body, list)
    assert len(body) == len(vector["schema"])
    assert canonical_bytes(body) + b"\n" == line.encode("utf-8")


def test_the_file_covers_every_logical_type() -> None:
    covered = {type_name.split("(")[0] for vector in POSITIVE for type_name in vector["types"]}
    assert covered == {member.value for member in LogicalKind}


def test_the_file_covers_every_code_the_seam_can_raise() -> None:
    """The rejection set is total over the seam's row-level and schema-level codes."""
    row_codes = {vector["code"] for vector in ROW_REJECTIONS}
    schema_codes = {vector["code"] for vector in SCHEMA_REJECTIONS}
    assert row_codes == {"FLX-3002", "FLX-3003", "FLX-3201", "FLX-3202", "FLX-3203"}
    assert schema_codes == {"FLX-3001", "FLX-3004"}


def test_every_vector_is_named_once() -> None:
    names = _ids(POSITIVE) + _ids(ROW_REJECTIONS) + _ids(SCHEMA_REJECTIONS)
    assert len(names) == len(set(names))


def test_the_file_declares_its_own_value_encoding() -> None:
    """Every tag the decoder understands is documented in the file."""
    documented = set(VECTORS["encoding"])
    assert documented == {"$decimal", "$date", "$datetime", "$bytes", "$ms", "$tuple"}


def test_the_file_exercises_each_tag_it_documents() -> None:
    text = VECTORS_PATH.read_text(encoding="utf-8")
    for tag in VECTORS["encoding"]:
        assert f'"{tag}"' in text.replace('"encoding"', ""), f"{tag} is documented but unused"


# ---------------------------------------------------------------------------
# A negative control on the driver itself
# ---------------------------------------------------------------------------


def test_the_driver_would_notice_a_wrong_line() -> None:
    """If the comparison were vacuous, this would pass anyway. It does not."""
    vector = POSITIVE[0]
    expose = _expose(vector["schema"])
    rendered = render_row(_decode(vector["row"]), expose, row_index=0)
    wrong = vector["line"].replace('"IE"', '"GB"').encode("utf-8")
    assert row_line(rendered) != wrong


def test_the_decoder_refuses_a_tag_it_does_not_know() -> None:
    with pytest.raises(AssertionError):
        _decode({"$mystery": "x"})


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        ({"$decimal": "1.50"}, Decimal("1.50")),
        ({"$ms": 5}, 5),
        ({"$tuple": [1, 2]}, (1, 2)),
        ({"$bytes": "aGk="}, b"hi"),
        ({"$date": "2026-01-01"}, date(2026, 1, 1)),
        ({"a": {"$decimal": "2"}}, {"a": Decimal("2")}),
        ([{"$decimal": "3"}], [Decimal("3")]),
        ({"two": "keys", "$decimal": "not a tag"}, {"two": "keys", "$decimal": "not a tag"}),
        ("plain", "plain"),
    ],
)
def test_the_decoder_round_trips_its_own_encoding(encoded: Any, expected: Any) -> None:
    assert _decode(encoded) == expected


def _first_tuple_row() -> Tuple[Any, ...]:
    for vector in POSITIVE:
        row = _decode(vector["row"])
        if isinstance(row, tuple):
            return row
    raise AssertionError("no positional vector in the file")


def test_the_file_carries_at_least_one_positional_row() -> None:
    assert isinstance(_first_tuple_row(), tuple)
