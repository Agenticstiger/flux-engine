# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim ship`` reports every forecast of the run, pending and scored (ADR 0029 section 8).

The runs are the ``tests/fixtures/outcomes`` bundle, planned, applied and shipped against its
profile after ``forecast make`` (pending rows) and after ``forecast score`` (scored rows). Pinned:
the section and ``forecast-accuracy.json`` appear only when the store holds a forecast registered
for the run's ``inputDigest``; a run with none keeps ``EVIDENCE.md`` and its pack byte for byte
(compared with the same run shipped without the forecast read, at a fixed instant and with a fixed
signing key, never with a hash computed on some earlier commit); the rows of the file are the rows of the section; the count
of forecasts on a state spans bundles; the weakest grade is stated; and the whole loop runs from the
command line.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.accuracy.store import AccuracyStore, prediction_from_document
from flux_engine.cli import main
from flux_engine.evidence.report import require_no_banned_words
from flux_engine.outcomes.forecast import forecast_make
from flux_engine.outcomes.scoring import score_forecasts
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship
from tests.outcomes.support import BUNDLE, PROFILE, next_state

pytestmark = pytest.mark.integration

ship_module = importlib.import_module("flux_engine.stages.ship")

#: The members of the pack that do not depend on where the state directory is, so that two runs of
#: the same bundle shipped from two directories agree on them. The pack's other members and the
#: archive itself carry the paths and the time of the run, so they are not compared. Nothing here is
#: an absolute hash: ``EVIDENCE.md`` and the records carry the engine's own report text and register
#: format, which every change to either moves, and the optional ``[sql]`` dependencies, which change
#: what the report says about the consumer queries; what this file pins is that a forecast adds
#: nothing to a run that has none.
NO_FORECAST_MEMBERS = (
    "bundle/run/EVIDENCE.md",
    "bundle/run/run-digest.json",
    "bundle/run/receipt.json",
    "bundle/run/scorecard.json",
    "bundle/run/parameter-register.json",
    "bundle/run/column-provenance.json",
)
NO_FORECAST_RUN_FILES = [
    "EVIDENCE.md",
    "column-provenance.json",
    "decisions.meta.ndjson",
    "decisions.ndjson",
    "emits/subscriptions.ndjson",
    "events.ndjson",
    "fidelity-input-subscriptions.json",
    "lineage.ndjson",
    "parameter-register.json",
    "plan.json",
    "population.ndjson",
    "receipt.json",
    "run-digest.json",
    "scorecard.json",
    "seam-attestation-subscriptions.json",
    "seed-paths.ndjson",
    "suppressions.ndjson",
]

_KEY = bytes(range(64, 96))
REGISTERED = "2026-07-01T00:00:00Z"
HORIZON = "2026-09-30T23:59:59Z"
OBSERVED = "2026-10-01T00:00:00Z"
STATE_DIGEST = "sha256:817592e3a22333846d9c4913e07cea2911ced79d414038208e84374b7528a630"
PREMIUM = "premium-offered.817592e3a2233384"
BASIC = "basic-control.817592e3a2233384"


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(ship_module, "datetime", _Frozen)


def _shipped(state: Path, out: Path) -> Tuple[RunLayout, Any]:
    applied = apply(BUNDLE, state_dir=state, profile=PROFILE)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    result = ship(
        BUNDLE,
        run_id=layout.run_id,
        out_dir=out / "dist",
        state_dir=state,
        signing_key=_KEY,
        profile=PROFILE,
    )
    assert result.success, result.stderr
    return layout, result


def _members(archive: Path) -> Dict[str, str]:
    found: Dict[str, str] = {}
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            handle = bundle.extractfile(member) if member.isfile() else None
            if handle is not None:
                found[member.name] = hashlib.sha256(handle.read()).hexdigest()
    return found


