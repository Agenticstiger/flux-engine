# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The parameter register through a real run: plan, apply, verify and ship.

docs/spec/PARAMETER-REGISTER-SPEC.md, end to end. Pinned here:

* a bundle that declares no register plans to the bytes it had, writes no
  record and gets the report it got;
* a declared register is bound by the plan's digest, written by ``apply``,
  reported by ``plan``, ``verify`` and ``ship``, and packed as the bytes
  ``ship`` checked, with its paragraph in ``EVIDENCE.md``;
* a record that is edited, missing, stray, or not the one the plan names is
  refused (``FLX-2116``), and nothing is packed;
* a citation is read against the pinned profile at plan time, and one the
  profile withholds is refused (``FLX-3113``) before anything is written.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from jsonschema import Draft202012Validator

from flux_engine.seam.profile import load_profile
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run
from flux_engine.stages.ship import ship
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify
from tests.stats.profiles import edited, profile_from_rows, statistic_of

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

#: The module, not the function the package exports under the same name.
apply_module = importlib.import_module("flux_engine.stages.apply")

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SCHEMA = json.loads(
    (REPO_ROOT / "docs" / "spec" / "parameter-register-1.schema.json").read_text(encoding="utf-8")
)
EXPOSE = "payment_recovery_moment"
REGISTER_FILE = "parameter-register.json"

#: The starter's plan digest as the how-to pages print it: a bundle that
#: declares no register plans to exactly these bytes.
STARTER_PLAN_DIGEST = "sha256:8835218acf7b4f8e28b738c7d7e9e8d6b5c5e8bca59e47533fdda09eb90cf501"

REGISTER = """extensions:
  com.agentics.flux/1:
    parameters:
      - document: q3-retention-world
        pointer: /spec/population/size
        source: assumption
        reason: Sized for a quick look, not taken from any subscriber count.
      - document: persona-pragmatic-family
        pointer: /spec/traits
        source: assumption
        reason: The growth team's persona workshop.
      - document: persona-pragmatic-family
        pointer: /spec/traits/monthlySpend/min
        source: default
"""


def _starter(root: Path, register: str = REGISTER) -> Path:
    """A copy of the starter, with *register* appended to its Simulation."""
    bundle = root / "bundle"
    shutil.copytree(STARTER, bundle)
    simulation = bundle / "simulation.flux.yml"
    simulation.write_text(simulation.read_text(encoding="utf-8") + register, encoding="utf-8")
    return bundle


class Run:
    """One bundle, planned and applied into its own state directory."""

    def __init__(self, root: Path, bundle: Path) -> None:
        self.bundle = bundle
        self.state = root / "state"
        # Planned under kernel 0.1.0 by name: the pinned plan digest below is the one the engine
        # gave this bundle before kernel 0.2.0 became the default, so a bundle that declares no
        # register keeps the bytes it had.
        planned = plan(bundle, state_dir=self.state, kernel="0.1.0")
        assert planned.success, planned.stderr
        applied = apply(
            bundle, run_id=planned.artifacts["run_id"], state_dir=self.state, auto_plan=False
        )
        assert applied.success, applied.stderr
        self.plan_artifacts: Dict[str, Any] = dict(planned.artifacts)
        self.plan_stdout: str = planned.stdout
        self.run_id: str = self.plan_artifacts["run_id"]
        self.run_dir = self.state / "runs" / self.run_id
        self.plan_document = json.loads((self.run_dir / "plan.json").read_text(encoding="utf-8"))

    @property
    def record(self) -> Path:
        return self.run_dir / REGISTER_FILE

    def verify(self) -> Any:
        return verify(self.bundle, run_id=self.run_id, state_dir=self.state)

    def ship(self, out: Path) -> Any:
        return ship(self.bundle, run_id=self.run_id, out_dir=out, state_dir=self.state)


@pytest.fixture(scope="module")
def declared(tmp_path_factory: pytest.TempPathFactory) -> Run:
    root = tmp_path_factory.mktemp("declared")
    return Run(root, _starter(root))


