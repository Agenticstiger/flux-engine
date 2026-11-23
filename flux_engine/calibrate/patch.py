# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The calibration patch (ADR 0028 section 8): RFC 6902 ``replace`` operations on declared numbers.

A patch is a JSON document. Its header says how it was made, and its ``operations`` are
``{document, op: "replace", path, value}``, one for each value the fit moved. ``document`` is the file
name the calibration block gave, ``path`` the JSON Pointer into the document in that file, and ``value``
the new number as exact decimal text (a JSON number cannot be exact, and a float is never written into a
hashed document). A ``replace`` needs its target to exist, so a patch moves a number the bundle already
declares and adds nothing.

The patch's digest is the ``raw-bytes`` digest of the text :func:`render_patch` returns, the bytes that
are written, so a reader digests the file as it is.

**Applying.** :func:`splice` writes the new values into the text of a document and leaves every other
byte as it was: a comment, a quote style and the order of keys all stay. It is done on the YAML node
graph's source positions, so a document that uses an alias or a merge key, where one position stands for
several, is refused (``FLX-3711``) rather than edited in one of its places.

This module imports the standard library, PyYAML and the engine's digest and error modules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from flux_engine.calibrate.errors import FLX_3711
from flux_engine.core.canonical import nfc
from flux_engine.core.digest import digest_of_bytes
from flux_engine.core.errors import FluxError
from flux_engine.seam.coverage import printable

__all__ = [
    "METHOD_ID",
    "Located",
    "Operation",
    "locate",
    "patch_digest",
    "render_patch",
    "splice",
]

#: The method id the fit reports in its patch and its scorecard.
METHOD_ID = "flux.calibration.cma/1"

_NUMBER_TAGS = frozenset({"tag:yaml.org,2002:int", "tag:yaml.org,2002:float"})
_MERGE_TAG = "tag:yaml.org,2002:merge"


@dataclass(frozen=True)
class Operation:
    """One ``replace``: the number at *path* of the document in *document* becomes *value*."""

    document: str
    path: str
    value: str

    def as_document(self) -> Dict[str, str]:
        return {"document": self.document, "op": "replace", "path": self.path, "value": self.value}


def render_patch(document: Mapping[str, Any]) -> str:
    """The exact text of a patch: sorted keys, two-space indent, a final newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def patch_digest(text: str) -> str:
    """The ``sha256:`` digest of a patch's text, as it is written."""
    return digest_of_bytes(text.encode("utf-8"), subject="calibration-patch").value


@dataclass(frozen=True)
class Located:
    """Where a number is written in a document's text: its character span, and its quote."""

    start: int
    end: int
    #: ``""`` for a plain scalar, else the quote character it is written in.
    quote: str


def _refuse(document: str, pointer: str, why: str) -> FluxError:
    return FluxError(
        FLX_3711, f"{printable(document)} {printable(pointer)}: {why}; nothing was written"
    )


def _graph(text: str, document: str) -> Any:
    try:
        if any(
            isinstance(event, yaml.AliasEvent) for event in yaml.parse(text, Loader=yaml.SafeLoader)
        ):
            raise _refuse(
                document, "", "the document uses an alias, so one position stands for several"
            )
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        raise _refuse(
            document, "", f"the text does not compose as one YAML document ({type(error).__name__})"
        ) from error
    if root is None:
        raise _refuse(document, "", "the document is empty")
    return root


def locate(
    text: str, document: str, targets: Sequence[Tuple[str, Sequence[str]]]
) -> Dict[str, Located]:
    """Where each of *targets* (a pointer and its tokens) is written in *text*, the file *document*.

    ``FLX-3711`` for a pointer that reaches no member, a target that is not one scalar written on one
    line, a plain scalar that is not a number, a merge key on the way, and a document that aliases.
    """
    root = _graph(text, document)
    found: Dict[str, Located] = {}
    for pointer, tokens in targets:
        node: Optional[Any] = root
        for token in tokens:
            node = _member(node, token)
            if node is None:
                raise _refuse(document, pointer, "the pointer reaches no member")
        found[pointer] = _scalar(node, text, document, pointer)
    return found


def _mapping_member(node: Any, token: str) -> Optional[Any]:
    """The value under *token* in a mapping node, or ``None`` (a merge key on the way gives none)."""
    for key, value in node.value:
        if key.tag == _MERGE_TAG:
            return None
        if isinstance(key, yaml.ScalarNode) and nfc(str(key.value)) == token:
            return value
    return None


def _sequence_member(node: Any, token: str) -> Optional[Any]:
    """The item at the index *token* spells in a sequence node, or ``None``."""
    if token.isdecimal() and (token == "0" or not token.startswith("0")):
        index = int(token)
        if index < len(node.value):
            return node.value[index]
    return None


def _member(node: Any, token: str) -> Optional[Any]:
    if isinstance(node, yaml.MappingNode):
        return _mapping_member(node, token)
    if isinstance(node, yaml.SequenceNode):
        return _sequence_member(node, token)
    return None


def _scalar(node: Any, text: str, document: str, pointer: str) -> Located:
    if not isinstance(node, yaml.ScalarNode):
        raise _refuse(document, pointer, "the target is a mapping or a list, not one number")
    if node.style not in (None, "'", '"'):
        raise _refuse(document, pointer, "the target is a block scalar")
    if node.style is None and node.tag not in _NUMBER_TAGS:
        raise _refuse(document, pointer, "the target is not a number")
    start, end = node.start_mark.index, node.end_mark.index
    if "\n" in text[start:end]:
        raise _refuse(document, pointer, "the target is written over several lines")
    return Located(start, end, node.style or "")


def splice(text: str, edits: Sequence[Tuple[Located, str]]) -> str:
    """*text* with each located scalar written as the new value text, in its own quote."""
    out: List[str] = []
    cursor = 0
    for where, value in sorted(edits, key=lambda item: item[0].start):
        out.append(text[cursor : where.start])
        out.append(f"{where.quote}{value}{where.quote}")
        cursor = where.end
    out.append(text[cursor:])
    return "".join(out)
