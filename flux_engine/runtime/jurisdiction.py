# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which jurisdiction a market sits in (the ``ConsentProfile`` gate's missing edge).

``ConsentProfile.spec.allowedJurisdictions`` is a list of jurisdiction codes
(``[DE, FR, NL]``) and ``World.spec.population.markets`` is a list of market
names (``[mkt-a, mkt-b, mkt-c]``). FLUX 0.5.0 declares no edge between them, so
a campaign cannot answer "is this subject in an allowed jurisdiction?" from the
documents alone. The edge is engine-owned, per vertical, and lives here.

Like :mod:`flux_engine.world.derived` this is the **v0.1 stopgap** the vertical
pack's own docstring anticipates ("FLUX 0.6 RFC moves the declaration into the
spec"). It is looked up on the pack first -- a pack that grows a
``market_jurisdictions`` attribute takes precedence over this table without any
change here -- and falls back to the table below. See the REQUESTS section of
this work item for the pack-side diff.

An unmapped market is ``FLX-4110``, never a default: guessing a jurisdiction
would either suppress a lawful delivery or allow an unlawful one, and both are
governance claims the documents do not support.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from flux_engine.core.errors import FluxError
from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import nfc
from flux_engine.runtime.errors import FLX_4110

__all__ = ["MARKET_JURISDICTIONS", "jurisdiction_map", "jurisdiction_of"]

#: The attribute a vertical pack may carry to supersede the table below.
PACK_ATTRIBUTE = "market_jurisdictions"

#: Market -> ISO 3166-1 alpha-2 jurisdiction, per vertical. The telco row maps
#: the shipped example's three markets onto the three jurisdictions
#: ``cp-telco-eu`` allows, in the markets' own sorted order. The towers
#: example names each market by its ISO 3166-1 alpha-2 country code, so its row
#: is the identity over the six markets it declares.
MARKET_JURISDICTIONS: Mapping[str, Mapping[str, str]] = {
    "telco": {"mkt-a": "DE", "mkt-b": "FR", "mkt-c": "NL"},
    "towers": {code: code for code in ("DZ", "IQ", "JO", "KW", "QA", "TN")},
}


def jurisdiction_map(pack: VerticalPack) -> Mapping[str, str]:
    """The market -> jurisdiction table for *pack*, NFC-keyed.

    The pack wins when it carries one; otherwise the engine-owned table is used;
    a vertical in neither gets an empty map, which makes every market
    ``FLX-4110`` at the point of the decision rather than at import time -- a
    vertical with no campaigns is a legitimate configuration.
    """
    declared: Optional[Any] = getattr(pack, PACK_ATTRIBUTE, None)
    source: Mapping[str, str] = (
        declared if isinstance(declared, Mapping) else MARKET_JURISDICTIONS.get(nfc(pack.name), {})
    )
    return {nfc(str(market)): nfc(str(code)) for market, code in sorted(source.items())}


def jurisdiction_of(pack: VerticalPack, market: Optional[str]) -> str:
    """The jurisdiction *market* sits in; ``FLX-4110`` when none is declared."""
    table = jurisdiction_map(pack)
    key = nfc(market) if market is not None else ""
    if key not in table:
        raise FluxError(
            FLX_4110,
            f"vertical {pack.name!r} declares no jurisdiction for market "
            f"{market!r}; it maps {', '.join(sorted(table)) or 'no markets'}",
        )
    return table[key]
