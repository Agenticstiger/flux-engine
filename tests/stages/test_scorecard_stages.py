# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify`` and ``ship`` score a run against the profile its plan pins.

docs/spec/SCORECARD-SPEC.md, end to end on the small telco bundle: the run is
planned and applied with its profile, then verified and shipped with it.
Pinned here:

* the acceptance of the scorecard: a run with one column drawn from the wrong
  distribution is withheld, and the scorecard names the column;
* a run that matches its profile ships, and the pack carries the scorecard
  the gate decided on, byte for byte;
* a missed threshold withholds, naming the property to look at, and a
  property the profile withheld is censored, never a failure;
* a pinned run needs its profile, and a run that pins none is untouched.

The runs a test only reads, or ships with a refusal that writes nothing, are
shared per module; a test that changes what another reads gets its own.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from jsonschema import Draft202012Validator

from flux_engine.concordance.scorecard import SCORECARD_FILE
from flux_engine.seam.profile import load_profile
from flux_engine.stages.run import run
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify
from tests.stats.profiles import edited, profile_from_rows, statistic_of

from .conftest import FIXTURE_BUNDLE, write_committed_slice

EXPOSE = "payment_recovery_moment"
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "docs" / "spec" / "scorecard-1.schema.json").read_text(
        encoding="utf-8"
    )
)
#: How far the shifted profile moves the mean of ``amount``: inside the
#: declared absolute tolerance of 50, so the property stays within while
#: its similarity drops below 1.
SHIFT = 10


class Unpinned:
    """One run of a bundle (the one shipped, by default), which pins no profile."""

    def __init__(self, root: Path, source: Path = FIXTURE_BUNDLE) -> None:
        self.state = root / "state"
        result = run(source, state_dir=self.state)
        assert result.success, result.stderr
        self.run_id: str = result.artifacts["plan"]["run_id"]
        self.run_dir = self.state / "runs" / self.run_id


def _pinned_bundle(
    root: Path, profile: Dict[str, Any], scorecard: str = "", source: Path = FIXTURE_BUNDLE
) -> Tuple[Path, Path, str]:
    """Write *profile* and a copy of *source* that pins it; return both paths and the digest."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "source.profile.json"
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    digest = load_profile(path).digest.value
    bundle = root / "bundle"
    shutil.copytree(source, bundle)
    (bundle / "flux-manifest.json").unlink(missing_ok=True)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    anchor = "  com.agentics.flux/1:\n    emits:\n"
    assert text.count(anchor) == 1
    simulation.write_text(
        text.replace(
            anchor,
            f"  com.agentics.flux/1:\n    profile:\n      digest: {digest}\n"
            f"{scorecard}    emits:\n",
        ),
        encoding="utf-8",
    )
    return bundle, path, digest


class Pinned:
    """A bundle that pins one profile, planned and applied with it."""

    def __init__(
        self,
        root: Path,
        profile: Dict[str, Any],
        scorecard: str = "",
        source: Path = FIXTURE_BUNDLE,
    ) -> None:
        self.bundle, self.profile, self.digest = _pinned_bundle(root, profile, scorecard, source)
        self.state = root / "state"
        applied = run(self.bundle, state_dir=self.state, profile=self.profile)
        assert applied.success, applied.stderr
        self.run_id: str = applied.artifacts["plan"]["run_id"]
        self.run_dir = self.state / "runs" / self.run_id

    def verify(self, profile: Optional[Path] = None, **kwargs: Any) -> Any:
        return verify(
            self.bundle, run_id=self.run_id, state_dir=self.state, profile=profile, **kwargs
        )

    def ship(self, out: Path, profile: Optional[Path] = None, **kwargs: Any) -> Any:
        return ship(
            self.bundle,
            run_id=self.run_id,
            out_dir=out,
            state_dir=self.state,
            profile=profile,
            **kwargs,
        )


@pytest.fixture(scope="module")
def unpinned(tmp_path_factory: pytest.TempPathFactory) -> Unpinned:
    return Unpinned(tmp_path_factory.mktemp("unpinned"))


@pytest.fixture(scope="module")
def compiled_and_rows(unpinned: Unpinned) -> Tuple[Any, List[Dict[str, Any]]]:
    """The block's compiled form and the rows the unpinned run emitted."""
    from flux_engine.concordance.compiler import compile_fidelity
    from flux_engine.concordance.predicate import expose_from_contract

    run_dir = unpinned.run_dir
    fidelity_input = json.loads((run_dir / f"fidelity-input-{EXPOSE}.json").read_text())
    contract = fidelity_input["contract"]
    compiled = compile_fidelity(
        contract=contract,
        emit=fidelity_input["emit"],
        expose=expose_from_contract(contract, EXPOSE),
    )
    columns = json.loads((run_dir / f"seam-attestation-{EXPOSE}.json").read_text())["columns"]
    lines = (run_dir / "emits" / f"{EXPOSE}.ndjson").read_text().splitlines()
    rows = [dict(zip(columns, json.loads(line), strict=True)) for line in lines if line]
    return compiled, rows


