# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shim over the ``flux_spec`` distribution (validate/enforce/bundle).

``flux_spec`` is the public distribution published from
``Agenticstiger/flux``. It is a declared dependency of this package
(``[project.dependencies]`` in pyproject.toml, a PyPI version range), so an
ordinary install gets it, and it is the only place the engine takes the
specification from: the vendored fallback copy this package used to carry
is retired (ADR 0005, amended 2026-11-17). Without the distribution,
importing this package raises :class:`ModuleNotFoundError` with the
command that installs it.

What answers is not a detail: ``flux_spec`` supplies the document
validator, the RFC-04 enforcement decision and the RFC-09 evidence
bundler. :data:`SOURCE` and :data:`SOURCE_VERSION` report it, and
``fluxsim describe --self`` surfaces both. An installed ``flux_spec``
outside :data:`SUPPORTED_SPEC_VERSION_RANGE` is **refused** at import
rather than used: a different enforcement decision silently substituted
for the pinned one is the failure this shim exists to prevent.

Import ``flux_engine.spec.validate`` / ``.enforce`` / ``.bundle`` rather
than ``flux_spec`` directly -- the sibling modules in this package are
what the rest of flux_engine (and import-linter's architecture contracts)
are written against. In particular, only ``flux_engine.governance.gate``
may import ``flux_engine.spec.enforce`` (see the ``[tool.importlinter]``
contracts in pyproject.toml); every other enforcement-gate consumer goes
through that module.

Data files -- the FLUX schema, the RFC-04 conformance vectors -- are
resolved by asking ``flux_spec`` for the constant it already computed
(``validate.FLUX_SCHEMA_PATH``, ``enforce.DEFAULT_VECTORS``), never by
rebuilding a directory layout from ``__file__``. The distribution keeps
its data under ``flux_spec/data/`` and resolves it through
``importlib.resources``; two probes that looked for ``flux_spec/vectors/``
and ``flux_spec/schema/`` once missed it, and the engine read its schema
and vectors from another copy without a word.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Tuple

__all__ = [
    "SOURCE",
    "SOURCE_VERSION",
    "SUPPORTED_SPEC_VERSION_RANGE",
    "default_vectors_path",
    "flux_schema_path",
]

_MIN_SPEC_VERSION: Tuple[int, int, int] = (0, 5, 4)
_MAX_SPEC_VERSION_EXCLUSIVE: Tuple[int, int, int] = (0, 6, 0)

#: The ``flux_spec`` versions this engine is pinned against. The floor is 0.5.4, the first
#: release that reads a bundle's files as UTF-8 whatever the locale, as the engine's own
#: pre-reads of them do (:mod:`flux_engine.seam.resolve`, :mod:`flux_engine.evidence.pack`).
SUPPORTED_SPEC_VERSION_RANGE = ">=0.5.4,<0.6"

if importlib.util.find_spec("flux_spec") is None:
    raise ModuleNotFoundError(
        "flux_engine needs the flux-spec distribution, which supplies the FLUX document "
        "validator, the RFC-04 enforcement decision and the evidence bundler, and it is "
        f"not installed. Install it: pip install 'flux-spec{SUPPORTED_SPEC_VERSION_RANGE}' "
        "(installing flux-sim-engine with pip installs it too).",
        name="flux_spec",
    )

#: The backend that answers: always ``"flux_spec"``, the published
#: distribution, now that it is the only one. Kept because
#: ``fluxsim describe --self`` reports it as ``spec_source``, and a host
#: reading that document keeps working.
SOURCE = "flux_spec"


def _version_tuple(version: str) -> Tuple[int, int, int]:
    """``major.minor.patch`` as numbers, a missing part 0 and a suffix (``rc1``, ``.dev0``) dropped."""
    found = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if found is None:  # pragma: no cover - defensive; upstream is semver
        raise RuntimeError(f"flux_spec reports an unreadable version {version!r}")
    major, minor, patch = (int(part) if part else 0 for part in found.groups())
    return major, minor, patch


def _resolve_source_version() -> str:
    """The installed distribution's version, refusing one outside the pin."""
    import flux_spec

    version = str(getattr(flux_spec, "__version__", "0"))
    if not (_MIN_SPEC_VERSION <= _version_tuple(version) < _MAX_SPEC_VERSION_EXCLUSIVE):
        raise RuntimeError(
            f"flux_spec {version} is installed, but this flux_engine is pinned to "
            f"flux_spec {SUPPORTED_SPEC_VERSION_RANGE}. flux_spec supplies the document "
            "validator, the RFC-04 enforcement decision and the evidence bundler; running "
            "an unpinned one would change those answers silently. Install a supported "
            "flux_spec (see [project.dependencies] in pyproject.toml)."
        )
    return version


#: The installed distribution's ``__version__``.
SOURCE_VERSION: str = _resolve_source_version()


def _backend(module: str) -> ModuleType:
    """Import ``flux_spec.<module>`` -- ``validate``, ``enforce`` or ``bundle``."""
    return importlib.import_module(f"flux_spec.{module}")


def default_vectors_path() -> Path:
    """Return the path to the RFC-04 conformance vectors ``flux_spec`` ships.

    Asks the distribution for the constant it already resolved rather than
    guessing where it keeps its data -- so the code answering an
    enforcement question and the vectors that check that answer always
    come from the same copy.
    """
    return Path(_backend("enforce").DEFAULT_VECTORS)


def flux_schema_path() -> Path:
    """Return the path to the ``flux-schema-latest.json`` ``flux_spec`` ships.

    Same rule as :func:`default_vectors_path`: one distribution supplies both
    the validator and the schema it validates against, by construction.
    """
    return Path(_backend("validate").FLUX_SCHEMA_PATH)
