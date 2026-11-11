# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Validating readers for the JSON documents the CLI takes from a caller.

Three commands take a hand-written JSON file -- ``accuracy register
--file``, ``accuracy score --readout`` and ``gate decide --request`` --
and each of them used to index the parsed document with a bare
``document["key"]`` and build its enums with a bare ``Enum(value)``. A
missing field therefore reached the terminal as ``KeyError:
'run_digest'``, one field per run-and-crash cycle, and a bad enum as
``ValueError: 'holdout' is not a valid ClaimBasis`` -- withholding the
three legal values it had just checked against.

This module is the layer that was missing. It reads the file, parses it,
and reports **every** problem it can see in one message: each missing
field, each field of the wrong type, and for a closed set, the values
that set actually admits. The same field list is what the flags'
``--help`` prints, so the shape is discoverable from the CLI rather than
from a Python docstring in the source tree.

Nothing here is a schema language: :class:`Field` carries the four facts
the three documents need (name, kind, required, choices) and no more. A
document that grows a nested shape belongs in a real model, not in a
fifth kind here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

__all__ = [
    "Field",
    "GATE_REQUEST_FIELDS",
    "PREDICTION_FIELDS",
    "READOUT_FIELDS",
    "DocumentProblems",
    "MissingDocument",
    "field_list_help",
    "problems_in",
    "read_json_object",
]


@dataclass(frozen=True)
class Field:
    """One field of a caller-supplied document.

    ``kind`` is one of ``"string"``, ``"decimal"`` (a JSON number or its
    string spelling, kept exact), ``"integer"`` or ``"object"``.
    ``choices``, when set, is the closed set of values the field admits --
    and the set every error message about it prints.
    """

    name: str
    kind: str
    required: bool = True
    choices: Optional[Tuple[str, ...]] = None

    def describe(self) -> str:
        """This field as one line of ``--help``: name, type, and legal values."""
        shape = self.kind if self.choices is None else "|".join(self.choices)
        return f"{self.name} ({shape})" + ("" if self.required else " [optional]")


class DocumentProblems(Exception):
    """Every problem found in one caller-supplied document, in one message.

    Carries the problems as a tuple as well as in ``str(self)`` so a
    caller that wants to render them itself does not have to split the
    message back apart.
    """

    def __init__(self, what: str, problems: Sequence[str]) -> None:
        self.what = what
        self.problems: Tuple[str, ...] = tuple(problems)
        super().__init__(f"{what}: " + "; ".join(self.problems))


class MissingDocument(DocumentProblems):
    """The document is not on disk at all.

    A distinct type because it is a distinct failure: a file that does not
    exist has no shape, so reporting it under a shape/validation code sent
    the reader to check fields of a document they have not written yet. The
    caller maps this one to its own "input not found" code and leaves the
    shape code for a document that is there and wrong.
    """


#: ``fluxsim accuracy register --file``: a ``PredictionRecord``.
PREDICTION_FIELDS: Tuple[Field, ...] = (
    Field("prediction_id", "string"),
    Field("run_digest", "string"),
    Field("metric_id", "string"),
    Field("statement", "string"),
    Field("predicted", "decimal"),
    Field("horizon_end", "string"),
    Field("registered_at", "string"),
    Field("interval_low", "decimal", required=False),
    Field("interval_high", "decimal", required=False),
)

#: ``fluxsim accuracy score --readout``: an ``ObservedReadout``. ``basis``'s
#: choices are :class:`~flux_engine.api.claims.ClaimBasis`'s own values,
#: read from the enum rather than restated here.
READOUT_FIELDS: Tuple[Field, ...] = (
    Field("prediction_id", "string"),
    Field("metric_id", "string"),
    Field("observed", "decimal"),
    Field("sample_size", "integer"),
    # `choices` is filled in by `readout_fields()` from the enum itself.
    Field("basis", "string"),
    Field("observed_at", "string"),
)

#: ``fluxsim gate decide --request``: the policy, the request, and the
#: optional skill the RFC-04 decision is taken over.
GATE_REQUEST_FIELDS: Tuple[Field, ...] = (
    Field("policy", "object"),
    Field("request", "object"),
    Field("skill", "object", required=False),
)


def readout_fields() -> Tuple[Field, ...]:
    """:data:`READOUT_FIELDS` with ``basis``'s legal values filled in.

    A function rather than a constant because the values come from
    :class:`~flux_engine.api.claims.ClaimBasis`, and importing it at module
    scope would put the accuracy API on the cold path of every command.
    """
    from flux_engine.api.claims import ClaimBasis

    choices = tuple(member.value for member in ClaimBasis)
    return tuple(
        Field(f.name, f.kind, f.required, choices) if f.name == "basis" else f
        for f in READOUT_FIELDS
    )


def field_list_help(fields: Sequence[Field]) -> str:
    """The field list as it appears in a flag's ``--help``."""
    return "fields: " + ", ".join(field.describe() for field in fields)


def _kind_problem(field: Field, value: Any) -> Optional[str]:
    if field.kind == "string":
        if not isinstance(value, str):
            return f"{field.name} must be a string, got {type(value).__name__}"
    elif field.kind == "object":
        if not isinstance(value, Mapping):
            return f"{field.name} must be a JSON object, got {type(value).__name__}"
    elif field.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{field.name} must be an integer, got {type(value).__name__}"
    elif field.kind == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return f"{field.name} must be a number, got {type(value).__name__}"
        try:
            Decimal(str(value))
        except InvalidOperation:
            return f"{field.name} must be a number, got {value!r}"
    return None


def problems_in(document: Any, fields: Sequence[Field]) -> Tuple[str, ...]:
    """Every problem in *document*, in field order -- never just the first one.

    Reports a missing required field, a field whose type does not match,
    and a value outside a field's closed set (naming the whole set). An
    absent optional field is not a problem; an optional field that is
    present and wrong is.
    """
    if not isinstance(document, Mapping):
        return (f"expected a JSON object, got {type(document).__name__}",)
    problems: List[str] = []
    for field in fields:
        if field.name not in document:
            if field.required:
                problems.append(f"missing required field {field.name!r}")
            continue
        value = document[field.name]
        kind_problem = _kind_problem(field, value)
        if kind_problem is not None:
            problems.append(kind_problem)
            continue
        if field.choices is not None and value not in field.choices:
            problems.append(f"{field.name}={value!r} is not one of {', '.join(field.choices)}")
    return tuple(problems)


def read_json_object(
    path: Union[str, Path], *, what: str, fields: Sequence[Field]
) -> Dict[str, Any]:
    """Read, parse and validate one caller-supplied JSON document.

    Raises :class:`DocumentProblems` -- with every problem in one message
    -- for a file that is absent, unreadable, not JSON, or not the shape
    *fields* describes. The caller decides what FLX code that becomes:
    the shape of a document is the same question whichever command asked
    for it, but the band the failure belongs to is not.
    """
    file_path = Path(path)
    subject = f"{what} {file_path}"
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MissingDocument(subject, ["no such file"]) from None
    except OSError as error:
        raise DocumentProblems(subject, [str(error.strerror or error)]) from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise DocumentProblems(
            subject, [f"not valid JSON (line {error.lineno}, column {error.colno})"]
        ) from None
    problems = problems_in(document, fields)
    if problems:
        raise DocumentProblems(subject, problems)
    return dict(document)
