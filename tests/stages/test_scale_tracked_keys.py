# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A tracked foreign key never leaves a child naming keys its parent lacks, at any ``--scale``.

The bundle is ``tests/fixtures/history-fleet`` with a catalogue, ``body_type`` (26 drawn rows keyed
101..126), and a history ``vehicle_version`` that tracks ``body_type_id``, a foreign key into it, over
26 levels, one for each key (ADR 0017 section 6). A tracked column writes its level as the cell and
takes no parent row, so a level is a value of the parent's key only while the parent holds that key.
``--scale`` below 1 cuts the catalogue's rows and its key range and leaves the levels where they are.
What is pinned:

* **The refusal.** At ``--scale 0.25`` the plan is ``FLX-3403`` naming the expose, the column, a level
  and the parent's keys in that plan; so is a bundle at scale 1 whose level is no key of the catalogue.
* **The run.** At scale 1 and at 2 the bundle plans and applies, every cell is a key of the catalogue,
  and ``verify`` scores the link at 100 %.
* **The pick.** A foreign key that is not tracked takes the nearest key the catalogue holds, so the same
  levels at ``--scale 0.25`` keep the link whole.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.verify import verify

from ._fleet import (
    CONTRACT,
    HISTORY,
    NAMESPACE,
    PRODUCT,
    bundle,
    expose,
    plan_module,
    repin,
    rows,
    run_bundle,
)

pytestmark = pytest.mark.integration

CATALOGUE = "body_type"
COLUMN = "body_type_id"
KEYS = [str(key) for key in range(101, 127)]
#: Twenty-four levels at 4 % and two at 2 %: the weights total one exactly.
WEIGHTS = {level: ("0.04" if place < len(KEYS) - 2 else "0.02") for place, level in enumerate(KEYS)}


def _catalogue_bundle(
    tmp_path: Path,
    *,
    levels: Optional[List[str]] = None,
    tracked: bool = True,
    name: str = "bundle",
) -> Path:
    """The fleet with a 26-row catalogue and a history column of *levels* pointing at it."""
    target = bundle(tmp_path, name=name)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    contract["exposes"].insert(
        1,
        {
            "exposeId": CATALOGUE,
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "glue_table",
                "location": {"database": "fleet", "table": CATALOGUE, "region": "eu-west-1"},
            },
            "contract": {
                "schema": [
                    {"name": COLUMN, "type": "INT", "required": True},
                    {"name": "label", "type": "STRING", "required": True},
                ]
            },
            "semantics": {"entities": [{"name": CATALOGUE, "type": "primary", "expr": COLUMN}]},
        },
    )
    history = expose(contract, HISTORY)
    history["contract"]["schema"].append({"name": COLUMN, "type": "INT", "required": True})
    history["semantics"]["relationships"].append(
        {
            "name": CATALOGUE,
            "to": CATALOGUE,
            "fromColumns": [COLUMN],
            "toColumns": [COLUMN],
            "cardinality": "many_to_one",
            "joinType": "inner",
        }
    )
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["spec"]["emits"].insert(
        1,
        {
            "productRef": PRODUCT,
            "exposeId": CATALOGUE,
            "fluidVersion": "0.7.7",
            "provenance": {"contractDigest": "sha256:" + "0" * 64},
        },
    )
    extension = document["extensions"][NAMESPACE]
    extension["emits"][CATALOGUE] = {
        "rows": len(KEYS),
        "fidelity": {
            "encoding": {"fractionalBits": 16, "rounding": "floor"},
            "records": {"min": 1, "max": 1000000},
            "columns": {
                COLUMN: {"range": {"min": KEYS[0], "max": KEYS[-1]}},
                "label": {"levels": {"a": "0.5", "b": "0.5"}},
            },
            "properties": [
                {
                    "property": "mean",
                    "column": COLUMN,
                    "tolerance": {"type": "absolute", "value": "1000"},
                }
            ],
            "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
            "failureAction": "withholdBundle",
        },
    }
    held = levels if levels is not None else KEYS
    weights = {level: WEIGHTS.get(level, "0.02") for level in held}
    extension["emits"][HISTORY]["fidelity"]["columns"][COLUMN] = {"levels": weights}
    if tracked:
        # Each level moves to the next: a ring over the levels, so a run changes it on every version.
        extension["history"][0]["tracked"] = [
            {
                "column": COLUMN,
                "transitions": {
                    level: {held[(place + 1) % len(held)]: "1"} for place, level in enumerate(held)
                },
            }
        ]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    repin(target, contract)
    return target


def _refused(target: Path, state: Path, **options: Any) -> str:
    """The text a refused plan of *target* ends in, which is ``FLX-3403``."""
    planned = plan_module.plan(
        target, state_dir=state, kernel=KERNEL_V2.version, stage=False, **options
    )
    assert not planned.success
    assert "FLX-3403" in planned.stderr, planned.stderr
    return str(planned.stderr)


def _link(target: Path, state: Path, **options: Any) -> str:
    """Plan, apply and verify *target*; the verified link's line, after checking every cell is a key."""
    layout = run_bundle(target, state, **options)
    keys = {row[COLUMN] for row in rows(layout, CATALOGUE)}
    cells = {row[COLUMN] for row in rows(layout, HISTORY)}
    assert cells <= keys, sorted(cells - keys)
    verified = verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stdout + verified.stderr
    found = re.search(
        rf"integrity {HISTORY}\.{CATALOGUE} -> {CATALOGUE}: found .*", verified.stdout
    )
    assert found is not None, verified.stdout
    return found.group(0)


def test_a_scale_that_cuts_the_keys_below_a_tracked_level_is_refused(tmp_path: Path) -> None:
    text = _refused(_catalogue_bundle(tmp_path), tmp_path / "state", scale="0.25")
    # The expose, the column, a level no key of the cut catalogue holds, and the keys it does hold.
    assert f"expose '{HISTORY}' column '{COLUMN}'" in text
    assert f"'{CATALOGUE}'" in text
    assert "level '108'" in text
    assert "keys 101 to 107" in text and "7 rows" in text
    assert "plan without --scale" in text
    assert "stop tracking the column" in text


def test_a_level_that_is_no_key_of_its_parent_is_refused_at_scale_one(tmp_path: Path) -> None:
    levels = [*KEYS[:-1], "127"]
    text = _refused(_catalogue_bundle(tmp_path, levels=levels), tmp_path / "state")
    assert "level '127'" in text and "keys 101 to 126" in text and "26 rows" in text


#: The versions a run writes at each scale: 200 declared, scaled and rounded half up.
VERSIONS = {None: 200, "1": 200, "1.5": 300, "2": 400}


@pytest.mark.parametrize("scale", sorted(VERSIONS, key=str))
def test_the_levels_at_scale_one_and_above_are_keys_and_the_link_is_whole(
    tmp_path: Path, scale: Optional[str]
) -> None:
    line = _link(_catalogue_bundle(tmp_path), tmp_path / "state", scale=scale)
    assert f"found {VERSIONS[scale]} of {VERSIONS[scale]} (100.00%)" in line


def test_an_untracked_foreign_key_takes_the_nearest_key_of_the_cut_catalogue(
    tmp_path: Path,
) -> None:
    line = _link(_catalogue_bundle(tmp_path, tracked=False), tmp_path / "state", scale="0.25")
    assert "found 50 of 50 (100.00%)" in line
