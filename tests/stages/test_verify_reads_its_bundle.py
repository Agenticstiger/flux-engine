# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify``'s arguments have to mean something.

Three defects, all of them a check that could not fail:

* the required positional ``bundle`` was never read. ``bundle_directory``
  occurred exactly once in ``stages/verify.py`` -- in the signature -- so
  ``fluxsim verify /etc --run-id <id>`` and ``fluxsim verify /nope --run-id
  <id>`` produced the same verdict as the real bundle, exit 0 included.
* row conformance compared row widths **to each other**
  (``len({len(row) for row in rows}) > 1``) rather than to the column
  count the emit's seam attestation declares, so a stream whose every row
  was uniformly the wrong width passed the leg whose own docstring
  promised "every row's width matches its declared column count".
* a line that was not JSON reached a bare ``json.loads`` and left the CLI
  reporting ``JSONDecodeError`` with ``error.code: null``, which the envelope
  defines as an unclassified internal failure -- and it suppressed the
  RFC-04, fidelity and golden legs entirely.

Plus one silent no-op: a mistyped ``--committed-slice`` key was consumed
with ``.get()``, so it was never looked up and never mentioned, and ship
then told the caller to pass the flag they had just passed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify as run_verify

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

EXPOSE_ID = "payment_recovery_moment"


@pytest.fixture()
def applied(tmp_path: Path) -> RunLayout:
    result = apply(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    return RunLayout.for_state_dir(tmp_path / ".flux", str(result.artifacts["run_id"]))


def _verify(layout: RunLayout, bundle: Path, **kwargs: object) -> object:
    return run_verify(
        bundle,
        run_id=layout.run_id,
        state_dir=layout.state_dir,
        allow_declared_baseline_drift=True,
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_real_bundle_verifies(applied: RunLayout) -> None:
    """The positive control for every refusal below."""
    result = _verify(applied, FIXTURE_BUNDLE)
    assert result.success, result.stderr  # type: ignore[attr-defined]


def test_a_bundle_that_is_not_a_bundle_is_refused(applied: RunLayout, tmp_path: Path) -> None:
    empty = tmp_path / "not-a-bundle"
    empty.mkdir()
    result = _verify(applied, empty)
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert result.error_code is not None  # type: ignore[attr-defined]


def test_a_path_that_does_not_exist_is_refused(applied: RunLayout, tmp_path: Path) -> None:
    result = _verify(applied, tmp_path / "nowhere")
    assert result.exit_code == 1  # type: ignore[attr-defined]


def test_a_different_bundle_is_refused_by_input_digest(applied: RunLayout, tmp_path: Path) -> None:
    """The case the argument exists for: a real bundle, but not this run's.

    The edit is a comment -- something no part of the simulation reads --
    because KERNEL-SPEC §11 digests the *raw bytes* of every document, so
    this is the smallest possible difference that still makes the run
    directory answerable to a different bundle.
    """
    other = tmp_path / "other-bundle"
    shutil.copytree(FIXTURE_BUNDLE, other)
    persona = other / "persona.flux.yml"
    persona.write_text(persona.read_text(encoding="utf-8") + "\n# a comment\n", encoding="utf-8")

    result = _verify(applied, other)
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert result.error_code == "FLX-2101"  # type: ignore[attr-defined]
    recorded = json.loads(applied.receipt_path.read_text(encoding="utf-8"))["inputDigest"]
    assert result.artifacts["inputDigest"]["recorded"] == recorded  # type: ignore[attr-defined]
    assert result.artifacts["inputDigest"]["recomputed"] != recorded  # type: ignore[attr-defined]


def test_rows_of_a_uniformly_wrong_width_are_a_row_conformance_problem(
    applied: RunLayout,
) -> None:
    """Every row three-wide against four declared columns: self-consistent, and wrong."""
    path = applied.emit_path(EXPOSE_ID)
    narrowed = [
        json.dumps(json.loads(line)[:3], separators=(",", ":"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    path.write_text("\n".join(narrowed) + "\n", encoding="utf-8")

    result = _verify(applied, FIXTURE_BUNDLE)
    problems = result.artifacts["rowConformance"]["problems"]  # type: ignore[attr-defined]
    assert result.artifacts["rowConformance"]["ok"] is False  # type: ignore[attr-defined]
    assert any("declares 4 columns" in problem for problem in problems), problems


def test_a_line_that_is_not_json_is_a_named_problem_not_a_traceback(
    applied: RunLayout,
) -> None:
    path = applied.emit_path(EXPOSE_ID)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = '["unterminated'
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _verify(applied, FIXTURE_BUNDLE)
    assert result.exit_code == 1  # type: ignore[attr-defined]
    # A catalogued code, never `null`, and the other legs still ran.
    assert result.error_code == "FLX-2110"  # type: ignore[attr-defined]
    assert result.artifacts["rowConformance"]["problems"] == [  # type: ignore[attr-defined]
        f"{EXPOSE_ID}: line 1 is not JSON"
    ]
    assert "rfc04" in result.artifacts  # type: ignore[attr-defined]
    assert "fidelity" in result.artifacts  # type: ignore[attr-defined]


def test_a_committed_slice_for_an_unknown_expose_is_refused_by_name(
    applied: RunLayout, tmp_path: Path
) -> None:
    slice_path = tmp_path / "slice.ndjson"
    slice_path.write_text("{}\n", encoding="utf-8")
    result = _verify(
        applied,
        FIXTURE_BUNDLE,
        committed_slice={"payment_recovery_momnt": f"file://{slice_path}"},
    )
    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert result.error_code == "FLX-2111"  # type: ignore[attr-defined]
    # It names both the typo and what the run actually emits, so the next
    # command is the same one spelled right.
    assert "payment_recovery_momnt" in result.stderr  # type: ignore[attr-defined]
    assert EXPOSE_ID in result.stderr  # type: ignore[attr-defined]
