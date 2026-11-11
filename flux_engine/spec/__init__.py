# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shim over the ``flux_spec`` distribution (validate/enforce/bundle).

``flux_spec`` is the public distribution published from
``Agenticstiger/flux``. It is a declared dependency of this package
(``[project.dependencies]`` in pyproject.toml, a PyPI version range), so an
ordinary install gets it; ``flux_engine._vendor.flux_spec`` remains as a fallback for an
environment that has to run without it -- an offline machine, or an older
flux_engine pinned from before the distribution existed.

Which backend answered is not a detail: ``flux_spec`` supplies the
document validator, the RFC-04 enforcement decision and the RFC-09
evidence bundler. :data:`SOURCE` and :data:`SOURCE_VERSION` report it,
and ``fluxsim describe --self`` surfaces it. An installed ``flux_spec``
outside :data:`SUPPORTED_SPEC_VERSION_RANGE` is **refused** at import
rather than used: a different enforcement decision silently substituted
for the pinned one is the failure this shim exists to prevent.

Import ``flux_engine.spec.validate`` / ``.enforce`` / ``.bundle`` rather
than reaching into ``flux_engine._vendor`` directly -- the sibling modules
in this package are what the rest of flux_engine (and import-linter's
architecture contracts) are written against. In particular, only
``flux_engine.governance.gate`` may import ``flux_engine.spec.enforce``
(see the ``[tool.importlinter]`` contracts in pyproject.toml); every other
enforcement-gate consumer goes through that module.

Data files -- the FLUX schema, the RFC-04 conformance vectors -- are
resolved by asking the *active backend* for the constant it already
computed (``validate.FLUX_SCHEMA_PATH``, ``enforce.DEFAULT_VECTORS``,
present in both backends), never by rebuilding a directory layout from
``__file__``. The two probes that did the latter looked for
``flux_spec/vectors/`` and ``flux_spec/schema/``; the published
distribution keeps its data under ``flux_spec/data/`` and resolves it
through ``importlib.resources``, so both probes missed and were swallowed
by an ``exists()`` check -- the engine took its code from the
distribution and its schema and vectors from the vendored copy, with
:data:`SOURCE` naming the wrong provenance and nothing said out loud.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Optional, Tuple

__all__ = [
    "SOURCE",
    "SOURCE_VERSION",
    "SUPPORTED_SPEC_VERSION_RANGE",
    "default_vectors_path",
    "flux_schema_path",
]

#: ``"flux_spec"`` when the public distribution is installed, ``"vendored"``
#: when flux_engine falls back to its bundled copy. Derived once at import
#: time via ``importlib.util.find_spec`` -- never a constant someone has to
#: remember to flip.
SOURCE = "flux_spec" if importlib.util.find_spec("flux_spec") is not None else "vendored"

_MIN_SPEC_VERSION: Tuple[int, int] = (0, 5)
_MAX_SPEC_VERSION_EXCLUSIVE: Tuple[int, int] = (0, 6)

#: The ``flux_spec`` versions this engine is pinned against; the vendored
#: fallback is a copy of the same line.
SUPPORTED_SPEC_VERSION_RANGE = ">=0.5,<0.6"


def _version_tuple(version: str) -> Tuple[int, int]:
    parts = version.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError as exc:  # pragma: no cover - defensive; upstream is semver
        raise RuntimeError(f"flux_spec reports an unreadable version {version!r}") from exc


def _resolve_source_version() -> Optional[str]:
    """The installed distribution's version, refusing one outside the pin.

    Returns ``None`` for the vendored backend, which ships no version of
    its own -- ``flux_engine/_vendor/flux_spec/VENDOR-NOTES.md`` records
    the upstream commit it was taken from instead.
    """
    if SOURCE != "flux_spec":
        return None
    import flux_spec

    version = str(getattr(flux_spec, "__version__", "0"))
    if not (_MIN_SPEC_VERSION <= _version_tuple(version) < _MAX_SPEC_VERSION_EXCLUSIVE):
        raise RuntimeError(
            f"flux_spec {version} is installed, but this flux_engine is pinned to "
            f"flux_spec {SUPPORTED_SPEC_VERSION_RANGE}. flux_spec supplies the document "
            "validator, the RFC-04 enforcement decision and the evidence bundler; running "
            "an unpinned one would change those answers silently. Install a supported "
            "flux_spec (see [project.dependencies] in pyproject.toml), or uninstall it to "
            "fall back to the vendored copy."
        )
    return version


#: The active backend's version: the installed distribution's
#: ``__version__``, or ``None`` for the vendored fallback.
SOURCE_VERSION: Optional[str] = _resolve_source_version()


def _backend(module: str) -> ModuleType:
    """Import ``<active backend>.<module>`` -- ``validate``, ``enforce`` or ``bundle``."""
    if SOURCE == "flux_spec":
        return importlib.import_module(f"flux_spec.{module}")
    return importlib.import_module(f"flux_engine._vendor.flux_spec.{module}")


def default_vectors_path() -> Path:
    """Return the path to the RFC-04 conformance vectors of the active backend.

    Asks the backend for the constant it already resolved rather than
    guessing where it keeps its data -- so the code answering an
    enforcement question and the vectors that check that answer always
    come from the same copy.
    """
    return Path(_backend("enforce").DEFAULT_VECTORS)


def flux_schema_path() -> Path:
    """Return the path to ``flux-schema-latest.json`` of the active backend.

    Same rule as :func:`default_vectors_path`: one backend supplies both
    the validator and the schema it validates against, by construction.
    """
    return Path(_backend("validate").FLUX_SCHEMA_PATH)
