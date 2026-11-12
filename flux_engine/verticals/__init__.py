# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Vertical attribute packs keyed by ``World.spec.vertical`` (v0.1 stopgap).

EXPR-SPEC §3 sources the vertical slice of the attribute model from
``World.spec.vertical`` -> ``verticals/<pack>.py``, and marks it explicitly as
a "v0.1 stopgap; FLUX 0.6 RFC moves the declaration into the spec". A pack
therefore declares **types only** -- no values, no rates, no sampling -- so
that moving the declaration into a FLUX document later changes where the
names come from and nothing about what they mean.

A pack is looked up by the string in ``World.spec.vertical``::

    from flux_engine.verticals import get_pack

    pack = get_pack(world["spec"]["vertical"])

The registry is closed at import time and keyed by NFC-normalised name, so the
lookup is deterministic and an unknown vertical fails by name.
"""

from __future__ import annotations

from typing import Mapping, Tuple

from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import nfc
from flux_engine.verticals.telco import TELCO, TELCO_EMIT_ALIASES
from flux_engine.verticals.towers import TOWERS, TOWERS_EMIT_ALIASES

__all__ = [
    "EMIT_ALIASES",
    "PACKS",
    "VerticalPack",
    "emit_aliases",
    "get_pack",
    "pack_names",
]

#: Every pack this engine ships, keyed by the ``World.spec.vertical`` string.
PACKS: Mapping[str, VerticalPack] = {TELCO.name: TELCO, TOWERS.name: TOWERS}

#: Per-vertical expose-column aliases -- step 2 of
#: :func:`flux_engine.seam.mapping.resolve_column_mapping`. Kept beside the
#: packs rather than inside :class:`~flux_engine.expr.attrs.VerticalPack`
#: because a pack is EXPR-SPEC §3's attribute-model slice (what an
#: expression may name) and an alias is a seam concern (which declared
#: column a runtime attribute fills). Two questions, two tables, one
#: registry.
EMIT_ALIASES: Mapping[str, Mapping[str, str]] = {
    TELCO.name: TELCO_EMIT_ALIASES,
    TOWERS.name: TOWERS_EMIT_ALIASES,
}


def pack_names() -> Tuple[str, ...]:
    """Return the registered vertical names, sorted."""
    return tuple(sorted(PACKS))


def get_pack(name: str) -> VerticalPack:
    """Return the pack named by ``World.spec.vertical``.

    Raises ``KeyError`` naming the known packs when *name* is not one of them:
    a missing pack would silently strip the vertical attributes out of the
    subject scope and turn every reference to one into ``FLX-6004``, which is
    a confusing failure a long way from its cause.
    """
    key = nfc(name)
    if key not in PACKS:
        raise KeyError(f"unknown vertical {name!r}; this engine ships {', '.join(pack_names())}")
    return PACKS[key]


def emit_aliases(name: str) -> Mapping[str, str]:
    """Return the expose-column alias table for vertical *name*.

    Empty for a registered vertical that declares none: an alias table is
    a convenience, and a vertical whose contracts name their columns the
    way the runtime names its attributes needs no entries. Raises
    ``KeyError`` for an unregistered vertical, the same way
    :func:`get_pack` does -- one lookup rule for both tables.
    """
    key = nfc(name)
    if key not in PACKS:
        raise KeyError(f"unknown vertical {name!r}; this engine ships {', '.join(pack_names())}")
    return EMIT_ALIASES.get(key, {})
