# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The row scope a flux-expr/2 text is checked against (EXPR-SPEC-2 §3, §10).

A text is evaluated for one row of one expose. The checker resolves its
names against a :class:`Model`: every expose's columns (with their FLUID
types, read through RFC-data-rules.md §2.3), its relationships, the key its
primary entity declares, and, for an entity with versions, its validity
window. The exposes whose relationships point to an expose are its
children collections, so they are derived here rather than declared.

This is the information EXPR-SPEC-2 §10's ``attrmodel/2`` ``row`` scope
lists. Building it from a pinned FLUID contract is the next piece's work
(the rules step that reads ``exposes[].contract.invariants[]``);
:func:`model_from_json` reads the model fragments of the vector files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.expr2.types import ColumnType, column_type

__all__ = ["Expose", "Link", "Model", "Versions", "model_from_json"]


@dataclass(frozen=True)
class Link:
    """A relationship an expose declares: ``fromColumns`` of this row equal ``toColumns`` of the target.

    Every relationship reaches at most one row (RFC-data-rules.md §2.2).
    """

    name: str
    target: str
    from_columns: Tuple[str, ...]
    to_columns: Tuple[str, ...]


@dataclass(frozen=True)
class Versions:
    """An entity with versions: its key columns and its validity window's start and end."""

    entity: Tuple[str, ...]
    start: str
    end: Optional[str] = None


@dataclass(frozen=True)
class Expose:
    """One expose: its columns in declared order, its links, its key, and its versions if any."""

    name: str
    columns: Mapping[str, ColumnType]
    links: Mapping[str, Link] = field(default_factory=dict)
    key: Tuple[str, ...] = ()
    versions: Optional[Versions] = None

    @property
    def declares_validity_window(self) -> bool:
        return self.versions is not None


@dataclass(frozen=True)
class Model:
    """The exposes a text can reach, by name."""

    exposes: Mapping[str, Expose]

    def expose(self, name: str) -> Expose:
        return self.exposes[name]

    def children_links(self, parent: str, child: str) -> List[Link]:
        """The relationships declared on *child* whose target is *parent*, in declared order."""
        found = self.exposes.get(child)
        if found is None:
            return []
        return [link for link in found.links.values() if link.target == parent]


def _names(value: Any, where: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} is a list of names")
    return tuple(value)


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} is an object")
    return value


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} is a name")
    return value


def _texts(value: Any, where: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{where} is a list of non-empty strings")
    return tuple(value)


def _column(raw: Any, where: str) -> ColumnType:
    declared = _object(raw, where)
    enum: Optional[Sequence[str]] = None
    if declared.get("enum") is not None:
        enum = _names(declared["enum"], f"{where}.enum")
    values: Tuple[str, ...] = ()
    prefixes: Tuple[str, ...] = ()
    if declared.get("placeholders") is not None:
        placeholders = _object(declared["placeholders"], f"{where}.placeholders")
        values = _texts(placeholders.get("values", []), f"{where}.placeholders.values")
        prefixes = _texts(placeholders.get("prefixes", []), f"{where}.placeholders.prefixes")
    return column_type(
        _name(declared.get("type"), f"{where}.type"),
        enum=enum,
        placeholder_values=values,
        placeholder_prefixes=prefixes,
    )


def _expose(name: str, raw: Any) -> Expose:
    where = f"exposes.{name}"
    body = _object(raw, where)
    columns = {
        column: _column(declared, f"{where}.columns.{column}")
        for column, declared in _object(body.get("columns", {}), f"{where}.columns").items()
    }
    links: Dict[str, Link] = {}
    for link_name, raw_link in _object(body.get("links", {}), f"{where}.links").items():
        at = f"{where}.links.{link_name}"
        link = _object(raw_link, at)
        from_columns = _names(link.get("fromColumns"), f"{at}.fromColumns")
        to_columns = _names(link.get("toColumns"), f"{at}.toColumns")
        if not from_columns or len(from_columns) != len(to_columns):
            raise ValueError(f"{at} pairs one or more fromColumns with as many toColumns")
        links[link_name] = Link(
            link_name, _name(link.get("to"), f"{at}.to"), from_columns, to_columns
        )
    versions = None
    if body.get("versions") is not None:
        raw_versions = _object(body["versions"], f"{where}.versions")
        end = raw_versions.get("end")
        versions = Versions(
            _names(raw_versions.get("entity"), f"{where}.versions.entity"),
            _name(raw_versions.get("start"), f"{where}.versions.start"),
            None if end is None else _name(end, f"{where}.versions.end"),
        )
    return Expose(name, columns, links, _names(body.get("key", []), f"{where}.key"), versions)


def model_from_json(blob: Mapping[str, Any]) -> Model:
    """Read a model fragment of a vector file.

    ``{"exposes": {"<name>": {"columns": {"<column>": {"type": "decimal(12,2)",
    "enum": [...], "placeholders": {"values": [...], "prefixes": [...]}}},
    "links": {"<rel>": {"to": "<expose>", "fromColumns": [...],
    "toColumns": [...]}}, "key": [...], "versions": {"entity": [...], "start":
    "<column>", "end": "<column>"}}}}``. Raises ``ValueError``, naming the path,
    on a fragment that does not have that shape or whose link names an expose
    it does not hold.
    """
    raw_exposes = _object(_object(blob, "a model fragment").get("exposes"), "exposes")
    exposes: Dict[str, Expose] = {name: _expose(name, raw) for name, raw in raw_exposes.items()}
    for expose in exposes.values():
        for link in expose.links.values():
            if link.target not in exposes:
                raise ValueError(
                    f"{expose.name}.links.{link.name} points to {link.target!r}, "
                    "which the model does not hold"
                )
    return Model(exposes)
