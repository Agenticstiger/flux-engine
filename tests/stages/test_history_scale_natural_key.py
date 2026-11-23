# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``--scale`` scales a history's natural entity column with its entities, through the ``fluxsim`` command line.

Two bundles whose history has a natural entity column that no relationship targets: ``device_version`` of
``tests/fixtures/history-window`` (``device_id``, 30 values for 30 entities) and ``subscription_version`` of
``tests/fixtures/history-natural-key`` (``subscription_id``, 60 values for 60 entities). The column's declared
range holds exactly its entities, so ``--scale`` above 1 grows the entities and has to grow the range with
them. What is pinned:

* **Plan, run and verify** at scale 2, at a scale above 1 that is not a whole number, and below 1.
* **The key.** One value per entity, all inside the scaled range.
* **Size.** The shares of ``--shard I/2`` joined are the whole run's bytes at a scale above 1.
* **The refusal.** A range too narrow at scale 1 still refuses, and the refusal does not blame ``--scale``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from tests.bundles import repin_contract

from .conftest import REPO_ROOT
from .test_asof_run import flux
from .test_history_window_e2e import Run, _apply, _plan, _rows, _verify

pytestmark = pytest.mark.integration

FIXTURES = REPO_ROOT / "tests" / "fixtures"
NAMESPACE = "com.agentics.flux/1"


class Shape(NamedTuple):
    fixture: str
    emit: str
    column: str
    entities: int


SHAPES = {
    "device": Shape("history-window", "device_version", "device_id", 30),
    "subscription": Shape("history-natural-key", "subscription_version", "subscription_id", 60),
}


def _copied(tmp_path: Path, fixture: str, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / fixture, target)
    return target


def _bundle(tmp_path: Path, shape: Shape, name: str = "bundle") -> Path:
    return _copied(tmp_path, shape.fixture, name)


def _entity_count(shape: Shape, scale: str) -> int:
    """The entities a scale gives, as the engine rounds them (half up)."""
    return int(shape.entities * float(scale) + 0.5)


@pytest.mark.parametrize("name", sorted(SHAPES))
@pytest.mark.parametrize("scale", ["2", "1.5", "0.5"])
def test_the_natural_key_scales_with_the_entities(tmp_path: Path, name: str, scale: str) -> None:
    shape = SHAPES[name]
    bundle = _bundle(tmp_path, shape)
    state = tmp_path / "state"
    layout = _plan(bundle, state, "--scale", scale)
    _apply(bundle, layout, state)
    _verify(Run(bundle, state, layout))
    count = _entity_count(shape, scale)
    keys = [row[shape.column] for row in _rows(layout, shape.emit)]
    # One distinct value per entity, every one inside the scaled range 1..count.
    assert len(set(keys)) == count
    assert min(keys) >= 1 and max(keys) <= count


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_shares_of_a_scaled_run_joined_are_the_whole_runs_bytes(
    tmp_path: Path, name: str
) -> None:
    shape = SHAPES[name]
    bundle = _bundle(tmp_path, shape)
    state = tmp_path / "state"
    layout = _plan(bundle, state, "--scale", "2")
    _apply(bundle, layout, state)
    whole = layout.emit_path(shape.emit).read_bytes()
    for index in (1, 2):
        _apply(bundle, layout, state, "--shard", f"{index}/2")
    shares = [layout.shard_emit_path(shape.emit, index, 2) for index in (1, 2)]
    assert b"".join(share.read_bytes() for share in shares) == whole


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_a_range_too_narrow_at_scale_one_is_refused_without_blaming_the_scale(
    tmp_path: Path, name: str
) -> None:
    shape = SHAPES[name]
    bundle = _bundle(tmp_path, shape)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    declared = document["extensions"][NAMESPACE]["emits"][shape.emit]["fidelity"]["columns"]
    declared[shape.column]["range"]["max"] = str(shape.entities - 1)
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    for scale in ("1", "2"):
        planned = flux(
            *("plan", str(bundle), "--state-dir", str(tmp_path / f"s{scale}"), "--kernel", "0.2.0"),
            *("--scale", scale),
            cwd=tmp_path,
        )
        assert planned.returncode == 1
        assert "FLX-3403" in planned.stderr
        assert "--scale" not in planned.stderr


def _parented(tmp_path: Path) -> Path:
    """``history-chain`` with no relationship into the history, and its natural key's range as narrow as its entities."""
    bundle = _copied(tmp_path, "history-chain", "parented")
    contract = bundle / "subscriptions.fluid.yml"
    text = contract.read_text("utf-8")
    pattern = re.compile(
        r"      relationships:\n        - name: subscription\n          to: subscription_version\n(?:          .*\n)+"
    )
    assert len(pattern.findall(text)) == 3
    contract.write_text(pattern.sub("", text), "utf-8")
    path = bundle / "simulation.flux.yml"
    repin_contract(contract, path, "billing.subscriptions")
    document = yaml.safe_load(path.read_text("utf-8"))
    emits = document["extensions"][NAMESPACE]["emits"]
    emits["subscription_version"]["fidelity"]["columns"]["subscription_id"]["range"]["max"] = "60"
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


@pytest.mark.parametrize("scale", ["1", "2", "1.5"])
def test_the_natural_key_of_a_history_with_a_parent_scales_with_the_entities(
    tmp_path: Path, scale: str
) -> None:
    bundle = _parented(tmp_path)
    state = tmp_path / "state"
    layout = _plan(bundle, state, "--scale", scale)
    _apply(bundle, layout, state)
    count = int(60 * float(scale) + 0.5)
    keys = [row["subscription_id"] for row in _rows(layout, "subscription_version")]
    assert len(set(keys)) == count and max(keys) <= count