@pytest.fixture(scope="module")
def own_profile(compiled_and_rows: Tuple[Any, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """A profile of the run's own rows: what the run must match exactly."""
    compiled, rows = compiled_and_rows
    return profile_from_rows(rows, compiled)


@pytest.fixture(scope="module")
def drifted_profile(
    compiled_and_rows: Tuple[Any, List[Dict[str, Any]]], tmp_path_factory: pytest.TempPathFactory
) -> Dict[str, Any]:
    """A profile of a committed slice whose ``amount`` follows another distribution."""
    compiled, _ = compiled_and_rows
    path = tmp_path_factory.mktemp("slice") / "slice.ndjson"
    write_committed_slice(path)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    return profile_from_rows(rows, compiled)


@pytest.fixture(scope="module")
def own_pinned(own_profile: Dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Pinned:
    """A run pinned to its own profile, for the tests that change nothing another reads."""
    return Pinned(tmp_path_factory.mktemp("own"), own_profile)


def _shifted(profile: Dict[str, Any], shift: int = SHIFT) -> Dict[str, Any]:
    """*profile* with the mean of ``amount`` moved by *shift*, on the 16-bit grid."""
    document = edited(profile)
    (group,) = statistic_of(document, EXPOSE, "amount", "mean/1")["groups"]
    group["Sx"] = str(int(group["Sx"]) + shift * (1 << 16) * group["k"])
    return document


def _censored(profile: Dict[str, Any]) -> Dict[str, Any]:
    """*profile* with the support of ``market`` withheld, as a disclosure rule would."""
    document = edited(profile)
    statistic_of(document, EXPOSE, "market", "support/1")["groups"] = [{"group": "", "status": "A"}]
    return document


def _property(document: Dict[str, Any], property_id: str) -> Dict[str, Any]:
    (table,) = [table for table in document["tables"] if table["exposeId"] == EXPOSE]
    return next(entry for entry in table["properties"] if entry["propertyId"] == property_id)


def _packed(archive: Path, name: str) -> bytes:
    with tarfile.open(archive) as packed:
        member = next(item for item in packed.getmembers() if item.name.endswith(f"/run/{name}"))
        extracted = packed.extractfile(member)
        assert extracted is not None
        return extracted.read()


def test_a_run_with_one_column_from_the_wrong_distribution_is_withheld_and_named(
    tmp_path: Path, drifted_profile: Dict[str, Any]
) -> None:
    pinned = Pinned(tmp_path, drifted_profile)
    out = tmp_path / "out"
    shipped = pinned.ship(out, profile=pinned.profile)
    assert shipped.exit_code == 1 and shipped.error_code == "FLX-8001", shipped.stderr
    assert not out.exists() or not any(out.iterdir()), "a withheld ship writes nothing"
    assert not (pinned.run_dir / SCORECARD_FILE).exists()
    document = shipped.artifacts["scorecard"]
    Draft202012Validator(SCHEMA).validate(document)
    named = _property(document, "000:mean:amount")
    assert (named["outcome"], named["columns"]) == ("outside", ["amount"])
    assert "property 000:mean:amount (mean of amount)" in shipped.stdout
    assert "scorecard: overall" in shipped.stdout
    assert "shape: " in shipped.stdout, "a withheld ship still shows the shape flags"

    verified = pinned.verify(profile=pinned.profile)
    assert verified.exit_code == 1
    assert "property 000:mean:amount (mean of amount): outside the declared margin" in (
        verified.stderr
    )
    assert verified.artifacts["fidelity"][EXPOSE]["reference"] == "profile"
    assert verified.artifacts["scorecard"]["document"] == document

    # A committed slice of the run's own rows passes the slice's gate; the
    # pinned profile still finds `amount` outside, and verify says what ship
    # does: the emit fails, and the check names the column.
    uri = _own_rows_slice(pinned.run_dir, tmp_path / "own.ndjson")
    both = pinned.verify(profile=pinned.profile, committed_slice={EXPOSE: uri})
    leg = both.artifacts["fidelity"][EXPOSE]
    assert leg["status"] == "failed" and leg["profile"]["status"] == "failed", leg
    assert leg["outside"] == [], "the slice found nothing outside"
    assert "against the pinned profile" in both.stderr
    assert "property 000:mean:amount (mean of amount)" in both.stderr
    gated = pinned.ship(tmp_path / "both", profile=pinned.profile, committed_slice={EXPOSE: uri})
    checks = {check["check"]: check for check in gated.artifacts["decisions"][EXPOSE]["checks"]}
    assert checks["properties.outcomes"]["passed"] is True
    assert checks["profile.properties"]["passed"] is False
    assert "000:mean:amount (mean of amount)" in checks["profile.properties"]["detail"]


def _own_rows_slice(run_dir: Path, path: Path) -> str:
    """The run's own rows as a committed slice: the slice's gate finds them identical."""
    columns = json.loads((run_dir / f"seam-attestation-{EXPOSE}.json").read_text())["columns"]
    lines = (run_dir / "emits" / f"{EXPOSE}.ndjson").read_text().splitlines()
    rows = [dict(zip(columns, json.loads(line), strict=True)) for line in lines if line]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path.as_uri()


def test_a_run_that_matches_its_profile_ships_with_its_scorecard(
    tmp_path: Path, own_pinned: Pinned
) -> None:
    pinned = own_pinned
    verified = pinned.verify(profile=pinned.profile)
    assert verified.success, verified.stderr
    leg = verified.artifacts["scorecard"]
    assert leg["status"] == "computed" and leg["met"] is True
    assert leg["document"]["overall"]["score"] == "1"
    assert verified.artifacts["fidelity"][EXPOSE]["status"] == "passed"
    assert "  scorecard: overall 1 against profile " + pinned.digest in verified.stdout

    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile)
    assert shipped.success, shipped.stderr
    on_disk = (pinned.run_dir / SCORECARD_FILE).read_bytes()
    assert _packed(Path(shipped.artifacts["archive"]), SCORECARD_FILE) == on_disk
    assert json.loads(on_disk) == leg["document"], "verify and ship score the run alike"
    assert not (
        pinned.run_dir / f"fidelity-record-{EXPOSE}.json"
    ).exists(), "the profile path writes no fidelity record"
    evidence = _packed(Path(shipped.artifacts["archive"]), "EVIDENCE.md").decode("utf-8")
    assert "fidelity checked against the pinned profile, 2 of 2" in evidence
    assert f"Scorecard (`run/{SCORECARD_FILE}`), against the pinned profile" in evidence
    assert "the measured score replaces the Playback's declared credibility" in evidence
    assert "signed with a key generated in memory" not in evidence, "no record was signed"
    assert "These dimensions were not measured at all: joint, crossTable" in evidence

    again = pinned.ship(tmp_path / "again", profile=pinned.profile)
    assert again.success, again.stderr
    assert (pinned.run_dir / SCORECARD_FILE).read_bytes() == on_disk, "two ships, one scorecard"


def test_a_missed_threshold_withholds_and_names_where_to_look(
    tmp_path: Path, own_profile: Dict[str, Any]
) -> None:
    block = (
        "    scorecard:\n      thresholds:\n"
        '        overall: { mustBeGreaterOrEqualTo: "0.9999" }\n'
    )
    pinned = Pinned(tmp_path, _censored(_shifted(own_profile)), scorecard=block)
    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile)
    assert shipped.exit_code == 1, shipped.stderr
    decision = shipped.artifacts["decisions"][EXPOSE]
    failed = [check for check in decision["checks"] if not check["passed"]]
    assert [check["check"] for check in failed] == ["scorecard.threshold"]
    assert (
        "threshold overall mustBeGreaterOrEqualTo 0.9999: not met (0.9989); lowest property "
        "000:mean:amount (mean of amount) of payment_recovery_moment: 0.9989"
    ) in failed[0]["detail"]
    document = shipped.artifacts["scorecard"]
    assert _property(document, "000:mean:amount")["outcome"] == "within"
    censored = _property(document, "001:support:market")
    assert (censored["outcome"], censored["score"]) == ("censored", None)
    assert "001:support:market" not in shipped.stdout, "a censored property is no failure"

    verified = pinned.verify(profile=pinned.profile)
    assert verified.exit_code == 1
    assert "threshold overall mustBeGreaterOrEqualTo 0.9999: not met" in verified.stderr


def test_a_censored_property_ships_and_is_reported_as_not_scored(
    tmp_path: Path, own_profile: Dict[str, Any]
) -> None:
    pinned = Pinned(tmp_path, _censored(own_profile))
    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile)
    assert shipped.success, shipped.stderr
    evidence = (pinned.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    assert "1 of 2 declared properties within tolerance, 1 censored and not scored" in evidence
    document = json.loads((pinned.run_dir / SCORECARD_FILE).read_text())
    structure = document["dimensions"][0]
    assert (structure["dimension"], structure["status"]) == ("structure", "not measured")


def test_a_pinned_run_needs_its_own_profile(
    tmp_path: Path, own_pinned: Pinned, drifted_profile: Dict[str, Any]
) -> None:
    pinned = own_pinned
    unscored = pinned.verify()
    assert unscored.success, unscored.stderr
    assert unscored.artifacts["scorecard"] == {
        "status": "not checked",
        "detail": f"this run's plan pins profile {pinned.digest}; pass that profile with "
        "--profile to score the run",
    }
    assert "pass that profile with --profile" in unscored.artifacts["fidelity"][EXPOSE]["detail"]

    out = tmp_path / "out"
    refused = pinned.ship(out)
    assert refused.exit_code == 1 and refused.error_code == "FLX-3109", refused.stderr
    assert not out.exists()

    other = tmp_path / "other.profile.json"
    other.write_text(json.dumps(drifted_profile), encoding="utf-8")
    for mismatched in (pinned.verify(profile=other), pinned.ship(out, profile=other)):
        assert mismatched.error_code == "FLX-3110", mismatched.stderr
    assert not out.exists()


def test_a_run_that_pins_no_profile_has_no_scorecard(tmp_path: Path, unpinned: Unpinned) -> None:
    state, run_id = unpinned.state, unpinned.run_id
    verified = verify(FIXTURE_BUNDLE, run_id=run_id, state_dir=state)
    assert "scorecard" not in verified.artifacts
    assert verified.artifacts["fidelity"][EXPOSE] == {
        "status": "declared, not checked",
        "detail": "declares a fidelity block; pass --committed-slice "
        f"{EXPOSE}=file://<path> to run the check",
    }
    assert "scorecard" not in verified.stdout
    profile = tmp_path / "stray.profile.json"
    profile.write_text(json.dumps({"_producer": "urn:x"}), encoding="utf-8")
    out = tmp_path / "out"
    for refused in (
        verify(FIXTURE_BUNDLE, run_id=run_id, state_dir=state, profile=profile),
        ship(FIXTURE_BUNDLE, run_id=run_id, out_dir=out, state_dir=state, profile=profile),
    ):
        assert refused.error_code == "FLX-3109", refused.stderr
    assert not out.exists()

    # A scorecard.json nothing formed would be packed as the gate's: refused.
    planted = unpinned.run_dir / SCORECARD_FILE
    planted.write_text('{"met": true}\n', encoding="utf-8")
    try:
        stray = ship(FIXTURE_BUNDLE, run_id=run_id, out_dir=out, state_dir=state)
        assert stray.error_code == "FLX-8001", stray.stderr
        assert "pins no profile, so no scorecard was formed for it" in stray.stderr
        assert not out.exists()
    finally:
        planted.unlink()


def test_a_slice_and_a_profile_are_both_checked(tmp_path: Path, own_pinned: Pinned) -> None:
    pinned = own_pinned
    uri = write_committed_slice(tmp_path / "slice.ndjson")
    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile, committed_slice={EXPOSE: uri})
    checks = {check["check"]: check for check in shipped.artifacts["decisions"][EXPOSE]["checks"]}
    assert {"properties.outcomes", "record.signature", "profile.properties"} <= set(checks)
    assert checks["profile.properties"]["passed"] is True, "the run matches its own profile"
    # The slice describes another distribution, so its gate withholds the emit
    # whatever the profile found: both references must agree it is within.
    assert checks["properties.outcomes"]["passed"] is False and shipped.exit_code == 1


