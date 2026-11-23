# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``EVIDENCE.md`` names each conditioned emit and never prints a value it fixes (ADR 0028 section 10).

The runs go through ``plan``, ``apply`` and ``ship`` on ``tests/fixtures/conditions``, whose
``conditions`` come from the tests. A run whose plan holds none writes the file it wrote before.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.evidence.report import render_conditions_note, render_evidence_report
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship
from tests.seam.test_conditions_block import TWO_GROUPS, bundle_with

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

ship_module = importlib.import_module("flux_engine.stages.ship")

#: The SHA-256 of ``EVIDENCE.md`` for ``telco-fidelity-small``, shipped at a fixed instant with a
#: fixed signing key, computed with the code this change was made on, before it.
NO_CONDITIONS_SHA256 = "04fee632b640bbd135f16ad3b685b28a64c81c1c8ce3c99277da6687a1a39bc5"
_KEY = bytes(range(64, 96))


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(ship_module, "datetime", _Frozen)


def _slice_of(layout: RunLayout, expose: str, target: Path) -> str:
    columns = json.loads(layout.seam_attestation_path(expose).read_text("utf-8"))["columns"]
    rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in layout.emit_path(expose).read_text("utf-8").splitlines()
        if line
    ]
    target.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return target.as_uri()


def _shipped_evidence(
    bundle: Path, expose: str, tmp_path: Path, *, scale: str = "1"
) -> Tuple[bytes, Any]:
    state = tmp_path / ".flux"
    applied = apply(bundle, state_dir=state)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state, run_id)
    result = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "dist",
        state_dir=state,
        committed_slice={expose: _slice_of(layout, expose, tmp_path / "slice.ndjson")},
        signing_key=_KEY,
    )
    assert result.success, result.stderr
    return (layout.run_dir / "EVIDENCE.md").read_bytes(), result


def test_a_run_with_no_conditions_writes_the_evidence_it_wrote_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch)
    data, _ = _shipped_evidence(FIXTURE_BUNDLE, "payment_recovery_moment", tmp_path)
    assert hashlib.sha256(data).hexdigest() == NO_CONDITIONS_SHA256
    assert b"Conditioned emit" not in data


def test_the_report_adds_nothing_for_no_conditioned_emit() -> None:
    assert render_conditions_note(()) is None


def test_the_note_names_the_emit_each_group_its_rows_and_its_columns() -> None:
    note = render_conditions_note(
        [("subscriptions", [(400, ("plan", "region")), (600, ("plan",))])]
    )
    assert note is not None
    assert "Conditioned emit `subscriptions`" in note
    assert "group 1 holds 400 rows and fixes `plan`, `region`" in note
    assert "group 2 holds 600 rows and fixes `plan`" in note
    assert "declarations of the bundle" in note
    assert "no statistic stands behind them and no privacy grade covers them" in note


def test_a_conditioned_report_puts_the_entries_under_what_was_not_proven() -> None:
    from flux_engine.api.verdict import Reproducibility
    from flux_engine.core.digest import digest_of_document

    text = render_evidence_report(
        bundle_name="b",
        kernel_version="0.2.0",
        run_digest=digest_of_document({"run": 1}, subject="run"),
        generated_at="2026-09-01T12:00:00+00:00",
        conformance_summary="c",
        reproducibility=Reproducibility.NOT_CHECKED,
        fidelity_summary="f",
        conditioned=[("subscriptions", [(10, ("plan",))])],
    )
    assert text.index("Conditioned emit") > text.index("## What Was Not Proven")


def test_ship_lists_each_conditioned_emit_and_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch)
    bundle = bundle_with(tmp_path, TWO_GROUPS)
    data, _ = _shipped_evidence(bundle, "subscriptions", tmp_path)
    text = data.decode("utf-8")
    not_proven = text[text.index("## What Was Not Proven") :]
    assert "Conditioned emit `subscriptions`" in not_proven
    assert "group 1 holds 400 rows and fixes `plan`, `region`" in not_proven
    assert "group 2 holds 600 rows and fixes `plan`." in not_proven
    assert "declarations of the bundle" in not_proven
    for value in ("premium", "basic", "EU"):
        assert value not in text


def test_a_scaled_run_names_the_scaled_counts(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, TWO_GROUPS)
    state = tmp_path / ".flux"
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["plan", str(bundle), "--state-dir", str(state), "--scale", "0.5", "--json"])
    assert code == 0, err.getvalue() + out.getvalue()
    plan_document = json.loads(out.getvalue())
    run_id = plan_document["artifacts"]["run_id"]
    layout = RunLayout.for_state_dir(state, run_id)
    applied = apply(bundle, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr
    result = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "dist",
        state_dir=state,
        committed_slice={
            "subscriptions": _slice_of(layout, "subscriptions", tmp_path / "slice.ndjson")
        },
    )
    assert result.success, result.stderr
    text = (layout.run_dir / "EVIDENCE.md").read_text("utf-8")
    assert "group 1 holds 200 rows and fixes `plan`, `region`" in text
    assert "group 2 holds 300 rows and fixes `plan`." in text


def test_plan_apply_ship_on_the_conditions_fixture_through_the_cli(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, TWO_GROUPS)
    state = tmp_path / ".flux"

    def cli(*arguments: str) -> Any:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*arguments, "--json"])
        assert code == 0, err.getvalue() + out.getvalue()
        return json.loads(out.getvalue())

    run_id = cli("plan", str(bundle), "--state-dir", str(state))["artifacts"]["run_id"]
    cli("apply", str(bundle), "--run-id", run_id, "--state-dir", str(state))
    layout = RunLayout.for_state_dir(state, run_id)
    uri = _slice_of(layout, "subscriptions", tmp_path / "slice.ndjson")
    shipped = cli(
        "ship",
        str(bundle),
        "--run-id",
        run_id,
        "-o",
        str(tmp_path / "dist"),
        "--state-dir",
        str(state),
        "--committed-slice",
        f"subscriptions={uri}",
    )
    assert shipped["ok"]
    text = (layout.run_dir / "EVIDENCE.md").read_text("utf-8")
    assert "Conditioned emit `subscriptions`" in text
    assert "group 1 holds 400 rows and fixes `plan`, `region`" in text
    assert "premium" not in text
