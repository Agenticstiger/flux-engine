# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A run directory's simulation id and expose ids are identifiers before they name a file.

``verify --update-golden`` took ``receipt["simulationId"]`` and wrote
``golden/<simulationId>.json``; ``verify`` and ``ship`` joined every key of
``receipt["emits"]`` into ``emits/<exposeId>.ndjson`` and the seam, fidelity
record and fidelity input file names. A bundle's schemas pin both ids to the
FLUX identifier grammar, so a run ``apply`` wrote never holds anything else,
but ``receipt.json`` sits in a directory a reader can edit, and
``"../../../victim"`` joined into the golden path overwrote a file outside
``golden/``. Every :class:`~flux_engine.stages.layout.RunLayout` builder that
joins one of those ids now refuses a value that is not an identifier
(``FLX-2112``) before it builds the path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout, require_identifier
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

_RUN_ID = "0123456789abcdef"

#: What no identifier can be: a parent reference, a separator in either
#: direction, an absolute path, nothing, a leading or trailing dot, a space.
_NOT_IDENTIFIERS = (
    "..",
    ".",
    "../../../victim",
    "a/b",
    "a\\b",
    "/etc/passwd",
    "",
    ".hidden",
    "trailing.",
    "a b",
    "line\nbreak",
)

_EXPOSE_BUILDERS = {
    "emit_path": lambda layout, value: layout.emit_path(value),
    "shard_emit_path": lambda layout, value: layout.shard_emit_path(value, 1, 2),
    "seam_attestation_path": lambda layout, value: layout.seam_attestation_path(value),
    "fidelity_record_path": lambda layout, value: layout.fidelity_record_path(value),
    "fidelity_input_path": lambda layout, value: layout.fidelity_input_path(value),
}
_ALL_BUILDERS = {
    **_EXPOSE_BUILDERS,
    "golden_path": lambda layout, value: layout.golden_path(value),
}


def _layout(tmp_path: Path) -> RunLayout:
    return RunLayout.for_state_dir(tmp_path / ".flux", _RUN_ID)


@pytest.mark.parametrize("value", _NOT_IDENTIFIERS)
@pytest.mark.parametrize("name", sorted(_ALL_BUILDERS))
def test_a_builder_refuses_an_id_that_is_not_an_identifier(
    name: str, value: str, tmp_path: Path
) -> None:
    builder: Callable[[RunLayout, str], Path] = _ALL_BUILDERS[name]
    with pytest.raises(FluxError) as raised:
        builder(_layout(tmp_path), value)
    assert raised.value.code.code == "FLX-2112"


@pytest.mark.parametrize("name", sorted(_ALL_BUILDERS))
def test_a_builder_refuses_an_id_that_is_not_a_string(name: str, tmp_path: Path) -> None:
    for value in (None, 7, ["a"]):
        with pytest.raises(FluxError) as raised:
            _ALL_BUILDERS[name](_layout(tmp_path), value)  # type: ignore[arg-type]
        assert raised.value.code.code == "FLX-2112"


@pytest.mark.parametrize("name", sorted(_ALL_BUILDERS))
@pytest.mark.parametrize("value", ("sub-1", "telco_fidelity", "v1.2", "a", "_x", "X9"))
def test_a_builder_keeps_the_path_under_its_directory_for_an_identifier(
    name: str, value: str, tmp_path: Path
) -> None:
    layout = _layout(tmp_path)
    path = _ALL_BUILDERS[name](layout, value)
    parent = layout.golden_dir if name == "golden_path" else layout.run_dir
    assert parent in path.parents
    assert path.parent in (parent, layout.emits_dir)
    assert value in path.name


def test_a_refusal_names_the_field_and_shows_a_short_safe_form_of_the_value(
    tmp_path: Path,
) -> None:
    with pytest.raises(FluxError) as raised:
        _layout(tmp_path).golden_path("../" * 50 + "victim\x1b[31m")
    message = str(raised.value)
    assert "simulationId" in message and "identifier" in message
    assert "victim" not in message and "\x1b" not in message
    assert len(message) < 400

    with pytest.raises(FluxError) as raised:
        _layout(tmp_path).emit_path("evil\x1b[2J/x")
    assert "exposeId" in str(raised.value)
    assert "\x1b" not in str(raised.value)


def test_require_identifier_returns_the_value_it_accepts() -> None:
    assert require_identifier("sub-1.v2", field="exposeId") == "sub-1.v2"


# --- end to end -------------------------------------------------------------


