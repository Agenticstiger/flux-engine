# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error catalog for ``flux-expr/1`` (the ``FLX-6nnn`` range).

KERNEL-SPEC §13 assigns the 6000 range to "expression language" and
requires ``fluxsim explain FLX-nnnn`` to print a catalog entry, so every code
raised by this package is declared here with a title and a hint rather
than being an ad-hoc string at the raise site.

The engine-wide registry is ``flux_engine.core.errors``, whose contract is
that "the catalog is populated at import time by the module that actually
raises the code, via ``register``". This module honours that: importing it
publishes all five ``FLX-6nnn`` entries into the shared catalog. It also
keeps :data:`CATALOG` as a local, self-contained copy, so the expression
package still works -- and still explains its codes -- when the shared
registry is not importable, which is the state ``flux_engine.core`` was in
when this package was written. See :func:`publish_to_core_catalog` and the
REQUEST in the build report (``core.errors._CORE_CODE_MODULES`` has to name
this module for ``fluxsim explain FLX-6nnn`` to work without an expression
having been parsed first).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CATALOG",
    "PUBLISHED_TO_CORE_CATALOG",
    "CatalogEntry",
    "FluxExprError",
    "ParseError",
    "ReservedWordError",
    "EnumLiteralError",
    "UnknownPathError",
    "TypeMismatchError",
    "known_attributes_hint",
    "publish_to_core_catalog",
    "register_all",
    "with_known_attributes",
]


@dataclass(frozen=True)
class CatalogEntry:
    """One row of the ``FLX-6nnn`` catalog: the code, its title and a hint."""

    code: str
    title: str
    hint: str


#: The closed catalog for flux-expr/1. EXPR-SPEC §1-§3 name every one of
#: these; adding a code is a grammar change and therefore a
#: ``kernelVersion`` bump, not a patch.
CATALOG: Mapping[str, CatalogEntry] = {
    entry.code: entry
    for entry in (
        CatalogEntry(
            "FLX-6001",
            "not derivable from the flux-expr/1 grammar",
            "The grammar is closed: only the productions in EXPR-SPEC §1 parse. "
            "There is no arithmetic, no lambda, no regex, no double-quoted string "
            "and no user-defined function.",
        ),
        CatalogEntry(
            "FLX-6002",
            "reserved word held for flux-expr/2",
            "This word is reserved and has no meaning in flux-expr/1. A grammar "
            "that needs it is flux-expr/2; raise the request there.",
        ),
        CatalogEntry(
            "FLX-6003",
            "literal outside the declared closed set",
            "The attribute is an enum whose levels come from the bundle. Compare "
            "it against one of its declared levels.",
        ),
        CatalogEntry(
            "FLX-6004",
            "unknown path for this scope",
            "Names are resolved against the attribute model derived at plan time. "
            "Print the model with `fluxsim expr model` to see what this scope offers.",
        ),
        CatalogEntry(
            "FLX-6005",
            "type mismatch",
            "flux-expr/1 has no coercion: the two sides of a comparison must have "
            "the same type, and each method is declared on one type only.",
        ),
    )
}


class FluxExprError(Exception):
    """Base class for every ``FLX-6nnn`` failure raised by this package.

    Carries the machine-readable ``code`` alongside the human message and,
    where the failure has a source location, the 0-based character offset
    into the **NFC-normalised** expression text (the lexer normalises its
    input first, so offsets index the normalised string -- see
    :mod:`flux_engine.expr.lexer`).
    """

    #: Subclasses set this; the catalog is keyed on it.
    code: str = "FLX-6001"

    def __init__(self, message: str, *, position: Optional[int] = None) -> None:
        self.message = message
        self.position = position
        super().__init__(str(self))

    def __str__(self) -> str:
        where = "" if self.position is None else f" (at offset {self.position})"
        return f"{self.code}: {self.message}{where}"

    @property
    def entry(self) -> CatalogEntry:
        """The catalog row for this error's code."""
        return CATALOG[self.code]


class ParseError(FluxExprError):
    """FLX-6001 -- the text is not derivable from the closed grammar."""

    code = "FLX-6001"


class ReservedWordError(FluxExprError):
    """FLX-6002 -- a word reserved for ``flux-expr/2`` was used."""

    code = "FLX-6002"


class EnumLiteralError(FluxExprError):
    """FLX-6003 -- a string literal falls outside an enum's declared levels."""

    code = "FLX-6003"