def test_run_hands_its_profile_to_verify_and_ship(
    tmp_path: Path, own_profile: Dict[str, Any]
) -> None:
    bundle, profile, _ = _pinned_bundle(tmp_path / "pinned", own_profile)
    result = run(
        bundle,
        state_dir=tmp_path / "state",
        profile=profile,
        verify=True,
        ship=True,
        out_dir=tmp_path / "out",
    )
    assert result.success, result.stderr
    assert result.artifacts["verify"]["scorecard"]["status"] == "computed"
    run_dir = tmp_path / "state" / "runs" / result.artifacts["plan"]["run_id"]
    assert json.loads((run_dir / SCORECARD_FILE).read_text()) == (
        result.artifacts["verify"]["scorecard"]["document"]
    )


@pytest.mark.parametrize(
    "value", ["abc", "1.5", "-0.1", True, 0.7, "NaN", "1e-99999999", "1e999999999"]
)
def test_a_recorded_min_credibility_that_is_no_floor_is_refused(value: Any) -> None:
    from flux_engine.core.errors import FluxError
    from flux_engine.stages._scorecard import min_credibility

    assert min_credibility({"emit": {"minCredibility": "0.7"}}) == "0.7"
    assert min_credibility({"emit": {}}) is None
    with pytest.raises(FluxError) as raised:
        min_credibility({"emit": {"minCredibility": value}})
    assert raised.value.code.code == "FLX-3105"


