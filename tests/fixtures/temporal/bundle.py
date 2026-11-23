# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy of ``tests/fixtures/temporal`` in a temporary directory, with namespace keys added.

The fixture's own files stay as they are. A test that needs the namespace's ``lifetimes`` or
``within`` key passes the YAML of the keys, and the helper writes it first under the namespace
of the copy's Simulation.
"""

from __future__ import annotations

import shutil
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
SIMULATION_ID = "sim-billing-dates"
_NAMESPACE = "  com.agentics.flux/1:\n"
_FILES = ("billing.fluid.yml", "persona.flux.yml", "simulation.flux.yml", "world.flux.yml")

#: A lifetime for each of customer, account and invoice, and a date within a parent's lifetime for
#: the account, the invoice and the payment: the chain customer, account, invoice, payment.
CHAIN = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end }
  - { emit: account, start: a_open, end: a_close }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, date: a_open }
  - { emit: invoice, relationship: account, date: i_issued }
  - { emit: payment, relationship: invoice, date: p_paid }
"""


#: The end columns' declarations in :func:`copy_uncut`: the same NULL shares over a range no end leaves.
UNCUT_CUSTOMER_END = (
    "cu_end: { nullFraction: '0.6', range: { min: '2000-01-01', max: '2100-12-31' } }"
)
UNCUT_ACCOUNT_CLOSE = (
    "a_close: { nullFraction: '0.7', range: { min: '2000-01-01', max: '2100-12-31' } }"
)


def copy_bundle(root: Path, name: str, keys: str = "") -> Path:
    """Copy the fixture to ``root / name``, with the YAML *keys* first under the namespace."""
    target = root / name
    target.mkdir(parents=True)
    for file in _FILES:
        shutil.copy2(FIXTURE_DIR / file, target / file)
    if keys:
        path = target / "simulation.flux.yml"
        text = path.read_text(encoding="utf-8")
        assert text.count(_NAMESPACE) == 1
        block = "".join(f"    {line}\n" for line in keys.splitlines())
        path.write_text(text.replace(_NAMESPACE, _NAMESPACE + block, 1), encoding="utf-8")
    return target


#: What the dates steps cut a draw to (ADR 0024 sections 4 and 5): the accounts' opening range, and the
#: end ranges of the customers and the accounts, with the declarations that cut nothing in their place.
_UNCUT = (
    ("a_open: { range: { min: '2023-01-01', max: '2025-09-30' } }", "a_open: {}"),
    (
        "cu_end: { nullFraction: '0.6', range: { min: '2024-01-01', max: '2025-12-31' } }",
        UNCUT_CUSTOMER_END,
    ),
    (
        "a_close: { nullFraction: '0.7', range: { min: '2024-01-01', max: '2025-12-31' } }",
        UNCUT_ACCOUNT_CLOSE,
    ),
)


def copy_uncut(root: Path, name: str, keys: str = "") -> Path:
    """:func:`copy_bundle` with the date ranges that would cut a draw taken out of the way.

    The accounts' opening date declares no range, and the customers' and accounts' ends declare one
    wide enough to cut no end, so the dates steps draw each value as the lifetimes and the window give
    it, which is what the oracles of the suites that recompute a draw from the parents' rows write out,
    and an enforced entry plans. The ranges themselves are exercised by
    ``tests/stages/test_dates_ranges_run.py`` and ``tests/stages/test_ship_temporal.py``, which keep them.
    """
    target = copy_bundle(root, name, keys)
    path = target / "simulation.flux.yml"
    text = path.read_text(encoding="utf-8")
    for old, new in _UNCUT:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return target
