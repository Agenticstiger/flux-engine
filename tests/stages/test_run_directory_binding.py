# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a run directory has to be before any stage will read or write it.

Three properties, none of which was checked:

* **The run id is 16 hex digits.** ``<state_dir>/runs/<run_id>`` is a
  path built from a caller-supplied string, and ``--run-id
  '../../../../tmp/x'`` walked straight out of the state directory and
  created a run tree there, exit 0. The first repair held it to the FLUX
  identifier grammar, which still admitted ``--run-id nothexatall``: a
  run id is *derived* (the first 16 hex characters of ``planDigest``,
  :func:`~flux_engine.stages.layout.run_id_for`), never chosen, so the
  gate is now exactly as narrow as the derivation.
* **The directory belongs to the plan inside it.** ``plan.json`` was only
  checked for self-consistency, so a run directory copied under another
  name kept every later comparison green -- ``plan.json``,
  ``fidelity-input-<id>.json`` and ``seam-attestation-<id>.json`` travel
  together, and all three live inside the copied tree. ``apply`` and
  ``ship`` went through ``load_recorded_plan``; ``verify`` read no
  ``plan.json`` at all, so it was the last stage that still accepted one.
* **The commit marker is written whole.** ``receipt.json`` is what
  ``ship`` and ``verify`` read as "this run completed", and it was written
  with a plain ``write_text``: interrupted part-way it left a present,
  truncated file that read as a finished run and surfaced as a
  ``JSONDecodeError`` two stages later.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.stages.apply import apply, load_recorded_plan
from flux_engine.stages.layout import (
    RunLayout,
    atomic_write_text,
    require_run_id,
    run_id_for,
)
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration


# --- the run id is 16 hex digits -------------------------------------------

_TRAVERSALS = (
    "../../../../tmp/escaped",
    "..",
    "../sibling",
    "runs/nested",
    "/absolute",
    "",
    ".hidden",
    "a b",
)

#: Ids the old identifier grammar accepted and no plan could ever derive:
#: a word, a dotted or hyphenated name, an uppercase spelling of a real
#: id, and hex strings of the wrong length.
_NOT_DERIVABLE = (
    "nothexatall",
    "zzzzzzzzzzzzzzzz",
    "unplanned",
    "run.1",
    "run-1",
    "a",
    "33E533EE2D9A6786",
    "33e533ee2d9a678",
    "33e533ee2d9a67866",
    "33e533ee2d9a6786 ",
)


@pytest.mark.parametrize("run_id", _TRAVERSALS)
def test_a_run_id_that_is_not_one_safe_component_is_refused(run_id: str, tmp_path: Path) -> None:
    with pytest.raises(FluxError) as raised:
        RunLayout.for_state_dir(tmp_path / ".flux", run_id)
    assert raised.value.code.code == "FLX-2112"


@pytest.mark.parametrize("run_id", _NOT_DERIVABLE)
def test_a_run_id_no_plan_could_have_derived_is_refused(run_id: str, tmp_path: Path) -> None:
    """The refutation, as a test: ``require_run_id('nothexatall')`` used to succeed."""
    with pytest.raises(FluxError) as raised:
        require_run_id(run_id)
    assert raised.value.code.code == "FLX-2112"
    with pytest.raises(FluxError):
        RunLayout.for_state_dir(tmp_path / ".flux", run_id)


@pytest.mark.parametrize("run_id", ("33e533ee2d9a6786", "0123456789abcdef", "0" * 16))
def test_the_ids_the_engine_itself_derives_are_accepted(run_id: str, tmp_path: Path) -> None:
    """The positive control: a guard that refused every id would refuse the engine's own."""
    layout = RunLayout.for_state_dir(tmp_path / ".flux", run_id)
    assert require_run_id(run_id) == run_id
    assert layout.ensure().run_dir.is_dir()


def test_the_derivation_and_the_gate_read_one_grammar() -> None:
    """Whatever ``run_id_for`` produces, ``require_run_id`` accepts -- and vice versa."""
    digest = "sha256:" + "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a39281706f5e4d3c2b1a0"
    derived = run_id_for(digest)
    assert derived == "9f8e7d6c5b4a3928"
    assert require_run_id(derived) == derived
    with pytest.raises(FluxError) as raised:
        run_id_for("")
    assert raised.value.code.code == "FLX-2112"


def test_a_traversing_run_id_creates_nothing_outside_the_state_directory(tmp_path: Path) -> None:
    """The defect, stated as the filesystem fact it was: a directory appeared."""
    state_dir = tmp_path / ".flux"
    outside = tmp_path / "outside"
    with pytest.raises(FluxError):
        RunLayout.for_state_dir(state_dir, f"../../{outside.name}").ensure()
    assert not outside.exists()