def _copy_of(pinned: Pinned, root: Path) -> Pinned:
    """*pinned* with its state directory copied, so a test can break the copy."""
    copy = object.__new__(Pinned)
    copy.__dict__.update(pinned.__dict__)
    copy.state = root / "state"
    shutil.copytree(pinned.state, copy.state)
    copy.run_dir = copy.state / "runs" / pinned.run_id
    return copy


@pytest.mark.parametrize("damage", ["scalar-row", "attestation-gone", "attestation-not-json"])
def test_a_damaged_run_directory_is_refused_by_both_stages_never_raised(
    tmp_path: Path, own_pinned: Pinned, damage: str
) -> None:
    broken = _copy_of(own_pinned, tmp_path)
    if damage == "scalar-row":
        emits = broken.run_dir / "emits" / f"{EXPOSE}.ndjson"
        emits.write_text("5\n" + emits.read_text(encoding="utf-8"), encoding="utf-8")
    elif damage == "attestation-gone":
        (broken.run_dir / f"seam-attestation-{EXPOSE}.json").unlink()
    else:
        (broken.run_dir / f"seam-attestation-{EXPOSE}.json").write_text("{", encoding="utf-8")
    verified = broken.verify(profile=broken.profile)
    assert verified.exit_code == 1 and verified.error_code, verified.stderr
    shipped = broken.ship(tmp_path / "out", profile=broken.profile)
    assert shipped.exit_code == 1 and shipped.error_code, shipped.stderr
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


