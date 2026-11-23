# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The environment a generator plugin ran in, measured by the engine (ADR 0032 section 6).

A learned model often gives the same values only in the environment that ran it, so a plan that
uses a plugin records a digest of that environment, and ``replay`` measures it again to know
whether generating the rows again can be expected to give the stored bytes. The plugin never
states it: the engine reads it from the interpreter and from the installed distributions.

The document is the ``jcs-document`` digest's subject:

.. code-block:: json

    {
      "interpreter": {"implementation": "cpython", "version": "3.12.4"},
      "platform": {"system": "Linux", "machine": "x86_64"},
      "distributions": [["flux-engine", "0.9.0"], ["numpy", "2.1.0"]]
    }

The distributions are every one ``importlib.metadata`` finds, as ``[normalized name, version]``,
each pair once, sorted by name and then version. Nothing here imports a plugin.

Standard library and the engine's digest module only.
"""

from __future__ import annotations

import platform
import sys
from importlib import metadata
from typing import Any, Dict, List

from flux_engine.core.digest import digest_of_document
from flux_engine.generators.registry import normalize_distribution

__all__ = ["environment_digest", "environment_document"]


def _distributions() -> List[List[str]]:
    """Every installed distribution as ``[normalized name, version]``, sorted, each pair once."""
    found = set()
    for item in metadata.distributions():
        name = item.metadata["Name"]
        if not name:
            continue  # metadata with no name cannot be told apart from another
        found.add((normalize_distribution(name), str(item.version or "")))
    return [[name, version] for name, version in sorted(found)]


def environment_document() -> Dict[str, Any]:
    """The interpreter, the platform and every installed distribution, as a plain document."""
    return {
        "interpreter": {
            "implementation": sys.implementation.name,
            "version": platform.python_version(),
        },
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "distributions": _distributions(),
    }


def environment_digest() -> str:
    """The ``jcs-document`` digest of :func:`environment_document`, as ``sha256:`` and 64 hex digits.

    The one function ``plan`` records it with and ``replay`` measures it with.
    """
    return digest_of_document(environment_document(), subject="generatorEnvironment").value