class UnknownPathError(FluxExprError):
    """FLX-6004 -- a path the scope's attribute model does not declare."""

    code = "FLX-6004"


class TypeMismatchError(FluxExprError):
    """FLX-6005 -- operand or argument types do not line up; no coercion."""

    code = "FLX-6005"


#: The name an ``FLX-6004`` raise site quotes in its message (``'arpu' is
#: not an attribute of the subject scope``). Read back by
#: :func:`with_known_attributes` so the suggestion can be computed for the
#: name that actually failed, without every raise site having to hand the
#: attribute model down to the error.
_QUOTED_NAME = re.compile(r"'([^']+)'")

#: How many attributes an FLX-6004 message lists before it stops. A scope
#: with hundreds of attributes would otherwise turn one error into a wall
#: of text; the count that was elided is still reported.
_MAX_LISTED_ATTRIBUTES = 40


def known_attributes_hint(known: Iterable[str], *, name: Optional[str] = None) -> str:
    """The attributes a scope declares, and the closest match to *name*.

    The engine holds the whole attribute model in memory at the moment it
    raises ``FLX-6004``, and the one list that fixes the error in a single
    keystroke was being withheld from the reader. Returns the empty string
    when the scope declares nothing, so a caller can append it
    unconditionally.
    """
    attributes: Sequence[str] = sorted(set(known))
    if not attributes:
        return ""
    parts = []
    if name:
        close = difflib.get_close_matches(name, attributes, n=1, cutoff=0.6)
        if close:
            parts.append(f"did you mean {close[0]!r}?")
    listed: Tuple[str, ...] = tuple(attributes[:_MAX_LISTED_ATTRIBUTES])
    elided = len(attributes) - len(listed)
    known_text = "known attributes: " + ", ".join(listed)
    if elided:
        known_text = f"{known_text} (+{elided} more; `fluxsim expr model` prints them all)"
    parts.append(known_text)
    return " -- " + "; ".join(parts)


def with_known_attributes(error: "UnknownPathError", known: Iterable[str]) -> "UnknownPathError":
    """Return *error* again, with the scope's attribute list appended to its message.

    For the caller that has the attribute model to hand (the CLI's ``expr
    eval``) and can therefore answer the question the bare error only
    asks. The unknown name is read back from the message the raise site
    composed; when it cannot be read, the list is still appended, without
    a suggestion.
    """
    match = _QUOTED_NAME.search(error.message)
    hint = known_attributes_hint(known, name=match.group(1) if match else None)
    if not hint:
        return error
    return UnknownPathError(error.message + hint, position=error.position)


def register_all(registry: Dict[str, CatalogEntry]) -> None:
    """Copy this catalog into a plain ``code -> entry`` *registry*.

    The mapping-shaped counterpart to :func:`publish_to_core_catalog`, for a
    consumer that keeps its own table (a documentation generator, a test)
    rather than the engine-wide catalog. Idempotent, and it refuses to
    shadow a differing entry for the same code, so a collision is loud
    rather than silent.
    """
    for code, entry in sorted(CATALOG.items()):
        existing = registry.get(code)
        if existing is not None and existing != entry:
            raise ValueError(f"{code} is already registered with a different entry")
        registry[code] = entry


def publish_to_core_catalog() -> bool:
    """Publish this catalog into ``flux_engine.core.errors``; return whether it landed.

    The shared catalog's rule is that the module which raises a code
    registers it at import time, so this runs on import (see
    :data:`PUBLISHED_TO_CORE_CATALOG`). A missing or older
    ``flux_engine.core.errors`` is not an error -- the local
    :data:`CATALOG` is authoritative inside this package either way -- but a
    *conflicting* registration is: two subsystems must not share one number,
    so a ``ValueError`` from the shared registry propagates rather than
    being swallowed.
    """
    try:
        from flux_engine.core import errors as core_errors
    except ImportError:  # pragma: no cover - depends on the installed tree
        return False
    register = getattr(core_errors, "register", None)
    if register is None:  # pragma: no cover - depends on the installed tree
        return False
    for code in sorted(CATALOG):
        entry = CATALOG[code]
        register(entry.code, entry.title, entry.hint)
    return True


#: Whether the five ``FLX-6nnn`` entries reached the engine-wide catalog on
#: import. False means ``flux_engine.core.errors`` was not importable; the
#: local :data:`CATALOG` is unaffected.
PUBLISHED_TO_CORE_CATALOG = publish_to_core_catalog()
