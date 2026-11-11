# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Totality guard for ``fluxsim explain``: every ``FLX-nnnn`` code the engine
can actually raise is both registered (after
:func:`flux_engine.core.errors.ensure_core_codes_loaded`) and explainable
through the real CLI command -- closing a gap where "fluxsim explain cannot
explain 42 of the 72 registered codes" (a registered-but-unloaded code was
reported with the same sentence as a nonexistent one).

The source of truth for "every code the engine can raise" is the source
itself: every ``FLX-nnnn`` literal under ``flux_engine/`` (the ``_vendor``
copy of the third-party ``flux_spec`` shim is excluded -- its codes, if
any, are that project's catalog, not this engine's KERNEL-SPEC S13
bands). A code that appears only in prose (a docstring or comment
mentioning one FLX-nnnn as an example) would still need to be registered
for this test to pass -- ``register()`` is idempotent, so a module that
already registers the code for real absorbs a stray mention for free;
there are none today (checked: every source hit is a live ``register()``
call or a direct/format-string reference to one already registered).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import ensure_core_codes_loaded, is_registered

REPO_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_ROOT = REPO_ROOT / "flux_engine"
_CODE_PATTERN = re.compile(r"FLX-\d{4}")


def _codes_mentioned_in_source() -> Set[str]:
    codes: Set[str] = set()
    for path in _ENGINE_ROOT.rglob("*.py"):
        if "_vendor" in path.parts or "__pycache__" in path.parts:
            continue
        codes.update(_CODE_PATTERN.findall(path.read_text(encoding="utf-8")))
    return codes


@pytest.mark.unit
def test_every_source_flx_code_is_registered_after_loading() -> None:
    ensure_core_codes_loaded()
    codes = _codes_mentioned_in_source()
    assert codes, "the grep found nothing -- the pattern or the source tree moved"
    unregistered = sorted(code for code in codes if not is_registered(code))
    assert unregistered == [], (
        f"{len(unregistered)} FLX code(s) appear in flux_engine/ source but are not "
        f"registered after ensure_core_codes_loaded(): {unregistered}. "
        "Add the owning module to _CORE_CODE_MODULES in flux_engine/core/errors.py."
    )


@pytest.mark.unit
def test_flux_explain_resolves_every_source_flx_code(capsys) -> None:
    ensure_core_codes_loaded()
    codes = sorted(_codes_mentioned_in_source())
    assert codes

    failures = []
    for code in codes:
        exit_code = main(["explain", code])
        out = capsys.readouterr().out
        if exit_code != 0 or code not in out:
            failures.append((code, exit_code))
    assert failures == [], f"fluxsim explain failed for: {failures}"


@pytest.mark.unit
def test_explain_a_stages_layer_code_not_just_a_core_one(capsys) -> None:
    """Coverage gap: the pre-existing test used FLX-1001, a core code that
    was always reachable; the actual failure mode was codes owned by the
    newer stages/runtime/world/seam layers.
    """
    assert main(["explain", "FLX-3105"]) == 0
    assert "FLX-3105" in capsys.readouterr().out