@pytest.fixture()
def applied(tmp_path: Path) -> RunLayout:
    result = apply(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    return RunLayout.for_state_dir(tmp_path / ".flux", str(result.artifacts["run_id"]))


def _tamper_receipt(layout: RunLayout, **changes: object) -> None:
    receipt = json.loads(layout.receipt_path.read_text(encoding="utf-8"))
    receipt.update(changes)
    layout.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def test_a_normal_runs_ids_pass_every_builder(applied: RunLayout) -> None:
    receipt = json.loads(applied.receipt_path.read_text(encoding="utf-8"))
    assert receipt["emits"], "the fixture has to emit at least one expose"
    assert applied.golden_path(receipt["simulationId"]).parent == applied.golden_dir
    for expose_id in receipt["emits"]:
        for builder in _EXPOSE_BUILDERS.values():
            assert builder(applied, expose_id).parent in (applied.run_dir, applied.emits_dir)


def test_update_golden_refuses_a_receipt_whose_simulation_id_traverses(
    applied: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The proof of concept: ``../../../victim`` overwrote a file outside ``golden/``.

    Run through the CLI, as the reader did: the exit code is not 0 and nothing is
    written. The comparison of the receipt's simulation with the plan's refuses first
    (``FLX-2122``); the run layout's own refusal of the id (``FLX-2112``) stays behind it.
    The builders' own refusal is pinned below.
    """
    victim = tmp_path / "victim.json"
    victim.write_text("untouched\n", encoding="utf-8")
    for target in ("../victim", "../../victim", "../../../victim", "/" + str(victim)[:-5]):
        _tamper_receipt(applied, simulationId=target)
        before = sorted(p.relative_to(tmp_path.parent) for p in tmp_path.parent.rglob("*"))

        code = main(
            [
                "verify",
                str(FIXTURE_BUNDLE),
                "--run-id",
                applied.run_id,
                "--state-dir",
                str(applied.state_dir),
                "--update-golden",
                "--reason",
                "a traversing simulation id must not name a file",
            ]
        )

        captured = capsys.readouterr()
        assert code != 0
        assert any(code in captured.err for code in ("FLX-2112", "FLX-2122", "FLX-3105"))
        assert "golden baseline updated" not in captured.out
        assert victim.read_text(encoding="utf-8") == "untouched\n"
        after = sorted(p.relative_to(tmp_path.parent) for p in tmp_path.parent.rglob("*"))
        assert after == before, "no file may appear anywhere the golden directory does not own"
    assert not applied.golden_dir.exists() or list(applied.golden_dir.iterdir()) == []


def test_update_golden_still_writes_for_the_runs_own_simulation_id(
    applied: RunLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    """The positive control: a guard that refused every id would also refuse this."""
    code = main(
        [
            "verify",
            str(FIXTURE_BUNDLE),
            "--run-id",
            applied.run_id,
            "--state-dir",
            str(applied.state_dir),
            "--update-golden",
            "--reason",
            "recording the first baseline for this fixture",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    simulation_id = json.loads(applied.receipt_path.read_text(encoding="utf-8"))["simulationId"]
    assert applied.golden_path(simulation_id).is_file()


def _traversing_emits(layout: RunLayout) -> None:
    receipt = json.loads(layout.receipt_path.read_text(encoding="utf-8"))
    first = next(iter(receipt["emits"]))
    _tamper_receipt(layout, emits={"../../escaped": receipt["emits"][first]})


def test_verify_refuses_a_receipt_whose_expose_id_traverses(applied: RunLayout) -> None:
    _traversing_emits(applied)
    try:
        result = verify(FIXTURE_BUNDLE, run_id=applied.run_id, state_dir=applied.state_dir)
    except FluxError as error:
        assert error.code.code in ("FLX-2112", "FLX-2122")
    else:
        assert not result.success
        assert result.error_code in ("FLX-2112", "FLX-2122")
    assert not (applied.run_dir.parent.parent / "escaped.ndjson").exists()


def test_ship_refuses_a_receipt_whose_expose_id_traverses(applied: RunLayout) -> None:
    _traversing_emits(applied)
    out_dir = applied.state_dir / "dist"
    try:
        result = ship(
            FIXTURE_BUNDLE, run_id=applied.run_id, out_dir=out_dir, state_dir=applied.state_dir
        )
    except FluxError as error:
        assert error.code.code in ("FLX-2112", "FLX-2122")
    else:
        assert not result.success
        assert result.error_code in ("FLX-2112", "FLX-2122")
    assert not (out_dir / "EVIDENCE.md").exists()
