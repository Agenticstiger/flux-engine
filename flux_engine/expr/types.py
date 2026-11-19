# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The five static types of ``flux-expr/1`` and the per-scope attribute model.

EXPR-SPEC §2: "Five static types: ``bool``, ``number`` (evaluated as
``Decimal``), ``string``, ``set<string>``, ``enum<...>`` (a string drawn from
a declared closed set; compares as string ...). ``null`` is a value any path
may carry, not a type." That sentence is the whole of this module: there is
no null type, no "any", and no type constructor beyond :func:`enum_of`.

A :class:`ScopeModel` is one of the three namespaces of EXPR-SPEC §3
(``subject`` / ``event`` / ``observed``): a mapping from canonical dotted
path to :class:`Type`, plus the sugar aliases ("Bare ``lifecycle``,
``products``, ... are sugar for ``subject.<name>`` where unambiguous").
Sugar is resolved here, at check time; it is never rewritten into the
canonical form by ``unparse`` -- the canonical form of an expression is the
text the author wrote, normalised by the printer, not a de-sugared variant.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType  # stdlib ``types``; absolute import, not this module
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

__all__ = [
    "BOOL",
    "NUMBER",
    "STRING",
    "SET_STRING",
    "Scope",
    "ScopeModel",
    "Type",
    "TypeKind",
    "enum_of",
    "nfc",
]


def nfc(text: str) -> str:
    """Return *text* in Unicode NFC.

    Every identifier, enum level and string value that reaches a digest goes
    through here, matching KERNEL-SPEC §2.1 ("recursive NFC normalisation of
    every ``str`` value and every key"). It is also what makes the composed
    and decomposed spellings of an identifier name the same path
    (EXPR-SPEC §5).
    """
    return unicodedata.normalize("NFC", text)


class Scope(str, Enum):
    """The three namespaces of EXPR-SPEC §3. Closed; adding one is a spec change."""

    SUBJECT = "subject"
    EVENT = "event"
    OBSERVED = "observed"


class TypeKind(str, Enum):
    """The five static types of EXPR-SPEC §2. Closed."""

    BOOL = "bool"
    NUMBER = "number"
    STRING = "string"
    SET_STRING = "set<string>"
    ENUM = "enum"


@dataclass(frozen=True)
class Type:
    """A static type. ``levels`` is populated for -- and only for -- ``enum``.

    Enum levels are stored NFC-normalised, de-duplicated and sorted, so two
    models built from the same bundle in different mapping orders are equal
    and produce byte-identical canonical JSON (KERNEL-SPEC §2, "iterate
    mappings by sorted key").
    """

    kind: TypeKind
    levels: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind is TypeKind.ENUM:
            if not self.levels:
                raise ValueError("enum type requires at least one level")
        elif self.levels:
            raise ValueError(f"{self.kind.value} type carries no levels")

    @property
    def name(self) -> str:
        """Spelling used in error messages: ``enum<a|b>`` for enums, else the kind."""
        if self.kind is TypeKind.ENUM:
            return f"enum<{'|'.join(self.levels)}>"
        return self.kind.value

    def to_json(self) -> Dict[str, Any]:
        """Canonical JSON for the ``attrmodel/1`` document (EXPR-SPEC §3)."""
        if self.kind is TypeKind.ENUM:
            return {"type": self.kind.value, "levels": list(self.levels)}
        return {"type": self.kind.value}

    def __str__(self) -> str:
        return self.name


#: The four types that carry no parameters. Enums are built with
#: :func:`enum_of` because their levels come from the bundle.
BOOL = Type(TypeKind.BOOL)
NUMBER = Type(TypeKind.NUMBER)
STRING = Type(TypeKind.STRING)
SET_STRING = Type(TypeKind.SET_STRING)


def enum_of(levels: Iterable[str]) -> Type:
    """Build ``enum<...>`` from a bundle-declared closed set of *levels*."""
    normalised = sorted({nfc(level) for level in levels})
    return Type(TypeKind.ENUM, tuple(normalised))


def _frozen(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only, NFC-keyed, sorted copy of *mapping*."""
    return MappingProxyType({nfc(k): mapping[k] for k in sorted(mapping, key=nfc)})


@dataclass(frozen=True)
class ScopeModel:
    """One namespace of the attribute model: canonical paths and their types.

    ``attributes`` is keyed by the **canonical** dotted path (``subject.arpu``,
    ``data.amount``, ``late_payment_count``). ``aliases`` maps a sugar
    spelling to a canonical path; :meth:`resolve` consults it only after an
    exact match fails, so a canonical path can never be shadowed by sugar.
    """

    scope: Scope
    attributes: Mapping[str, Type] = field(default_factory=dict)
    aliases: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _frozen(self.attributes))
        object.__setattr__(self, "aliases", _frozen(self.aliases))
        for sugar, target in self.aliases.items():
            if target not in self.attributes:
                raise ValueError(f"alias {sugar!r} points at unknown path {target!r}")
            if sugar in self.attributes:
                raise ValueError(f"alias {sugar!r} shadows a canonical path")

    def resolve(self, path: str) -> Optional[str]:
        """Return the canonical path *path* names, or ``None`` if unknown.

        Exact match first, then the sugar table -- "bare ``lifecycle``,
        ``products``, ... are sugar for ``subject.<name>`` **where
        unambiguous**" (EXPR-SPEC §3). Ambiguous bare names are simply absent
        from ``aliases``, so they resolve to ``None`` and the checker raises
        ``FLX-6004``.
        """
        key = nfc(path)
        if key in self.attributes:
            return key
        return self.aliases.get(key)

    def type_of(self, path: str) -> Optional[Type]:
        """Return the type of *path* (after sugar resolution), or ``None``."""
        canonical = self.resolve(path)
        return None if canonical is None else self.attributes[canonical]

    def to_json(self) -> Dict[str, Any]:
        """Canonical JSON fragment for this scope of the ``attrmodel/1`` document."""
        return {
            "paths": {name: self.attributes[name].to_json() for name in sorted(self.attributes)},
            "aliases": {name: self.aliases[name] for name in sorted(self.aliases)},
        }