@pytest.mark.parametrize("damage", ["gone", "not-json", "no-contract"])
def test_an_unreadable_fidelity_input_refuses_the_emit(
    tmp_path: Path, own_pinned: Pinned, damage: str
) -> None:
    broken = _copy_of(own_pinned, tmp_path)
    recorded = broken.run_dir / f"fidelity-input-{EXPOSE}.json"
    if damage == "gone":
        recorded.unlink()
    elif damage == "not-json":
        recorded.write_text("{", encoding="utf-8")
    else:
        recorded.write_text(json.dumps({"emit": {}}), encoding="utf-8")
    verified = broken.verify(profile=broken.profile)
    assert verified.exit_code == 1, verified.stderr
    (table,) = verified.artifacts["scorecard"]["document"]["tables"]
    assert table["declaresFidelity"] is True and table["status"] == "refused", table
    shipped = broken.ship(tmp_path / "out", profile=broken.profile)
    assert shipped.exit_code == 1, shipped.stderr


def test_a_record_this_ship_did_not_write_is_named_and_never_attributed(
    tmp_path: Path, own_pinned: Pinned
) -> None:
    copy = _copy_of(own_pinned, tmp_path)
    uri = _own_rows_slice(copy.run_dir, tmp_path / "own.ndjson")
    first = copy.ship(tmp_path / "first", profile=copy.profile, committed_slice={EXPOSE: uri})
    assert first.success, first.stderr
    evidence = (copy.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    assert "Signature: the fidelity records this ship wrote were signed with a key" in evidence
    record = copy.run_dir / f"fidelity-record-{EXPOSE}.json"
    # A profile-only ship never reads the record, so it says the same of a
    # file put there by hand as of the one the first ship signed.
    record.write_text(json.dumps({"forged": True}), encoding="utf-8")
    second = copy.ship(tmp_path / "second", profile=copy.profile)
    assert second.success, second.stderr
    evidence = (copy.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    assert (
        "also holds a fidelity record that this ship did not write, read or check, "
        "and this report attributes it to no key"
    ) in evidence
    assert "Signature:" not in evidence, "this ship signed nothing, so it attributes nothing"


def test_a_record_this_ship_did_not_write_is_named_beside_an_emit_with_no_fidelity_block() -> None:
    from flux_engine.concordance.gate import GateAction, GateDecision
    from flux_engine.stages.ship import _fidelity_summary_text

    decisions = {"plain": GateDecision(action=GateAction.EMIT, expose_id="plain", checks=())}
    assert _fidelity_summary_text(decisions) == "- `plain`: no fidelity block declared"
    named = _fidelity_summary_text(decisions, {"plain"}, {"plain"})
    assert named.startswith(
        "- `plain`: no fidelity block declared; the run directory also holds a fidelity record"
    )
    assert named.endswith("and this report attributes it to no key")


def test_a_stream_that_cannot_be_read_refuses_its_table_beside_a_slice(
    tmp_path: Path, own_pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from flux_engine.concordance.errors import FLX_8001
    from flux_engine.core.errors import FluxError

    # The module, not the `ship` function `flux_engine.stages` re-exports
    # under the same name.
    ship_module = importlib.import_module("flux_engine.stages.ship")
    copy = _copy_of(own_pinned, tmp_path)
    uri = _own_rows_slice(copy.run_dir, tmp_path / "own.ndjson")

    def unreadable(path: Path) -> List[List[Any]]:
        raise FluxError(FLX_8001, f"{path.name}: line 1 is not JSON (Expecting value)")

    monkeypatch.setattr(ship_module, "_emit_rows", unreadable)
    shipped = copy.ship(tmp_path / "out", profile=copy.profile, committed_slice={EXPOSE: uri})
    assert shipped.exit_code == 1 and shipped.error_code == "FLX-8001", shipped.stderr
    (table,) = shipped.artifacts["scorecard"]["tables"]
    assert table["status"] == "refused", table
    assert any("not compared" in refusal["reason"] for refusal in table["refusals"]), table
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_a_missed_score_about_an_emit_the_run_did_not_decide_withholds_every_emit() -> None:
    from types import SimpleNamespace

    from flux_engine.concordance.gate import GateAction, GateDecision
    from flux_engine.stages.ship import _gate_on_scorecard

    decisions = {
        name: GateDecision(action=GateAction.EMIT, expose_id=name, checks=()) for name in "ab"
    }
    missed = SimpleNamespace(
        thresholds=[SimpleNamespace(met=False, threshold="overall")],
        credibility=[],
        threshold_line=lambda result: "overall 0.5 is below the floor 0.9",
        contributors=lambda threshold: ["not-an-emit-of-this-run"],
    )
    low = SimpleNamespace(
        thresholds=[],
        credibility=[
            SimpleNamespace(met=False, expose_id="not-an-emit-of-this-run", line=lambda: "low")
        ],
    )
    for scorecard, check in ((missed, "scorecard.threshold"), (low, "scorecard.credibility")):
        gated = _gate_on_scorecard(dict(decisions), scorecard)  # type: ignore[arg-type]
        assert {name: decision.action for name, decision in gated.items()} == {
            "a": GateAction.WITHHOLD,
            "b": GateAction.WITHHOLD,
        }
        assert all(check in [c.name for c in d.checks] for d in gated.values())


def test_a_pinned_run_replays_to_its_own_digest(tmp_path: Path, own_pinned: Pinned) -> None:
    from flux_engine.stages.replay import replay

    copy = _copy_of(own_pinned, tmp_path)
    plan_document = json.loads((copy.run_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan_document["profileDigest"] == copy.digest
    recorded = json.loads((copy.run_dir / "run-digest.json").read_text(encoding="utf-8"))
    from flux_engine.core.digest import digest_of_document

    expected = digest_of_document(recorded, subject="runDigest").value
    replayed = replay(copy.bundle, run_id=copy.run_id, assert_digest=expected, state_dir=copy.state)
    assert replayed.success, replayed.stderr


def test_a_censored_group_is_never_listed_as_outside() -> None:
    from flux_engine.concordance.predicate import PropertyKind
    from flux_engine.concordance.verifier import CENSORED, OUTSIDE, GroupOutcome, PropertyOutcome
    from flux_engine.stages.verify import _outside_document

    outcome = PropertyOutcome(
        property_id="003:mean:amount@market",
        kind=PropertyKind.MEAN,
        columns=("amount",),
        group_by="market",
        tolerance={"type": "absolute", "value": "2"},
        margin="2",
        epsilon="0",
        outcome=OUTSIDE,
        groups=(
            GroupOutcome(group="a", committed_count=0, synthetic_count=5, outcome=CENSORED),
            GroupOutcome(group="b", committed_count=9, synthetic_count=5, outcome=OUTSIDE),
        ),
    )
    assert _outside_document(outcome, refused=False)["groupOrdinals"] == [1]


# ---------------------------------------------------------------------------
# Shape flags (SCORECARD-SPEC.md §6)
# ---------------------------------------------------------------------------


def _all_distinct(profile: Dict[str, Any], column: str) -> Dict[str, Any]:
    """*profile* saying every record of *column* holds its own value, which no property reads."""
    document = edited(profile)
    records = document["tables"][EXPOSE]["rowCount"]
    document["tables"][EXPOSE]["columns"][column]["metrics"] = {
        name: {
            "definition": f"{name}/1",
            "status": "F",
            "sampleSize": {"min": 1},
            "disclosure": "none",
            "value": records,
        }
        for name in ("count", "distinctCount")
    }
    return document


def test_every_profiled_column_is_checked_and_a_faithful_run_shows_no_flag(
    own_pinned: Pinned,
) -> None:
    verified = own_pinned.verify(profile=own_pinned.profile)
    assert verified.success, verified.stderr
    shape = verified.artifacts["scorecard"]["document"]["shapeFlags"]
    assert shape["status"] == "checked" and shape["level"] == "1/1000"
    (table,) = shape["tables"]
    assert table["exposeId"] == EXPOSE and table["status"] == "checked"
    assert table["columns"] and all(column["flags"] == [] for column in table["columns"])
    assert any(
        line.strip().startswith("shape: no flags in") for line in verified.stdout.splitlines()
    )


def test_a_shape_flag_informs_and_withholds_nothing(
    tmp_path: Path, own_profile: Dict[str, Any]
) -> None:
    # The profile says every record of `market` holds its own value, and the
    # run's handful is far below half of what that allows. No property reads
    # the metrics, so every comparison is unchanged and the run ships.
    pinned = Pinned(tmp_path / "pinned", _all_distinct(own_profile, "market"))
    verified = pinned.verify(profile=pinned.profile)
    assert verified.success, verified.stderr
    document = verified.artifacts["scorecard"]["document"]
    assert document["met"] is True
    Draft202012Validator(SCHEMA).validate(document)
    (table,) = document["shapeFlags"]["tables"]
    flagged = {
        column["column"]: [flag["alert"] for flag in column["flags"]] for column in table["columns"]
    }
    assert flagged["market"] == ["LOW_CARDINALITY"]
    assert f"shape of {EXPOSE}.market: LOW_CARDINALITY" in verified.stdout

    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile)
    assert shipped.success, shipped.stderr
    archive = Path(shipped.artifacts["archive"])
    assert json.loads(_packed(archive, SCORECARD_FILE))["shapeFlags"] == document["shapeFlags"]
    evidence = _packed(archive, "EVIDENCE.md").decode("utf-8")
    assert f"- shape of {EXPOSE}.market: LOW_CARDINALITY" in evidence
    assert "they inform and withhold nothing" in evidence


def test_an_emit_with_no_fidelity_block_is_read_for_its_shape(own_pinned: Pinned) -> None:
    from flux_engine.stages.layout import RunLayout
    from flux_engine.stages.verify import shape_report

    layout = RunLayout(state_dir=own_pinned.state, run_id=own_pinned.run_id)
    receipt = {"emits": {EXPOSE: {"declaresFidelity": False}}}
    report = shape_report(layout, receipt, load_profile(own_pinned.profile))
    (table,) = report.tables
    assert table.summary.status == "checked"
    assert {column.summary.status for column in table.columns} == {"checked"}


def _with_far_bin(profile: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """*profile* with a bin table on ``amount`` that puts half its records above every run value."""
    from decimal import Decimal
    from math import floor

    document = edited(profile)
    amounts = [Decimal(str(row["amount"])) for row in rows if row.get("amount") is not None]
    low, high = floor(min(amounts)), floor(max(amounts)) + 1
    records = len(amounts)
    document["tables"][EXPOSE]["columns"]["amount"]["statistics"].append(
        {
            "definition": "marginalHistogram/1",
            "sampleSize": {"min": records, "max": records},
            "disclosure": "none",
            "groups": [
                {
                    "group": "",
                    "k": records,
                    "binCounts": [records // 2, records - records // 2],
                    "status": "F",
                }
            ],
            "bins": [str(low), str(high), str(2 * high)],
        }
    )
    return document


def test_an_empty_bin_is_flagged_through_verify_and_ship(
    tmp_path: Path,
    own_profile: Dict[str, Any],
    compiled_and_rows: Tuple[Any, List[Dict[str, Any]]],
) -> None:
    _, rows = compiled_and_rows
    pinned = Pinned(tmp_path / "pinned", _with_far_bin(own_profile, rows))
    verified = pinned.verify(profile=pinned.profile)
    assert verified.success, verified.stderr
    (table,) = verified.artifacts["scorecard"]["document"]["shapeFlags"]["tables"]
    (amount,) = [column for column in table["columns"] if column["column"] == "amount"]
    assert amount["flags"] == [{"alert": "EMPTY_BINS", "cells": [1]}]
    assert f"shape of {EXPOSE}.amount: EMPTY_BINS (cells 1)" in verified.stdout

    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile)
    assert shipped.success, shipped.stderr
    evidence = _packed(Path(shipped.artifacts["archive"]), "EVIDENCE.md").decode("utf-8")
    assert f"- shape of {EXPOSE}.amount: EMPTY_BINS (cells 1)" in evidence
    assert "The shape flags test every profiled column at a level of 1/1000" in evidence
