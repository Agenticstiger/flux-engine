# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's view of the ``FLX-nnnn`` catalog, and the modules that fill it.

The catalog itself, its eight bands (KERNEL-SPEC §13) and :class:`FluxError`
live in :mod:`flux_stats.errors`, so the statistics a profiler shares with
the engine number their refusals in the same catalog without importing the
engine. This module re-exports those objects unchanged: engine code,
``fluxsim explain`` and a sink plugin import them from here, and a process
still holds one catalog and one :class:`FluxError`.

What the engine adds is the list of the modules that own codes. The catalog
is filled by import, by the module that raises each code, so anything that
wants the *whole* catalog (the ``explain`` command, a documentation
generator) must first call :func:`ensure_core_codes_loaded`, which imports
every module that owns codes in the core bands, the engine's and
flux-stats's alike.
"""

from __future__ import annotations

from typing import Tuple

from flux_stats.errors import (
    BAND_SIZE,
    CATEGORIES,
    ErrorCode,
    FluxError,
    band_of,
    catalog,
    category_of,
    explain,
    get,
    is_registered,
    register,
)

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
    "flux_engine.core.kernel2",
    "flux_engine.core.lrm",
    "flux_engine.core.rng",
    "flux_engine.core.seedtree",
    "flux_engine.core.trace",
    "flux_engine.expr.errors",
    "flux_engine.expr2.errors",
    "flux_engine.calibrate.declared",
    "flux_engine.calibrate.errors",
    "flux_engine.consumers.errors",
    "flux_engine.accuracy.records",
    "flux_engine.accuracy.policy",
    "flux_engine.evidence.report",
    "flux_engine.api.accuracy",
    "flux_engine.concordance.errors",
    "flux_engine.consumers.runner_errors",
    "flux_engine.consumers.verify_errors",
    "flux_engine.generator.families",
    "flux_engine.generator.relations",
    "flux_engine.generator.statistics",
    "flux_engine.generators.errors",
    "flux_engine.governance.audit",
    "flux_engine.governance.gate",
    "flux_engine.governance.privacy",
    "flux_engine.governance.skills",
    "flux_engine.governance.sovereignty",
    "flux_engine.outcomes.drift",
    "flux_engine.outcomes.forecast",
    "flux_engine.outcomes.responses",
    "flux_engine.outcomes.scoring",
    "flux_engine.safeio",
    "flux_engine.runtime.errors",
    "flux_engine.seam.attest",
    "flux_engine.seam.conform",
    "flux_engine.seam.consumers",
    "flux_engine.seam.contract",
    "flux_engine.seam.coverage",
    "flux_engine.seam.emit",
    "flux_engine.seam.keys",
    "flux_engine.seam.parameters",
    "flux_engine.seam.profile",
    "flux_engine.seam.resolve",
    "flux_engine.sinks.errors",
    "flux_engine.core.sizing",
    "flux_engine.stages.errors",
    "flux_engine.stages.layout",
    "flux_engine.stages.staging",
    "flux_engine.verticals",
    "flux_engine.world.errors",
    # flux-stats (ADR 0009): the canonical form's FLX-1001 and FLX-1003..1007,
    # the exact YAML read's FLX-3105, the fixed-point grid's FLX-5001..5005, the
    # spellings' FLX-5036, the record reader's FLX-5022 and FLX-5031, and the fidelity
    # block's FLX-3001, FLX-5030, FLX-5032, FLX-5033 and FLX-5034. The
    # engine raises and explains them too.
    "flux_stats.canonical",
    "flux_stats.exact_yaml",
    "flux_stats.fidelity",
    "flux_stats.fixedpoint",
    "flux_stats.reading",
    "flux_stats.spelling",
)


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
        except ImportError as error:
            # A distribution missing beneath one of these modules (flux_spec,
            # whose shim names the command that installs it) is not a module
            # that moved: let its own message through.
            if error.name and not error.name.startswith(("flux_engine", "flux_stats")):
                raise
            missing.append(module_name)
    if missing:
        raise ImportError(
            "the FLX catalog is incomplete: "
            + ", ".join(missing)
            + " could not be imported. Update _CORE_CODE_MODULES if a module moved."
        )
