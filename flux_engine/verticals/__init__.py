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

A World's pack is looked up by the string in its ``spec.vertical``::

    from flux_engine.verticals import pack_of_world

    pack = pack_of_world(world)

The registry is closed at import time and keyed by NFC-normalised name, so the
lookup is deterministic and an unknown vertical fails by name: ``FLX-5042``
from :func:`pack_of_world`, ``KeyError`` from :func:`get_pack`, which looks up
one name.
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple

from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import nfc
from flux_engine.verticals.telco import TELCO, TELCO_EMIT_ALIASES
from flux_engine.verticals.towers import TOWERS, TOWERS_EMIT_ALIASES

__all__ = [
    "EMIT_ALIASES",
    "FLX_5042",
    "PACKS",
    "VerticalPack",
    "emit_aliases",
    "get_pack",
    "pack_names",
    "pack_of_world",
]

FLX_5042: ErrorCode = register(
    "FLX-5042",
    "the World names no vertical this engine ships",
    "A run builds its population from the vertical pack its World names in "
    "`spec.vertical`: the pack declares which attributes each simulated subject carries "
    "and which fields each signal's payload holds, and its alias table maps expose "
    "columns onto them. The FLUX schema leaves `spec.vertical` optional, so the FLUX "
    "reference validator (`python3 -m flux_spec.validate`) passes a World that names no "
    "vertical, or names one this engine does not ship. This engine then has nothing to "
    "build a subject from, even when a fidelity block declares every column. Add "
    "`vertical:` under the World's `spec`, naming one of the packs `fluxsim plugins` "
    "lists, for example `vertical: telco`. `fluxsim validate` refuses the bundle before "
    "anything runs, and `plan`, `run` and `expr` give the same refusal.",
)

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


def pack_of_world(world: Mapping[str, Any]) -> VerticalPack:
    """Return the pack the World document *world* names in ``spec.vertical``.

    Refuses with ``FLX-5042`` when the World names no vertical, or one this
    engine does not ship. The FLUX schema leaves ``spec.vertical`` optional, so
    the FLUX reference validator passes such a World; ``validate`` asks here before
    anything runs, and ``plan``, ``run`` and ``expr`` ask here for the pack they
    build from, so every command refuses it with one code and one message.
    """
    spec = world.get("spec")
    name = spec.get("vertical") if isinstance(spec, Mapping) else None
    where = f"World {world.get('id')!r}"
    shipped = ", ".join(pack_names())
    if name is None:
        raise FluxError(
            FLX_5042,
            f"{where} declares no spec.vertical; add one under its spec, naming a pack "
            f"this engine ships ({shipped}), for example 'vertical: {pack_names()[0]}'",
        )
    if not isinstance(name, str) or nfc(name) not in PACKS:
        raise FluxError(
            FLX_5042,
            f"{where} declares spec.vertical {name!r}, which this engine does not ship; "
            f"use a pack it ships ({shipped})",
        )
    return get_pack(name)


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
