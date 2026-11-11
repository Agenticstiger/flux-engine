# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``FLX-nnnn`` code catalog and the one exception type the kernel raises.

Implements KERNEL-SPEC §13 ("Error code ranges"): every diagnostic the
engine can emit is a numbered code in a band that names its subsystem,
and ``fluxsim explain FLX-nnnn`` prints the catalog entry.

    1000 canonicalisation · 2000 plan/replay binding · 3000 seam/conformance
    4000 governance/sovereignty · 5000 numerics · 6000 expression language
    7000 calibration · 8000 evidence/pack

**Where codes are declared.** The catalog is populated at import time by
the module that actually raises the code, via :func:`register`, not by a
central table here. A table here would drift: a code could be listed with
no raiser, or a raiser could invent a code the table never heard of.
Because the catalog is filled by import, anything that wants the *whole*
catalog (the ``explain`` command, a documentation generator) must first
call :func:`ensure_core_codes_loaded`, which imports the kernel modules
that own the core bands.

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
    "ensure_core_codes_loaded",
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

#: Every module that owns codes in one of the bands. Imported on demand
#: by :func:`ensure_core_codes_loaded` so a caller that needs the full
#: catalog gets one, without this module importing them at module scope
#: (which would be a cycle -- each of them imports :func:`register`).
#:
#: The import is by name through :func:`importlib.import_module`, so this
#: list creates no static import edge: ``flux_engine.core`` does not
#: depend on ``flux_engine.expr``, it is merely able to load its catalog
#: on request. A module added here must exist -- a missing entry would
#: make ``fluxsim explain`` answer "not registered" for a code the engine
#: can raise, so :func:`ensure_core_codes_loaded` fails loudly instead.
_CORE_CODE_MODULES: Tuple[str, ...] = (
    "flux_engine.core.canonical",
    "flux_engine.core.clock",
    "flux_engine.core.digest",
    "flux_engine.core.fixedpoint",
    "flux_engine.core.lrm",
    "flux_engine.core.rng",
    "flux_engine.core.seedtree",
    "flux_engine.core.trace",
    "flux_engine.expr.errors",
    "flux_engine.calibrate.declared",
    "flux_engine.accuracy.records",
    "flux_engine.accuracy.policy",
    "flux_engine.evidence.report",
    "flux_engine.api.accuracy",
    "flux_engine.concordance.errors",
    "flux_engine.generator.families",
    "flux_engine.generator.statistics",
    "flux_engine.governance.audit",
    "flux_engine.governance.gate",
    "flux_engine.governance.skills",
    "flux_engine.governance.sovereignty",
    "flux_engine.runtime.errors",
    "flux_engine.seam.attest",
    "flux_engine.seam.conform",
    "flux_engine.seam.contract",
    "flux_engine.seam.emit",
    "flux_engine.seam.resolve",
    "flux_engine.sinks.errors",
    "flux_engine.stages.errors",
    "flux_engine.stages.layout",
    "flux_engine.world.errors",
)


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
    see :func:`ensure_core_codes_loaded`.
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


def ensure_core_codes_loaded() -> None:
    """Import the kernel modules that own the core bands.

    The catalog is populated by import (see the module docstring), so any
    caller that needs the complete catalog -- ``fluxsim explain``, a docs
    generator, a totality test -- calls this first.
    """
    from importlib import import_module

    missing: list[str] = []
    for module_name in _CORE_CODE_MODULES:
        try:
            import_module(module_name)
        except ImportError:
            missing.append(module_name)
    if missing:
        raise ImportError(
            "the FLX catalog is incomplete: "
            + ", ".join(missing)
            + " could not be imported. Update _CORE_CODE_MODULES if a module moved."
        )
