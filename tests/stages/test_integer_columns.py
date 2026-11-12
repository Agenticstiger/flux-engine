# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §9's INT rule end to end: drawn counts in FLUID ``INTEGER`` columns.

The fixture is the starter bundle ``fluxsim init`` writes: in this
repository, scenario-driven, telco. Each case copies it, adds ``INTEGER``
columns to its FLUID contract, re-pins the contract digest the way an author
does after editing a contract, and runs the pipeline. The columns are named
after runtime attributes, so the by-name step maps them with no alias, and
between them they reach every scope the plan reads whole numbers from:

* ``attempts`` -> ``event.data.attempts``, a rate count shifted by one;
* ``tenureMonths`` -> ``subject.tenureMonths``, a bounded normal at scale 0;
* ``latePaymentCount`` -> ``subject.latePaymentCount``, a rate count whose
  rate each lifecycle overrides;
* ``committed`` -> ``subject.traits.committed``, a constant trait of 1 that
  the case adds to the starter Persona;
* ``arpu`` -> ``subject.arpu``, a bounded normal at scale 2, and
  ``digitalFluency`` -> ``subject.traits.digitalFluency``, a constant 0.6:
  both can carry a fraction, so both are refused.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Dict

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.seam.conform import render_rows
from flux_engine.seam.emit import row_line
from flux_engine.seam.resolve import load_documents_exact
from flux_engine.stages.pipeline import execute
from flux_engine.stages.plan import plan
from flux_engine.starter import scaffold

pytestmark = pytest.mark.integration

PRODUCT = "telco.gold.payment_recovery_moment"
EXPOSE_ID = "payment_recovery_moment"
COUNTS = ("attempts", "tenureMonths", "latePaymentCount", "committed")


def _column(name: str, column_type: str) -> str:
    return f"        - name: {name}\n          type: {column_type}\n          required: true\n"


def _bundle(target: Path, columns: Dict[str, str]) -> Path:
    """The starter bundle with *columns* appended to its contract, digest re-pinned.

    The starter Persona gains one constant trait, ``committed: 1``, so a trait
    source is in reach too. The Persona carries no digest pin.
    """
    scaffold(target)
    persona = target / "persona.flux.yml"
    text = persona.read_text("utf-8")
    anchor = "  traits:\n"
    assert text.count(anchor) == 1, "the starter persona changed shape"
    persona.write_text(text.replace(anchor, anchor + "    committed: 1\n"), "utf-8")

    contract = target / "payment-recovery.fluid.yml"
    text = contract.read_text("utf-8")
    anchor = "    binding:\n"
    assert text.count(anchor) == 1, "the starter contract changed shape"
    added = "".join(_column(name, column_type) for name, column_type in columns.items())
    contract.write_text(text.replace(anchor, added + anchor), "utf-8")

    (document,) = [doc for doc in load_documents_exact(contract) if doc.get("id") == PRODUCT]
    digest = digest_of_document(document, subject=f"contractDigest:{PRODUCT}").value
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    pinned = re.findall(r'contractDigest: "(sha256:[0-9a-f]{64})"', text)
    assert len(pinned) == 1, "the starter simulation changed shape"
    simulation.write_text(text.replace(pinned[0], digest), "utf-8")
    return target


@pytest.fixture(scope="module")
def counted(tmp_path_factory: pytest.TempPathFactory):
    bundle = _bundle(
        tmp_path_factory.mktemp("integer-columns") / "bundle",
        {name: "INTEGER" for name in COUNTS},
    )
    result = execute(bundle)
    return result, result.emit(EXPOSE_ID)


def test_every_count_column_is_bound_integral_and_nothing_else_is(counted) -> None:
    _, outcome = counted
    assert outcome.source == "scenario"
    integral = {binding.column for binding in outcome.mapping.bindings if binding.integral}
    assert integral == set(COUNTS)


def test_the_counts_reach_the_stream_as_json_integers(counted) -> None:
    _, outcome = counted
    assert outcome.rows, "a stream with no rows would pass this test vacuously"
    names = list(outcome.expose.column_names)
    rendered = render_rows(outcome.rows, outcome.expose)
    for row in rendered:
        line = json.loads(row_line(row))
        for name in COUNTS:
            value = line[names.index(name)]
            assert type(value) is int, f"{name} is spelled {type(value).__name__}"


def test_each_count_is_exactly_the_value_the_run_drew(counted) -> None:
    """No rounding anywhere: the int in the row equals the subject's drawn Decimal."""
    result, outcome = counted
    store = result.population
    ordinal_of = {store.identifier(ordinal): ordinal for ordinal in store.ordinals}
    names = list(outcome.expose.column_names)
    for row in outcome.rows:
        subject = store.subject(ordinal_of[row[names.index("subscriber_id")]])
        for name in ("tenureMonths", "latePaymentCount"):
            assert Decimal(row[names.index(name)]) == subject.derived[name]
        assert Decimal(row[names.index("committed")]) == subject.traits["committed"] == 1
        assert row[names.index("attempts")] >= 1


@pytest.mark.parametrize(
    ("column", "source"),
    [("arpu", "subject.arpu"), ("digitalFluency", "subject.traits.digitalFluency")],
)
def test_a_fraction_in_an_integer_column_is_refused_before_any_row(
    tmp_path: Path, column: str, source: str
) -> None:
    bundle = _bundle(tmp_path / "bundle", {column: "INTEGER"})
    with pytest.raises(FluxError) as error:
        execute(bundle)
    assert error.value.code.code == "FLX-3303"
    assert f"{column!r}" in str(error.value)
    assert source in str(error.value)


def test_plan_stops_on_it_and_names_the_code(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle", {"arpu": "INTEGER"})
    result = plan(bundle, state_dir=tmp_path / "state")
    assert result.exit_code != 0
    assert result.error_code == "FLX-3303"
    assert not list((tmp_path / "state").glob("runs/*/plan.json"))
