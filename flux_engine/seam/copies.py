# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The copies a Simulation asks for: ``copies: true`` (ADR 0022 section 4).

A Simulation declares them under the top-level ``copies`` key of its namespace, in the FLUX
bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        copies: true

Its only value is ``true``. Under kernel 0.2.0 it makes each emit copy the columns its contract
lists under a relationship's ``sharedColumns`` and a column's ``sharedAttribute`` from the row the
relationship reaches. Without it a contract that declares either is read as it was before copies
existed: the declaration is listed as declared, and no column is copied. KERNEL-SPEC-2 section 12
makes a change to what a plan generates additive only when a new declaration reads it, so a plan
written before copies existed replays to its digests.

**The key changes the rows an emit generates**, so it is bound into ``H`` before any row is drawn:
:func:`bound_into_h` is the member the entry for ``H`` of an emit that declares a ``sharedColumns``
or a ``sharedAttribute`` gains, present only then, so a bundle that does not declare it hashes
exactly as before. ``G`` leaves the member out, as it leaves out the links'.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from flux_engine.core.errors import ErrorCode, FluxError

__all__ = [
    "COPIES_KEY",
    "bound_into_h",
    "copies_of",
    "declares_copies",
    "read_copies_value",
]

#: The top-level key of the namespace that asks for copies, and the member an emit's ``H`` gains.
COPIES_KEY = "copies"


def read_copies_value(value: Any, *, where: str, code: ErrorCode) -> bool:
    """Whether a ``copies`` value asks for copies; anything but ``true`` is refused with *code*."""
    if value is not True:
        raise FluxError(code, f"{where} is true: it is the only value the key takes")
    return True


def _list_of(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def declares_copies(expose: Mapping[str, Any]) -> bool:
    """Whether the contract's *expose* declares a ``sharedColumns`` or a ``sharedAttribute``."""
    semantics = expose.get("semantics")
    for relationship in _list_of(
        semantics.get("relationships") if isinstance(semantics, Mapping) else None
    ):
        if (
            isinstance(relationship, Mapping)
            and isinstance(relationship.get("name"), str)
            and _list_of(relationship.get("sharedColumns"))
        ):
            return True
    contract = expose.get("contract")
    for column in _list_of(contract.get("schema") if isinstance(contract, Mapping) else None):
        if isinstance(column, Mapping):
            attribute = column.get("sharedAttribute")
            if isinstance(column.get("name"), str) and isinstance(attribute, str) and attribute:
                return True
    return False


def bound_into_h(declared: bool, expose: Mapping[str, Any]) -> Optional[bool]:
    """The member of an emit's entry for ``H`` that ``copies: true`` makes, or ``None``.

    ``True`` when the Simulation asks for copies and the emit's *expose* declares something to
    copy; ``None`` otherwise, so the entry gains no key.
    """
    return True if declared and declares_copies(expose) else None


def copies_of(extension: Optional[Mapping[str, Any]]) -> bool:
    """Whether an emit's bound extension holds the member :func:`bound_into_h` made."""
    return extension is not None and extension.get(COPIES_KEY) is True
