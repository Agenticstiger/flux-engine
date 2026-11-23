# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A run on kernel 0.1.0 names the kernel first when a bundle declares what only kernel 0.2.0 generates.

``lifetimes``, ``within``, ``rates``, ``hiddenColumns`` and ``hidden`` are refused with ``FLX-3105``
naming both kernels, whatever else is wrong with the declaration. Kernel 0.2.0 still reads what the
key holds and refuses it for that reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flux_engine.stages.plan import plan
from tests.fixtures.temporal.bundle import copy_bundle

pytestmark = pytest.mark.integration

#: A ``within`` with no lifetime for the emit it follows: kernel 0.2.0 refuses it for that.
ONLY_WITHIN = "within:\n  - { emit: account, relationship: customer, date: a_open }\n"
MALFORMED_RATES = "rates: nonsense\n"
ONLY_LIFETIMES = "lifetimes:\n  - { emit: customer, start: no_such_column }\n"
EMIT_LEVEL = {
    "hiddenColumns": "        hiddenColumns: nonsense\n",
    "hidden": "        hidden: nonsense\n",
}


def _refusal(bundle: Path, tmp_path: Path, kernel: str) -> str:
    result = plan(bundle, state_dir=tmp_path / "state", kernel=kernel)
    assert result.exit_code == 1
    assert result.error_code == "FLX-3105"
    return result.stderr


def _named(message: str, key: str) -> None:
    assert key in message
    assert "kernel 0.2.0" in message and "kernel 0.1.0" in message


@pytest.mark.parametrize(
    ("keys", "key"),
    [
        (ONLY_WITHIN, "within"),
        (ONLY_WITHIN + MALFORMED_RATES, "rates"),
        (MALFORMED_RATES, "rates"),
        (ONLY_LIFETIMES, "lifetimes"),
    ],
)
def test_namespace_keys_name_the_kernel_first(keys: str, key: str, tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "bundle", keys)
    _named(_refusal(bundle, tmp_path, "0.1.0"), key)


@pytest.mark.parametrize("key", sorted(EMIT_LEVEL))
def test_emit_keys_name_the_kernel_first(key: str, tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "bundle")
    path = bundle / "simulation.flux.yml"
    text = path.read_text(encoding="utf-8")
    assert text.count("      customer:\n") == 1
    path.write_text(
        text.replace("      customer:\n", "      customer:\n" + EMIT_LEVEL[key], 1),
        encoding="utf-8",
    )
    _named(_refusal(bundle, tmp_path, "0.1.0"), key)


def test_kernel_0_2_0_still_refuses_a_within_with_no_lifetime_for_its_own_reason(
    tmp_path: Path,
) -> None:
    bundle = copy_bundle(tmp_path, "bundle", ONLY_WITHIN)
    message = _refusal(bundle, tmp_path, "0.2.0")
    assert "declares no lifetime" in message
    assert "kernel 0.1.0" not in message
