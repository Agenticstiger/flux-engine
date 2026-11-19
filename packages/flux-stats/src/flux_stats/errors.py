# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``FLX-nnnn`` code catalog and the one exception type FLUX raises.

Implements KERNEL-SPEC §13 ("Error code ranges"): every diagnostic the
engine and this package can emit is a numbered code in a band that names
its subsystem. The engine's ``fluxsim explain FLX-nnnn`` prints the
catalog entry, and :func:`explain` renders the same text here.

    1000 canonicalisation · 2000 plan/replay binding · 3000 seam/conformance
    4000 governance/sovereignty · 5000 numerics · 6000 expression language
    7000 calibration · 8000 evidence/pack

**Where codes are declared.** The catalog is populated at import time by
the module that actually raises the code, via :func:`register`, not by a
central table here. A table here would drift: a code could be listed with
no raiser, or a raiser could invent a code the table never heard of.
Because the catalog is filled by import, anything that wants the *whole*
catalog (the ``explain`` command, a documentation generator) must first
import every module that raises a code. Importing :mod:`flux_stats` does
that for this package's own codes; the engine's
``flux_engine.core.errors.ensure_core_codes_loaded`` does it for the
engine's codes and this package's together.

The engine imports these names through ``flux_engine.core.errors``, which
re-exports the objects defined here. A process therefore holds one catalog
and one :class:`FluxError`, whichever of the two modules a caller names.

Registration is idempotent for an identical entry (so a module reload is
harmless) and refuses a conflicting redefinition of a code that is
already taken -- two subsystems cannot quietly share one number.

This module holds no state a run depends on: it reads no clock, draws no
randomness, and its ordering (:func:`catalog`) is by sorted code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple, Union

__all__ = [
    "BAND_SIZE",
    "CATEGORIES",
    "ErrorCode",
    "FluxError",
    "band_of",
    "catalog",
    "category_of",
    "explain",
    "get",
    "is_registered",
    "register",
]

#: Width of one code band. KERNEL-SPEC §13 spells the bands as round
#: thousands, so the band of ``FLX-5002`` is ``5000``.
BAND_SIZE = 1000

#: The eight bands of KERNEL-SPEC §13, keyed by band floor. A code
#: outside these bands is refused by :func:`register` -- the set is
#: closed, and widening it is a specification change, not a caller's
#: choice.
CATEGORIES: Mapping[int, str] = MappingProxyType(
    {
        1000: "canonicalisation",
        2000: "plan/replay binding",
        3000: "seam/conformance",
        4000: "governance/sovereignty",
        5000: "numerics",
        6000: "expression language",
        7000: "calibration",
        8000: "evidence/pack",
    }
)

_CODE_PATTERN = re.compile(r"^FLX-(\d{4})$")


@dataclass(frozen=True)
class ErrorCode:
    """One catalog entry: the code, its one-line title, its explanation.

    ``explanation`` is operator-facing prose -- it says what the engine
    saw and what the reader can do about it. It never contains a value
    from the data being processed (KERNEL-SPEC §9: diagnostics name a
    column, a row index and a value *class*, never the value).
    """

    code: str
    title: str
    explanation: str

    @property
    def number(self) -> int:
        """The numeric part of the code, e.g. ``1001``."""
        match = _CODE_PATTERN.match(self.code)
        if match is None:  # pragma: no cover - unreachable via register()
            raise ValueError(f"malformed FLX code: {self.code!r}")
        return int(match.group(1))

    @property
    def band(self) -> int:
        """The band floor this code sits in, e.g. ``1000``."""
        return band_of(self.code)

    @property
    def category(self) -> str:
        """The KERNEL-SPEC §13 subsystem name for this code's band."""
        return CATEGORIES[self.band]


class FluxError(Exception):
    """The single exception type the kernel raises, always carrying a code.

    ``str(err)`` renders as ``FLX-1001: <title> -- <detail>`` so a log
    line, a CLI message and a test assertion all agree on one shape.
    """

    def __init__(self, code: Union[str, ErrorCode], detail: str = "") -> None:
        entry = code if isinstance(code, ErrorCode) else get(code)
        self.code: ErrorCode = entry
        self.detail: str = detail
        message = f"{entry.code}: {entry.title}"
        if detail:
            message = f"{message} -- {detail}"
        super().__init__(message)


_CATALOG: Dict[str, ErrorCode] = {}


def band_of(code: str) -> int:
    """Return the band floor of *code*, validating its shape first."""
    match = _CODE_PATTERN.match(code)
    if match is None:
        raise ValueError(f"malformed FLX code: {code!r} (expected 'FLX-nnnn')")
    return (int(match.group(1)) // BAND_SIZE) * BAND_SIZE


def category_of(code: str) -> str:
    """Return the KERNEL-SPEC §13 subsystem name for *code*'s band."""
    band = band_of(code)
    if band not in CATEGORIES:
        raise ValueError(f"{code}: no KERNEL-SPEC §13 band for {band}")
    return CATEGORIES[band]


def register(code: str, title: str, explanation: str) -> ErrorCode:
    """Add *code* to the catalog and return its entry.

    Called at import time by the module that raises the code. Refuses a
    malformed code, a code outside the eight bands of KERNEL-SPEC §13, an
    empty title or explanation, and a redefinition that disagrees with an
    entry already registered under the same number. Re-registering the
    identical entry returns the existing one, so importing a module twice
    is harmless.
    """
    category_of(code)  # validates shape and band membership
    if not title.strip():
        raise ValueError(f"{code}: title must not be empty")
    if not explanation.strip():
        raise ValueError(f"{code}: explanation must not be empty")

    entry = ErrorCode(code=code, title=title, explanation=explanation)
    existing = _CATALOG.get(code)
    if existing is not None:
        if existing != entry:
            raise ValueError(
                f"{code} is already registered as {existing.title!r}; "
                "two subsystems must not share one code"
            )
        return existing
    _CATALOG[code] = entry
    return entry


def is_registered(code: str) -> bool:
    """Return whether *code* is in the catalog right now."""
    return code in _CATALOG


def get(code: str) -> ErrorCode:
    """Return the catalog entry for *code*.

    Raises ``KeyError`` when the owning module has not been imported --
    see the module docstring for what loads the whole catalog.
    """
    try:
        return _CATALOG[code]
    except KeyError:
        raise KeyError(
            f"{code} is not registered; the module that raises it may not be imported yet"
        ) from None


def catalog(band: Optional[int] = None) -> Tuple[ErrorCode, ...]:
    """Return every registered entry, sorted by code.

    Pass *band* (a band floor such as ``5000``) to restrict the result to
    one subsystem.
    """
    entries = sorted(_CATALOG.values(), key=lambda entry: entry.code)
    if band is None:
        return tuple(entries)
    return tuple(entry for entry in entries if entry.band == band)


def explain(code: str) -> str:
    """Render the catalog entry for *code* as the text ``fluxsim explain`` prints."""
    entry = get(code)
    return f"{entry.code}  [{entry.category}]\n{entry.title}\n\n{entry.explanation}"
