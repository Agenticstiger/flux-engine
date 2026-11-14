# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``plan.json`` records every compiled fidelity block, epsilons included.

SEAM-FIDELITY-SPEC §3: "Both ``H`` and ``G`` and every ``ε`` are written to
``plan.json``." ``plan`` compiled each declared block and then dropped the
result, so a plan recorded ``H`` and ``G`` and no epsilon, no margin and no
magnitude bound. A reader checking a fidelity record months later had no
planned number to hold the record's numbers against.

What is written is the compiled block's
:meth:`~flux_engine.concordance.compiler.CompiledFidelity.plan_document`,
the fragment the published vectors pin as ``expect.plan``. It sits in the
emit's own entry under ``fidelity``, the key the block is declared under in
``extensions["com.agentics.flux/1"].emits.<exposeId>``. An emit that
declares no block gets no key, so its plan keeps the bytes it had:
``tests/golden/towers-public/replay.json`` pins such a plan's ``planDigest``
through the run id and the unmasked ``runDigest``, both derived from it.

``apply`` and ``ship`` on a plan that records the block, and on one written
before it did, are in ``test_ship_fidelity_bundle.py``, beside the run they
reuse.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages.pipeline import compile_declared_fidelity
from flux_engine.stages.plan import plan
from tests.bundles import bundle_with_properties

from .conftest import FIXTURE_BUNDLE, read_json

pytestmark = pytest.mark.integration

SIMULATION_ID = "sim-payment-recovery"
EXPOSE_ID = "payment_recovery_moment"

#: The keys of an emit entry that declares no block: the whole entry, as
#: 0.4.0 wrote it. A block adds ``fidelity`` and nothing else.
ENTRY_KEYS = {"G", "H", "declaresFidelity", "generator", "productRef"}

#: The fixture's own first property, and the same mean taken per market.
MEAN = '{ property: mean, column: amount, tolerance: { type: absolute, value: "50" } }'
GROUPED_MEAN = MEAN.replace("column: amount,", "column: amount, groupBy: market,")


def _planned(bundle: Path, state_dir: Path) -> Dict[str, Any]:
    """Plan *bundle* and return the ``plan.json`` it wrote, read back from disk."""
    result = plan(bundle, state_dir=state_dir)
    assert result.success, result.stderr
    return read_json(Path(result.artifacts["plan_path"]))


def _declared_fragment(bundle: Path) -> Dict[str, Any]:
    """The fragment compiled from *bundle*'s declaration, as JSON reads it back."""
    (resolved,) = resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION_ID)
    compiled = compile_declared_fidelity(resolved)
    assert compiled is not None
    return json.loads(json.dumps(compiled.plan_document()))


def test_a_fidelity_bundles_plan_records_every_epsilon(bundle_dir: Path, state_dir: Path) -> None:
    """The defect: the emit's entry held ``H`` and ``G`` and no epsilon."""
    entry = _planned(bundle_dir, state_dir)["emits"][EXPOSE_ID]

    assert set(entry) == ENTRY_KEYS | {"fidelity"}
    recorded = entry["fidelity"]
    assert recorded == _declared_fragment(bundle_dir)
    assert (recorded["exposeId"], recorded["H"], recorded["G"]) == (
        EXPOSE_ID,
        entry["H"],
        entry["G"],
    )
    assert [(p["propertyId"], p["epsilon"]) for p in recorded["properties"]] == [
        # The mean's epsilon is one grid step, 2**-16 at 16 fractional bits;
        # a support count carries none.
        ("000:mean:amount", "0.0000152587890625"),
        ("001:support:market", "0"),
    ]


def test_an_emit_without_a_block_gets_no_key(tmp_path: Path, state_dir: Path) -> None:
    """Without a block the entry is the five keys it always was, so the bytes hold."""
    bundle = tmp_path / "no-block"
    bundle_with_properties(FIXTURE_BUNDLE, bundle, [MEAN])
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    start = text.index("        fidelity:\n")
    end = text.index("agentPolicy:\n")
    simulation.write_text(text[:start] + text[end:], encoding="utf-8")

    entry = _planned(bundle, state_dir)["emits"][EXPOSE_ID]

    assert entry["declaresFidelity"] is False
    assert set(entry) == ENTRY_KEYS


def test_a_grouped_property_records_its_group_column(tmp_path: Path, state_dir: Path) -> None:
    """A grouped property's entry names its ``groupBy``; an ungrouped one carries none."""
    bundle = bundle_with_properties(FIXTURE_BUNDLE, tmp_path / "grouped", [MEAN, GROUPED_MEAN])

    recorded = _planned(bundle, state_dir)["emits"][EXPOSE_ID]["fidelity"]

    assert recorded == _declared_fragment(bundle)
    ungrouped, grouped = recorded["properties"]
    assert "groupBy" not in ungrouped
    assert grouped["groupBy"] == "market"
    assert (grouped["property"], grouped["columns"]) == ("mean", ["amount"])
    # An absolute mean's epsilon is the grid's, whatever it is grouped by.
    assert (grouped["epsilon"], grouped["margin"]) == (ungrouped["epsilon"], ungrouped["margin"])


def test_plan_json_holds_no_float(bundle_dir: Path, state_dir: Path) -> None:
    """Every number the block adds is an exact string or an integer.

    ``planDigest`` is a JCS digest, and a float would make it depend on how
    a float is printed. The written file is the canonical rendering.
    """
    result = plan(bundle_dir, state_dir=state_dir)
    assert result.success, result.stderr
    written = Path(result.artifacts["plan_path"]).read_text(encoding="utf-8")

    def refuse(literal: str) -> float:
        raise AssertionError(f"plan.json holds a float: {literal}")

    document = json.loads(written, parse_float=refuse)
    assert document["emits"][EXPOSE_ID]["fidelity"]["properties"]
    assert json.dumps(document, indent=2, sort_keys=True) + "\n" == written