def _reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, read: bool
) -> Tuple[bytes, Dict[str, str]]:
    """``EVIDENCE.md`` and the pack members of the fixture shipped with no forecast registry at all.

    Shipped from a state directory of its own, beside the run under test. With ``read`` false the
    run does not read the outcome-accuracy store, so the reference is the run as it was before
    forecasts were reported; with it true the run reads a store that does not exist.
    """
    root = tmp_path / "reference"
    with monkeypatch.context() as patch:
        if not read:
            patch.setattr(ship_module, "_forecast_accuracy", lambda *args, **kwargs: None)
        layout, result = _shipped(root / "state", root)
    assert not (layout.run_dir / "forecast-accuracy.json").exists()
    members = _members(Path(result.artifacts["archive"]))
    assert "bundle/run/forecast-accuracy.json" not in members
    return (layout.run_dir / "EVIDENCE.md").read_bytes(), members


def _rows(text: str) -> List[List[str]]:
    section = text.split("## Forecast Accuracy")[1].split("\n## ")[0]
    return [
        [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        for line in section.splitlines()
        if line.startswith("| `")
    ]


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> Path:
    state = tmp_path_factory.mktemp("ship-forecast") / "state"
    result = forecast_make(
        BUNDLE, registered_at=REGISTERED, horizon_end=HORIZON, state_dir=state, profile=PROFILE
    )
    assert result.success, result.stderr
    return state


@pytest.fixture()
def state(made: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "state"
    shutil.copytree(made, copy)
    return copy


def _score(state: Path, tmp_path: Path, **cells: Any) -> Path:
    following = next_state(
        tmp_path,
        cells={
            ("renewed-offered", "premium"): (60, 120),
            ("renewed-not-offered", "basic"): (30, 120),
            **cells,
        },
    )
    score_forecasts(
        BUNDLE,
        profile=PROFILE,
        next_profile=following,
        observed_at=OBSERVED,
        state_dir=state,
    )
    return following


# ------------------------------------------------------------------ a run with no forecast


def test_a_run_with_no_forecast_keeps_its_evidence_and_its_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch)
    reference_evidence, reference_members = _reference(tmp_path, monkeypatch, read=False)
    layout, result = _shipped(tmp_path / "state", tmp_path)
    evidence = (layout.run_dir / "EVIDENCE.md").read_bytes()
    assert evidence == reference_evidence
    assert b"Forecast Accuracy" not in evidence and b"Weakest grade" not in evidence
    members = _members(Path(result.artifacts["archive"]))
    for name in NO_FORECAST_MEMBERS:
        assert members[name] == reference_members[name], name
    assert sorted(name[len("bundle/run/") :] for name in members if "/run/" in name) == (
        NO_FORECAST_RUN_FILES
    )
    assert not (layout.run_dir / "forecast-accuracy.json").exists()


def test_a_forecast_registered_for_another_bundle_adds_nothing_to_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch)
    reference_evidence, reference_members = _reference(tmp_path, monkeypatch, read=True)
    state = tmp_path / "state"
    store = AccuracyStore.for_state_dir(state)
    document: Dict[str, Any] = {
        "prediction_id": "elsewhere.817592e3a2233384",
        "run_digest": "sha256:" + "9" * 64,
        "metric_id": "query:renewed-offered/premium",
        "statement": "A forecast made from another bundle.",
        "predicted": "0.5",
        "interval_low": "0.4",
        "interval_high": "0.6",
        "horizon_end": HORIZON,
        "registered_at": REGISTERED,
        "stateDigest": STATE_DIGEST,
        "inputDigest": "sha256:" + "0" * 64,
    }
    store.register(prediction_from_document(document), document)
    layout, result = _shipped(state, tmp_path)
    evidence = (layout.run_dir / "EVIDENCE.md").read_bytes()
    assert evidence == reference_evidence
    members = _members(Path(result.artifacts["archive"]))
    for name in NO_FORECAST_MEMBERS:
        assert members[name] == reference_members[name], name
    assert "bundle/run/forecast-accuracy.json" not in members
    assert not (layout.run_dir / "forecast-accuracy.json").exists()


# ------------------------------------------------------------------ pending rows