@pytest.fixture(scope="module")
def undeclared(tmp_path_factory: pytest.TempPathFactory) -> Run:
    root = tmp_path_factory.mktemp("undeclared")
    bundle = root / "bundle"
    shutil.copytree(STARTER, bundle)
    return Run(root, bundle)


def _packed(archive: Path, suffix: str) -> bytes:
    with tarfile.open(archive) as packed:
        member = next(item for item in packed.getmembers() if item.name.endswith(suffix))
        extracted = packed.extractfile(member)
        assert extracted is not None
        return extracted.read()


def _names(archive: Path) -> List[str]:
    with tarfile.open(archive) as packed:
        return packed.getnames()


# ---------------------------------------------------------------------------
# No register: nothing moves
# ---------------------------------------------------------------------------


def test_a_bundle_that_declares_no_register_plans_to_the_bytes_it_had(undeclared: Run) -> None:
    assert undeclared.plan_document["planDigest"] == STARTER_PLAN_DIGEST
    assert "parameterRegisterDigest" not in undeclared.plan_document
    assert "parameter_register" not in undeclared.plan_artifacts
    assert "parameters:" not in undeclared.plan_stdout
    assert not undeclared.record.exists()


def test_a_run_with_no_register_is_verified_and_shipped_as_before(
    undeclared: Run, tmp_path: Path
) -> None:
    verified = undeclared.verify()
    assert verified.success, verified.stderr
    assert "parameterRegister" not in verified.artifacts
    assert "parameters:" not in verified.stdout
    shipped = undeclared.ship(tmp_path / "out")
    assert shipped.success, shipped.stderr
    assert "parameters:" not in shipped.stdout
    assert not [
        name for name in _names(Path(shipped.artifacts["archive"])) if REGISTER_FILE in name
    ]
    report = _packed(Path(shipped.artifacts["archive"]), "/run/EVIDENCE.md").decode("utf-8")
    assert "Parameter register" not in report


# ---------------------------------------------------------------------------
# A declared register
# ---------------------------------------------------------------------------


