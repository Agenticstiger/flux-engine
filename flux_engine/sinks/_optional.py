# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One gate for the optional libraries a sink needs.

:func:`require` returns the imported module, or raises ``FLX-3501`` with the
exact install line. The pattern is pandas' ``import_optional_dependency``
(``pandas/compat/_optional.py``, BSD-3-Clause), with two differences taken
from the same idea in forge-cli (``fluid_build/_optional.py``):

* The package is located with :func:`importlib.util.find_spec` before it is
  imported, so a missing package never half-imports.
* A package that is installed but fails to import re-raises its own error.
  Reporting a broken install as "not installed" would send the user to a
  ``pip install`` that changes nothing.

Standard library only, so importing this module costs nothing on the cold
``fluxsim --help`` path.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType

from flux_engine.core.errors import FluxError
from flux_engine.sinks.errors import FLX_3501

__all__ = ["DISTRIBUTION", "is_available", "require"]

#: The distribution that provides an import name, where the two differ.
DISTRIBUTION = {
    "adbc_driver_manager": "adbc-driver-manager",
    "adbc_driver_duckdb": "duckdb",
    "adbc_driver_sqlite": "adbc-driver-sqlite",
    "adbc_driver_postgresql": "adbc-driver-postgresql",
    "adbc_driver_snowflake": "adbc-driver-snowflake",
    "adbc_driver_bigquery": "adbc-driver-bigquery",
}


def _present(module: str) -> bool:
    """Whether *module* can be found, without importing it."""
    if module in sys.modules:
        # Present, or deliberately blocked: a test that sets the entry to
        # None is saying "pretend this is not installed".
        return sys.modules[module] is not None
    top = module.split(".", 1)[0]
    try:
        if importlib.util.find_spec(top) is None:
            return False
        # find_spec("a.b") raises ModuleNotFoundError when "a" is absent
        # (CPython bpo-30436), which is why the top level is probed first.
        return module == top or importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def is_available(module: str) -> bool:
    """Whether *module* is installed, without importing it."""
    return _present(module)


def require(module: str, *, extra: str, feature: str) -> ModuleType:
    """Import *module*, or raise ``FLX-3501`` naming the extra that provides it.

    *feature* names what needs the module ("the Parquet sink"), *extra* the
    ``flux-sim-engine[...]`` extra that installs it. A driver that no extra
    installs (the ADBC driver for one database) passes ``extra=""`` and the
    message names the package to install instead.
    """
    top = module.split(".", 1)[0]
    package = DISTRIBUTION.get(top, top)
    if not _present(module):
        if extra:
            hint = f"pip install 'flux-sim-engine[{extra}]'"
        else:
            hint = f"pip install {package}"
        raise FluxError(FLX_3501, f"{feature} needs {package}, which is not installed: {hint}")
    return importlib.import_module(module)