def test_the_containment_assertion_stands_behind_the_grammar(tmp_path: Path) -> None:
    """Defence in depth: even an id the grammar let through must land under ``runs/``.

    Constructed around :func:`require_run_id` on purpose -- the grammar is
    the gate, and this asserts the second one is really there rather than
    trusting the first.
    """
    layout = RunLayout(state_dir=tmp_path / ".flux", run_id="0123456789abcdef")
    escaped = RunLayout.__new__(RunLayout)
    object.__setattr__(escaped, "state_dir", layout.state_dir)
    object.__setattr__(escaped, "run_id", "../escaped")
    with pytest.raises(FluxError) as raised:
        escaped.ensure()
    assert raised.value.code.code == "FLX-2112"


# --- the directory belongs to the plan inside it ----------------------------


@pytest.fixture()
def applied(tmp_path: Path) -> RunLayout:
    result = apply(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    return RunLayout.for_state_dir(tmp_path / ".flux", str(result.artifacts["run_id"]))


def test_a_run_directory_copied_under_another_name_is_refused(applied: RunLayout) -> None:
    """The reproduction: ``cp -R runs/<real> runs/deadbeefdeadbeef`` then ship it.

    Every file in the copy agrees with every other file in the copy, which
    is exactly why the internal binding checks all passed and a signed
    evidence pack came out of a directory whose ``plan.json`` names a
    different run.
    """
    copied = applied.runs_dir / "deadbeefdeadbeef"
    shutil.copytree(applied.run_dir, copied)

    impostor = RunLayout.for_state_dir(applied.state_dir, "deadbeefdeadbeef")
    with pytest.raises(FluxError) as raised:
        load_recorded_plan(impostor)
    assert raised.value.code.code == "FLX-2113"

    result = ship(
        FIXTURE_BUNDLE,
        run_id="deadbeefdeadbeef",
        out_dir=applied.state_dir / "dist",
        state_dir=applied.state_dir,
    )
    assert not result.success
    assert result.error_code == "FLX-2113"
    assert not (applied.state_dir / "dist").exists()


def test_verify_refuses_a_copied_run_directory_too(applied: RunLayout) -> None:
    """``verify`` was the one stage that read no ``plan.json`` at all.

    ``apply`` and ``ship`` both go through :func:`load_recorded_plan`;
    ``verify`` never did, so ``cp -R runs/<real> runs/deadbeefdeadbeef``
    and ``fluxsim verify --run-id deadbeefdeadbeef`` exited 0 over a
    directory whose plan names a different run.
    """
    copied = applied.runs_dir / "deadbeefdeadbeef"
    shutil.copytree(applied.run_dir, copied)

    result = verify(
        FIXTURE_BUNDLE,
        run_id="deadbeefdeadbeef",
        state_dir=applied.state_dir,
        allow_declared_baseline_drift=True,
    )
    assert not result.success
    assert result.error_code == "FLX-2113"
    assert "deadbeefdeadbeef" in result.stderr


def test_the_run_directory_the_engine_wrote_is_accepted(applied: RunLayout) -> None:
    """The positive control: the real directory must still load its own plan."""
    document = load_recorded_plan(applied)
    assert document["planDigest"].partition(":")[2].startswith(applied.run_id)


def test_verify_accepts_the_run_directory_the_engine_wrote(applied: RunLayout) -> None:
    """The positive control for the leg above: the real directory still verifies."""
    result = verify(
        FIXTURE_BUNDLE,
        run_id=applied.run_id,
        state_dir=applied.state_dir,
        allow_declared_baseline_drift=True,
    )
    assert result.error_code != "FLX-2113", result.stderr


# --- the commit marker is written whole ------------------------------------


def test_the_receipt_is_replaced_atomically_never_truncated(tmp_path: Path) -> None:
    """A failed write leaves the previous marker intact, not a torn one."""
    path = tmp_path / "receipt.json"
    atomic_write_text(path, json.dumps({"runId": "first"}) + "\n")

    with pytest.raises(TypeError):
        atomic_write_text(path, object())  # type: ignore[arg-type]
    assert json.loads(path.read_text(encoding="utf-8")) == {"runId": "first"}
    assert not [child for child in tmp_path.iterdir() if child.name.startswith(".receipt.json-")]


def test_a_torn_receipt_is_a_catalogued_refusal_not_a_json_error(applied: RunLayout) -> None:
    """A present-but-truncated marker reads as "no applied run", by code.

    Before, ``ship`` parsed the marker with a bare ``json.loads`` and a
    half-written file surfaced as a ``JSONDecodeError`` out of a function
    whose whole contract is to return a :class:`StageResult`.
    """
    whole = applied.receipt_path.read_text(encoding="utf-8")
    applied.receipt_path.write_text(whole[: len(whole) // 2], encoding="utf-8")

    result = ship(
        FIXTURE_BUNDLE,
        run_id=applied.run_id,
        out_dir=applied.state_dir / "dist",
        state_dir=applied.state_dir,
    )
    assert not result.success
    assert result.error_code == "FLX-2109"
    assert "receipt" in result.stderr