def test_the_plan_binds_the_record_apply_writes(declared: Run) -> None:
    data = declared.record.read_bytes()
    assert declared.plan_document["parameterRegisterDigest"] == (
        "sha256:" + hashlib.sha256(data).hexdigest()
    )
    assert declared.plan_document["planDigest"] != STARTER_PLAN_DIGEST
    assert declared.plan_artifacts["parameter_register"] == {
        "declared": 20,
        "counts": {"profile": 0, "assumption": 8, "default": 12},
    }
    assert "parameters: 20 declared -- 0 from a profile, 8 assumed, 12 default" in (
        declared.plan_stdout
    )
    document = json.loads(data)
    assert not list(Draft202012Validator(SCHEMA).iter_errors(document))
    # The rows and the run are the undeclared run's: the register changes no draw.
    receipt = json.loads((declared.run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["emits"][EXPOSE]["rows"] == 154


def test_verify_reads_the_record_through_the_plan_and_reports_the_shares(declared: Run) -> None:
    verified = declared.verify()
    assert verified.success, verified.stderr
    assert verified.artifacts["parameterRegister"] == {
        "declared": 20,
        "counts": {"profile": 0, "assumption": 8, "default": 12},
    }
    assert "  parameters: 20 declared -- 0 from a profile, 8 assumed, 12 default" in (
        verified.stdout.splitlines()
    )


def test_ship_packs_the_bytes_it_checked_and_states_the_shares(
    declared: Run, tmp_path: Path
) -> None:
    shipped = declared.ship(tmp_path / "out")
    assert shipped.success, shipped.stderr
    archive = Path(shipped.artifacts["archive"])
    assert _packed(archive, f"/run/{REGISTER_FILE}") == declared.record.read_bytes()
    assert shipped.artifacts["parameterRegister"]["declared"] == 20
    assert "parameters: 20 declared -- 0 from a profile, 8 assumed, 12 default" in shipped.stdout
    report = _packed(archive, "/run/EVIDENCE.md").decode("utf-8")
    not_proven = report.split("## What Was Not Proven", 1)[1]
    assert "Parameter register: the register counts 20 numbers of this run:" in not_proven
    assert "It does not count seeds, a sweep," in not_proven
    assert "(1 number): `Sized for a quick look, not taken from any subscriber count.`" in (
        not_proven
    )


def test_a_recorded_plan_applied_again_writes_the_same_record(declared: Run) -> None:
    before = declared.record.read_bytes()
    again = apply(declared.bundle, run_id=declared.run_id, state_dir=declared.state)
    assert again.success, again.stderr
    assert declared.record.read_bytes() == before


def test_validate_counts_the_register_of_each_simulation(declared: Run) -> None:
    validated = validate(declared.bundle)
    assert validated.success, validated.stderr
    assert validated.artifacts["simulations"]["sim-payment-recovery"]["parameters"] == {
        "declared": 20,
        "counts": {"profile": 0, "assumption": 8, "default": 12},
    }


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _own_run(tmp_path: Path, register: str = REGISTER) -> Run:
    return Run(tmp_path, _starter(tmp_path, register))


def test_an_edited_record_is_refused_and_nothing_is_packed(tmp_path: Path) -> None:
    own = _own_run(tmp_path)
    document = json.loads(own.record.read_text(encoding="utf-8"))
    document["counts"] = {"profile": 20, "assumption": 0, "default": 0}
    own.record.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verified = own.verify()
    assert (verified.exit_code, verified.error_code) == (1, "FLX-2116")
    shipped = own.ship(tmp_path / "out")
    assert (shipped.exit_code, shipped.error_code) == (1, "FLX-2116")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_a_missing_record_is_refused(tmp_path: Path) -> None:
    own = _own_run(tmp_path)
    own.record.unlink()
    assert own.verify().error_code == "FLX-2116"
    assert own.ship(tmp_path / "out").error_code == "FLX-2116"


def test_a_record_in_a_run_whose_plan_binds_none_is_refused(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(STARTER, bundle)
    own = Run(tmp_path, bundle)
    own.record.write_text("{}\n", encoding="utf-8")
    verified = own.verify()
    assert verified.error_code == "FLX-2116"
    assert "binds no parameter register" in verified.stderr
    shipped = own.ship(tmp_path / "out")
    assert shipped.error_code == "FLX-2116"


def test_apply_refuses_a_record_the_plan_does_not_name_before_any_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _starter(tmp_path)
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state, stage=False)
    assert planned.success, planned.stderr
    real = apply_module.parameter_register_text
    monkeypatch.setattr(
        apply_module, "parameter_register_text", lambda result: real(result).replace("500", "501")
    )
    applied = apply(bundle, run_id=planned.artifacts["run_id"], state_dir=state)
    assert (applied.exit_code, applied.error_code) == (1, "FLX-2116")
    run_dir = state / "runs" / planned.artifacts["run_id"]
    assert not (run_dir / "receipt.json").exists()
    assert not (run_dir / REGISTER_FILE).exists()


def test_an_entry_that_names_no_number_of_the_run_is_refused_offline(tmp_path: Path) -> None:
    bundle = _starter(
        tmp_path,
        REGISTER.replace("document: q3-retention-world", "document: q3-retention-wrld"),
    )
    validated = validate(bundle)
    assert validated.exit_code == 1
    assert "FLX-3105" in validated.stderr and "q3-retention-wrld" in validated.stderr
    planned = plan(bundle, state_dir=tmp_path / "state")
    assert planned.error_code == "FLX-3105"
    assert not (tmp_path / "state").exists() or not list((tmp_path / "state").rglob("plan.json"))


# ---------------------------------------------------------------------------
# Citations against the pinned profile
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def own_profile(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    """A profile of the small bundle's own rows, every cell published."""
    from flux_engine.concordance.compiler import compile_fidelity
    from flux_engine.concordance.predicate import expose_from_contract

    state = tmp_path_factory.mktemp("rows") / "state"
    result = run(FIXTURE_BUNDLE, state_dir=state)
    assert result.success, result.stderr
    run_dir = state / "runs" / result.artifacts["plan"]["run_id"]
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
    return profile_from_rows(rows, compiled)


def _cited_bundle(root: Path, profile: Dict[str, Any], entries: str) -> Tuple[Path, Path, str]:
    """The small bundle pinning *profile*, with a register of *entries*."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "source.profile.json"
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    digest = load_profile(path).digest.value
    bundle = root / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, bundle)
    (bundle / "flux-manifest.json").unlink(missing_ok=True)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    anchor = "  com.agentics.flux/1:\n    emits:\n"
    assert text.count(anchor) == 1
    simulation.write_text(
        text.replace(
            anchor,
            f"  com.agentics.flux/1:\n    profile:\n      digest: {digest}\n"
            f"    parameters:\n{entries}    emits:\n",
        ),
        encoding="utf-8",
    )
    return bundle, path, digest


CITATIONS = """      - document: sim-payment-recovery
        pointer: /extensions/com.agentics.flux~11/emits/payment_recovery_moment/rows
        source: profile
        statistic: { pointer: /tables/payment_recovery_moment/rowCount }
      - document: sim-payment-recovery
        pointer: /extensions/com.agentics.flux~11/emits/payment_recovery_moment/fidelity/properties/0
        source: profile
        statistic:
          pointer: /tables/payment_recovery_moment/columns/amount/statistics/0
          definition: mean/1
      - document: pb-billing-history
        pointer: /spec/scorecard
        source: assumption
        reason: The billing team's backtest.
"""


def test_a_run_cites_statistics_of_its_pinned_profile(
    tmp_path: Path, own_profile: Dict[str, Any]
) -> None:
    bundle, profile, digest = _cited_bundle(tmp_path, own_profile, CITATIONS)
    state = tmp_path / "state"
    result = run(bundle, state_dir=state, profile=profile)
    assert result.success, result.stderr
    run_dir = state / "runs" / result.artifacts["plan"]["run_id"]
    document = json.loads((run_dir / REGISTER_FILE).read_text(encoding="utf-8"))
    assert document["profile"] == {"digest": digest}
    assert document["counts"]["profile"] == 2  # rows, and the mean's tolerance
    assert document["counts"]["assumption"] == 2  # the Playback's scorecard
    assert document["entries"][1]["statistic"] == {
        "pointer": "/tables/payment_recovery_moment/columns/amount/statistics/0",
        "definition": "mean/1",
    }
    # Applied again without the profile, the recorded plan rebuilds the same record.
    before = (run_dir / REGISTER_FILE).read_bytes()
    again = apply(bundle, run_id=result.artifacts["plan"]["run_id"], state_dir=state)
    assert again.success, again.stderr
    assert (run_dir / REGISTER_FILE).read_bytes() == before
    verified = verify(
        bundle, run_id=result.artifacts["plan"]["run_id"], state_dir=state, profile=profile
    )
    assert "  parameters: 35 declared -- 2 from the pinned profile, 2 assumed, 31 default" in (
        verified.stdout.splitlines()
    )


def test_a_citation_of_a_statistic_the_profile_withholds_is_refused_at_plan(
    tmp_path: Path, own_profile: Dict[str, Any]
) -> None:
    profile = edited(own_profile)
    statistic_of(profile, EXPOSE, "amount", "mean/1")["groups"] = [{"group": "", "status": "A"}]
    bundle, path, _ = _cited_bundle(tmp_path, profile, CITATIONS)
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state, profile=path)
    assert (planned.exit_code, planned.error_code) == (1, "FLX-3113")
    assert "parameters[1].statistic.pointer reaches a value the pinned profile withheld" in (
        planned.stderr
    )
    assert not state.exists() or not list(state.rglob("plan.json"))


def test_a_citation_in_a_simulation_that_pins_no_profile_is_refused(tmp_path: Path) -> None:
    register = REGISTER + (
        "      - document: det-payment-aborted-burst\n"
        "        pointer: /spec/timeWindowMs\n"
        "        source: profile\n"
        "        statistic: { pointer: /tables/x/rowCount }\n"
    )
    validated = validate(_starter(tmp_path, register))
    assert validated.exit_code == 1
    assert "cites the profile, and the Simulation pins none" in validated.stderr