def test_a_ship_after_forecast_make_lists_every_forecast_as_pending(
    state: Path, tmp_path: Path
) -> None:
    layout, result = _shipped(state, tmp_path)
    text = (layout.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    assert text.index("## Fidelity") < text.index("## Forecast Accuracy")
    assert text.index("## Forecast Accuracy") < text.index("## Privacy Grade")
    rows = _rows(text)
    assert [row[0] for row in rows] == [f"`{BASIC}`", f"`{PREMIUM}`"]
    for row in rows:
        assert row[1].startswith("`query:renewed-")
        assert row[2] == f"`{STATE_DIGEST}`"
        assert row[3] == f"`{REGISTERED}`"
        assert row[5:] == ["pending"] * 5
    assert "Registered for this run: 2. Scored: 0. Not scored: 0. Pending: 2." in text
    assert "Measured interval coverage: not measured, no forecast has been scored." in text
    assert "Declared interval coverage: 0.92, from the Playback `pb-renewal`." in text
    assert f"Pinned state `{STATE_DIGEST}`: **P1 (checked)**" in text
    document = json.loads((layout.run_dir / "forecast-accuracy.json").read_text("utf-8"))
    assert [row["status"] for row in document["rows"]] == ["pending", "pending"]
    members = _members(Path(result.artifacts["archive"]))
    assert "bundle/run/forecast-accuracy.json" in members
    packed = (layout.run_dir / "forecast-accuracy.json").read_bytes()
    assert members["bundle/run/forecast-accuracy.json"] == hashlib.sha256(packed).hexdigest()


# ------------------------------------------------------------------ scored rows


def test_a_ship_after_forecast_score_lists_every_forecast_with_its_scores(
    state: Path, tmp_path: Path
) -> None:
    following = _score(state, tmp_path)
    layout, _ = _shipped(state, tmp_path)
    text = (layout.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    rows = {row[0]: row for row in _rows(text)}
    premium, basic = rows[f"`{PREMIUM}`"], rows[f"`{BASIC}`"]
    # Observation, result, interval score, absolute error and drift of a hit and of a miss.
    assert premium[5] == "0.6666666666666666666666666667 (180 records)"
    assert premium[6] == "inside"
    assert basic[5] == "0.8 (150 records)"
    assert basic[6] == "outside"
    assert float(basic[7]) > float(premium[7]) > 0
    assert float(basic[8]) > 0 and float(premium[8]) > 0
    assert premium[9].startswith("`psi` ") and ", stale" in premium[9]
    assert "Inside their interval: 1 of 2." in text
    assert "Measured interval coverage: 0.5 (1 of 2 scored forecasts)." in text
    assert "Declared interval coverage: 0.92, from the Playback `pb-renewal`." in text
    next_digest = json.loads(
        AccuracyStore.for_state_dir(state).verdict_path(PREMIUM).read_text("utf-8")
    )["nextStateDigest"]
    assert f"Next state `{next_digest}`: **P1 (checked)**" in text
    assert following.name.startswith("next-state")
    require_no_banned_words(text)


def test_the_file_holds_the_rows_of_the_section(state: Path, tmp_path: Path) -> None:
    _score(state, tmp_path)
    layout, _ = _shipped(state, tmp_path)
    text = (layout.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    document = json.loads((layout.run_dir / "forecast-accuracy.json").read_text("utf-8"))
    rows = _rows(text)
    assert [f"`{row['predictionId']}`" for row in document["rows"]] == [row[0] for row in rows]
    for filed, shown in zip(document["rows"], rows, strict=True):
        assert shown[5].startswith(filed["observed"])
        assert shown[6] == ("inside" if filed["inside"] else "outside")
        assert shown[7] == filed["intervalScore"] and shown[8] == filed["absoluteError"]
        assert f"`{filed['stateDigest']}`" == shown[2]
    assert document["totals"]["scored"] == 2 and document["totals"]["inside"] == 1
    assert document["totals"]["measuredCoverage"] == "0.5"
    assert document["totals"]["declaredCoverage"] == "0.92"
    assert document["grades"]["weakest"] == "P1"


def test_a_forecast_not_scored_is_listed_with_its_reason(state: Path, tmp_path: Path) -> None:
    _score(state, tmp_path)
    following = next_state(tmp_path, "withheld", cells={("renewed-offered", "premium"): None})
    score_forecasts(
        BUNDLE, profile=PROFILE, next_profile=following, observed_at=OBSERVED, state_dir=state
    )
    layout, _ = _shipped(state, tmp_path)
    text = (layout.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    rows = {row[0]: row for row in _rows(text)}
    assert rows[f"`{PREMIUM}`"][5].startswith("not scored: ")
    assert "withheld" in rows[f"`{PREMIUM}`"][5]
    assert "Scored: 1. Not scored: 1. Pending: 0." in text


def test_a_second_bundles_forecast_on_the_same_state_is_counted(
    state: Path, tmp_path: Path
) -> None:
    store = AccuracyStore.for_state_dir(state)
    document: Dict[str, Any] = {
        "prediction_id": "premium-offered-b.817592e3a2233384",
        "run_digest": "sha256:" + "9" * 64,
        "metric_id": "query:renewed-offered/premium",
        "statement": "A forecast made from a second bundle on the same state.",
        "predicted": "0.5",
        "interval_low": "0.4",
        "interval_high": "0.6",
        "horizon_end": HORIZON,
        "registered_at": REGISTERED,
        "stateDigest": STATE_DIGEST,
        "inputDigest": "sha256:" + "0" * 64,
    }
    store.register(prediction_from_document(document), document)
    layout, _ = _shipped(state, tmp_path)
    text = (layout.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    assert (
        f"State `{STATE_DIGEST}`: 3 forecasts in the store across all bundles, "
        "2 of them for this run." in text
    )
    # The other bundle's forecast is counted, never listed as this run's.
    assert "premium-offered-b" not in text
    document_file = json.loads((layout.run_dir / "forecast-accuracy.json").read_text("utf-8"))
    assert document_file["stateCounts"] == {STATE_DIGEST: 3}
    assert len(document_file["rows"]) == 2


def test_the_privacy_grade_states_the_weakest_grade_of_every_profile_read(
    state: Path, tmp_path: Path
) -> None:
    _score(state, tmp_path)
    layout, _ = _shipped(state, tmp_path)
    text = (layout.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    privacy = text.split("## Privacy Grade")[1].split("## Column Provenance")[0]
    assert "Run grade: **P1 (checked)**." in privacy
    assert "Weakest grade of every profile this run read: **P1 (checked)**." in privacy


# ------------------------------------------------------------------ the whole loop


def test_forecast_make_forecast_score_and_ship_from_the_command_line(
    tmp_path: Path, capsys: Any
) -> None:
    state = ["--state-dir", str(tmp_path / "state")]
    following = next_state(
        tmp_path,
        cells={
            ("renewed-offered", "premium"): (60, 120),
            ("renewed-not-offered", "basic"): (30, 120),
        },
    )
    steps = [
        [
            "forecast",
            "make",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            "--registered-at",
            REGISTERED,
            "--horizon-end",
            HORIZON,
            *state,
        ],
        [
            "forecast",
            "score",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            "--next",
            str(following),
            "--observed-at",
            OBSERVED,
            *state,
        ],
        [
            "run",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            *state,
            "--ship",
            "-o",
            str(tmp_path / "out"),
        ],
    ]
    for step in steps:
        code = main(step)
        captured = capsys.readouterr()
        assert code == 0, (step[:2], captured.err, captured.out)
    (report,) = (tmp_path / "state").glob("runs/*/EVIDENCE.md")
    text = report.read_text(encoding="utf-8")
    assert "## Forecast Accuracy" in text
    rows = {row[0]: row for row in _rows(text)}
    assert rows[f"`{PREMIUM}`"][6] == "inside" and rows[f"`{BASIC}`"][6] == "outside"
    assert (report.parent / "forecast-accuracy.json").is_file()
    archive = tmp_path / "out" / "bundle.flux.tgz"
    assert "bundle/run/forecast-accuracy.json" in _members(archive)
