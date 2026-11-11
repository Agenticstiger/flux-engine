# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Saving a falsifying example so it can be committed as a regression case.

``FLUX_ACCURACY_COUNTEREXAMPLES`` names a directory. When an assertion inside
:func:`saving_counterexample` fails and the variable is set, the input that
failed is written there as ``{"property", "input", "tier", "baseSeed"}`` and
the original exception is re-raised unchanged. The nightly workflow uploads
the directory with its artefact; a person then copies the file into
``tests/accuracy/properties/regressions/``, where ``test_regressions_replay``
runs it on every pull request.

Hypothesis calls a failing test body many times while it shrinks, and the last
failing call is the minimal example. So the file name is a function of the
property and the process only (``<property>.<worker>.json``) and each failure
overwrites the previous one: what is left on disk is the shrunk example, not
dozens of intermediate ones.

The payload is made JSON-able with the result log's own converter, so a
``Decimal`` is written as its exact string spelling, never as a float.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from tests.accuracy.harness.record import jsonable
from tests.accuracy.harness.tier import current_tier

__all__ = ["COUNTEREXAMPLES_ENV", "saving_counterexample"]

COUNTEREXAMPLES_ENV = "FLUX_ACCURACY_COUNTEREXAMPLES"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _directory() -> Optional[Path]:
    raw = os.environ.get(COUNTEREXAMPLES_ENV, "").strip()
    return Path(raw) if raw else None


def _write(property_name: str, payload: Any) -> None:
    directory = _directory()
    if directory is None:
        return
    tier = current_tier()
    directory.mkdir(parents=True, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    stem = _SAFE.sub("_", f"{property_name}.{worker}")
    document = {
        "property": property_name,
        "input": jsonable(payload),
        "tier": tier.name,
        "baseSeed": tier.base_seed,
    }
    target = directory / f"{stem}.json"
    staging = directory / f".{stem}.tmp"
    staging.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staging, target)


@contextmanager
def saving_counterexample(property_name: str, payload: Any) -> Iterator[None]:
    """Run the body; if it raises, save *payload* as a counterexample and re-raise.

    Any ``Exception`` is a failed property and is saved. A pytest skip is not
    an ``Exception``, and a hypothesis rejection (``UnsatisfiedAssumption``)
    is not a counterexample, so neither is saved.
    """
    try:
        yield
    except Exception as exc:
        if type(exc).__module__.split(".")[0] in {"_pytest", "hypothesis"}:
            raise
        _write(property_name, payload)
        raise
