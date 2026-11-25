# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle written by ``fluxsim init`` for the orders fixture, with a profile overlay declared.

The overlay tests of the stages and of ``init`` share this one place: the orders contract and
profile of ``tests/fixtures/author``, a bundle written from them, and the text of a Simulation
that declares an overlay under its namespace.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

from flux_engine.cli._init import run_init

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHOR = REPO_ROOT / "tests" / "fixtures" / "author"
ORDERS_CONTRACT = AUTHOR / "orders.fluid.yaml"
ORDERS_PROFILE = AUTHOR / "orders.profile.json"
#: The same profile with the ``mean/1`` statistic of ``amount``: init declares a ``mean`` check on it.
ORDERS_MEAN_PROFILE = AUTHOR / "orders-mean.profile.json"

#: The overlay the tests use: the orders table has twice the rows, and its amounts are different.
DOUBLE_ORDERS: Dict[str, Any] = {
    "name": "double-orders",
    "patch": {"tables": {"orders": {"rowCount": 1200}}},
}


def init_orders(directory: Path, profile: Path = ORDERS_PROFILE) -> Path:
    """The bundle ``fluxsim init`` writes from the orders contract and a profile, with no overlay."""
    result = run_init(str(directory), contract=str(ORDERS_CONTRACT), profile=str(profile))
    assert result.exit_code == 0, result.stderr
    return directory


def without_replacements(bundle: Path) -> Path:
    """Take the ``replacedBy`` of each assumption off the register of *bundle*'s Simulation.

    ``init`` writes it (ADR 0030 section 8), and a register that carries it is format 1-3-0, so a
    run recorded before it was written is the run of the bundle without it: its register is the one
    format 1-2-0 wrote, with the bytes it had.
    """
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    simulation.write_text(
        re.sub(r"(?m)^ +replacedBy:\n(?: {8,}\S.*\n)+", "", text), encoding="utf-8"
    )
    return bundle


def with_the_profile_named(bundle: Path, profile_name: str) -> Path:
    """Write *profile_name* back into the header of each document of *bundle*.

    ``init`` no longer names the profile's file in the header it writes, since the header is part
    of each document's bytes, so a run recorded before is the run of the bundle whose header names
    the profile.
    """
    for document in sorted(bundle.glob("*.flux.yml")):
        text, found = re.subn(
            r"\A# Written by `fluxsim init` from the contract (\S+) and its profile\.\n",
            lambda match: f"# Written by `fluxsim init` from {match.group(1)} and {profile_name}.\n",
            document.read_text(encoding="utf-8"),
        )
        assert found == 1, document
        document.write_text(text, encoding="utf-8")
    return bundle


def declare_overlay(bundle: Path, overlay_yaml: str) -> Path:
    """Put *overlay_yaml*, an ``overlay:`` block, under the Simulation's namespace, after its pin."""
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    marker = "\n    emits:\n"
    assert text.count(marker) == 1
    block = "".join(f"    {line}\n" for line in overlay_yaml.strip().splitlines())
    simulation.write_text(text.replace(marker, "\n" + block + "    emits:\n", 1), encoding="utf-8")
    return bundle


DOUBLE_YAML = """overlay:
  name: double-orders
  patch:
    tables:
      orders: {rowCount: 1200}
"""


def mean_overlay_yaml(name: str, mean_sum: str, rows: int = 1200) -> str:
    """An overlay that restates the ``mean/1`` of ``amount`` and the row count of ``orders``.

    *mean_sum* is the statistic's ``Sx`` for the 600 rows of the published group, on the 16-bit
    grid: ``Sx = mean * 600 * 65536``.
    """
    return f"""overlay:
  name: {name}
  patch:
    tables:
      orders:
        rowCount: {rows}
        columns:
          amount:
            statistics:
            - definition: mean/1
              disclosure: acro-default
              sampleSize: {{min: 598, max: 602}}
              groups:
              - {{Sx: '{mean_sum}', group: '', k: 600, status: F}}
"""


def cite_rows_from(bundle: Path, name: str) -> Path:
    """Change the register entry of ``orders``' row count to cite the overlay *name*."""
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    old = "      source: profile\n      statistic:\n        pointer: /tables/orders/rowCount\n"
    assert text.count(old) == 1
    new = (
        f"      source: overlay\n      overlay: {name}\n      statistic:\n"
        "        pointer: /tables/orders/rowCount\n"
    )
    simulation.write_text(text.replace(old, new, 1), encoding="utf-8")
    return bundle
