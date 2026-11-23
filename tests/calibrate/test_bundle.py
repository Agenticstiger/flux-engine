# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A calibrated bundle: the input bundle with the patch applied, and its register recording each value (ADR 0028 section 8).

* **A new bundle.** The patch's values are written into a copy of the bundle at the output, in a scratch
  directory that is checked and then moved into place, and the input bundle is byte-identical afterwards.
  Every file but the Simulation's keeps its bytes except for the numbers moved.
* **The register** (format 1-3-0). Each moved value has the source ``calibrated`` and its ``calibration``
  member, which names the queries, the value before and after, the patch's digest, the targets' digest, the
  held-out evidence and the versions. An entry that named the number exactly is replaced, and one that
  was broader leaves a new, more specific entry. The record validates against the format's schema.
* **It plans.** The new bundle plans and applies, and draws the calibrated share.
* **Refusals leave nothing.** A name that is taken, a document the text cannot take and a bundle that does
  not validate write nothing and leave no scratch directory.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from jsonschema import Draft202012Validator

pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from flux_engine.calibrate.bundle import (  # noqa: E402
    HeldOutEvidence,
    register_entry,
    write_calibrated_bundle,
)
from flux_engine.calibrate.fit import (  # noqa: E402
    FitRequest,  # noqa: E402
    _prepare,
    fit_ceilings,
)
from flux_engine.calibrate.patch import Operation  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.evidence.register import (  # noqa: E402
    REGISTER_SCHEMA_URL_1_3,
    build_register_document,
)
from flux_engine.runtime import resolve_cast  # noqa: E402
from flux_engine.seam.parameters import Source  # noqa: E402
from flux_engine.seam.profile import load_profile  # noqa: E402
from flux_engine.seam.resolve import load_bundle  # noqa: E402
from flux_engine.stages.pipeline import parameter_register_of  # noqa: E402
from flux_engine.stages.run import run  # noqa: E402
from tests.calibrate._fit_bundle import (  # noqa: E402
    NAMESPACE,
    NULLS,
    SIMULATION,
    copy_fit_bundle,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "consumers"
PROFILE = FIXTURES / "subscriptions.profile.json"
HELD_OUT = FIXTURES / "subscriptions.held-out-profile.json"
SCHEMA = json.loads(
    (REPO_ROOT / "docs" / "spec" / "parameter-register-1-3.schema.json").read_text(encoding="utf-8")
)
PATCH = "sha256:" + "1" * 64
EVIDENCE = HeldOutEvidence(profile="sha256:" + "3" * 64, largest_gap="0.011", tolerance="0.02")
VERSIONS = {"cma": "4.6.0", "sqlglot": "30.33.0", "duckdb": "2.0.1"}


def digest_of(path: Path) -> str:
    return load_profile(path).digest.value


def make(root: Path) -> Path:
    return copy_fit_bundle(root)


def parameters(bundle: Path, root: Path) -> Any:
    prepared = _prepare(
        FitRequest(bundle, PROFILE, root / "calibrated", state_dir=root / "state"),
        fit_ceilings(None, None, None),
    )
    return prepared.parameters


def write(bundle: Path, out: Path, ops: List[Operation], parameters_: Any, **more: Any) -> Any:
    return write_calibrated_bundle(
        bundle,
        out,
        parameters=parameters_,
        operations=ops,
        queries=["premium-renewed"],
        patch=PATCH,
        targets=digest_of(PROFILE),
        held_out=EVIDENCE,
        versions=VERSIONS,
        profile_document=load_profile(PROFILE).document,
        **more,
    )


def snapshot(directory: Path) -> Dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def document(directory: Path, name: str = SIMULATION) -> Dict[str, Any]:
    loaded = yaml.safe_load((directory / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def calibrated(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    root = tmp_path_factory.mktemp("calibrated")
    bundle = make(root)
    before = snapshot(bundle)
    found = parameters(bundle, root)
    written = write(bundle, root / "out", [Operation(SIMULATION, NULLS, "0.02")], found)
    return {"root": root, "bundle": bundle, "before": before, "written": written, "found": found}


# ----------------------------------------------------------------------------- the bundle


def test_the_new_bundle_holds_the_value_and_the_input_bundle_is_byte_identical(
    calibrated: Dict[str, Any],
) -> None:
    out = calibrated["written"].path
    assert out == calibrated["root"] / "out"
    assert snapshot(calibrated["bundle"]) == calibrated["before"]
    columns = document(out)["extensions"][NAMESPACE]["emits"]["subscriptions"]["fidelity"][
        "columns"
    ]
    assert columns["renewed"]["nullFraction"] == "0.02"
    original = document(calibrated["bundle"])["extensions"][NAMESPACE]["emits"]["subscriptions"][
        "fidelity"
    ]["columns"]
    assert original["renewed"]["nullFraction"] == "0.1"


def test_everything_but_the_moved_value_and_the_register_is_as_it_was(
    calibrated: Dict[str, Any],
) -> None:
    out, bundle = calibrated["written"].path, calibrated["bundle"]
    assert sorted(snapshot(out)) == sorted(snapshot(bundle))
    for name in ("world.flux.yml", "persona.flux.yml", "subscriptions.fluid.yaml"):
        assert (out / name).read_bytes() == (bundle / name).read_bytes()
    new, old = document(out), document(bundle)
    new["extensions"][NAMESPACE]["emits"]["subscriptions"]["fidelity"]["columns"]["renewed"][
        "nullFraction"
    ] = "0.1"
    new["extensions"][NAMESPACE]["parameters"] = old["extensions"][NAMESPACE]["parameters"]
    assert new == old


def test_the_register_records_the_value_with_its_queries_before_and_after(
    calibrated: Dict[str, Any],
) -> None:
    loaded = load_bundle(calibrated["written"].path)
    cast = resolve_cast(loaded.flux_documents)
    register = parameter_register_of(loaded, cast)
    assert register is not None
    entries = [entry for entry in register.entries if entry.source is Source.CALIBRATED]
    assert len(entries) == 1
    (entry,) = entries
    assert entry.pointer == NULLS and entry.document == cast.simulation_id
    assert entry.calibration is not None
    assert entry.calibration.as_document() == {
        "queries": ["premium-renewed"],
        "before": "0.1",
        "after": "0.02",
        "patch": PATCH,
        "targets": digest_of(PROFILE),
        "heldOut": {
            "profile": EVIDENCE.profile,
            "largestGap": "0.011",
            "tolerance": "0.02",
        },
        "versions": VERSIONS,
    }
    assert register.counts()["calibrated"] == 1
    moved = [number for number in register.numbers if number.number.pointer == NULLS]
    assert [number.source for number in moved] == [Source.CALIBRATED]


def test_the_register_document_is_format_1_3_0_and_validates(calibrated: Dict[str, Any]) -> None:
    loaded = load_bundle(calibrated["written"].path)
    register = parameter_register_of(loaded, resolve_cast(loaded.flux_documents))
    assert register is not None
    record = build_register_document(register)
    assert record["_schemaURL"] == REGISTER_SCHEMA_URL_1_3
    assert list(Draft202012Validator(SCHEMA).iter_errors(record)) == []
    assert record["counts"]["calibrated"] == 1
    calibrated_rows = [row for row in record["parameters"] if row["source"] == "calibrated"]
    assert [row["pointer"] for row in calibrated_rows] == [NULLS]


def test_an_entry_that_named_the_number_exactly_is_replaced(calibrated: Dict[str, Any]) -> None:
    old = document(calibrated["bundle"])["extensions"][NAMESPACE]["parameters"]
    new = document(calibrated["written"].path)["extensions"][NAMESPACE]["parameters"]
    assert len(new) == len(old)
    assert [item["source"] for item in old if item["pointer"] == NULLS] == ["assumption"]
    (replaced,) = [item for item in new if item["pointer"] == NULLS]
    assert replaced["source"] == "calibrated" and "reason" not in replaced
    assert [item for item in old if item["pointer"] != NULLS] == [
        item for item in new if item["pointer"] != NULLS
    ]


def test_a_number_a_broader_entry_named_gets_a_new_more_specific_entry(tmp_path: Path) -> None:
    bundle = make(tmp_path)
    path = bundle / SIMULATION
    simulation = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = simulation["extensions"][NAMESPACE]["parameters"]
    simulation["extensions"][NAMESPACE]["parameters"] = [
        e for e in entries if e["pointer"] != NULLS
    ]
    path.write_text(yaml.safe_dump(simulation, sort_keys=False), encoding="utf-8")
    found = parameters(bundle, tmp_path)
    assert [item.source for item in found] == [Source.DEFAULT]
    write(bundle, tmp_path / "out", [Operation(SIMULATION, NULLS, "0.03")], found)
    new = document(tmp_path / "out")["extensions"][NAMESPACE]["parameters"]
    assert len(new) == len(entries)  # one fewer than the bundle had, plus the new entry
    assert new[-1]["pointer"] == NULLS and new[-1]["source"] == "calibrated"


def test_a_number_in_another_file_is_written_there_and_registered_by_its_document_id(
    tmp_path: Path,
) -> None:
    bundle = make(tmp_path)
    path = bundle / SIMULATION
    simulation = yaml.safe_load(path.read_text(encoding="utf-8"))
    simulation["extensions"][NAMESPACE]["consumers"]["calibration"]["parameters"] = [
        {
            "document": "persona.flux.yml",
            "pointer": "/spec/traits/digitalFluency",
            "min": "0",
            "max": "1",
            "step": "0.1",
        }
    ]
    path.write_text(yaml.safe_dump(simulation, sort_keys=False), encoding="utf-8")
    found = parameters(bundle, tmp_path)
    assert found[0].doc_id == "persona-telco.silver.subscriptions"
    operation = Operation("persona.flux.yml", "/spec/traits/digitalFluency", "0.7")
    write(bundle, tmp_path / "out", [operation], found)
    persona = (tmp_path / "out" / "persona.flux.yml").read_text(encoding="utf-8")
    original = (bundle / "persona.flux.yml").read_text(encoding="utf-8")
    assert persona == original.replace("digitalFluency: 0.5", "digitalFluency: 0.7")
    entries = document(tmp_path / "out")["extensions"][NAMESPACE]["parameters"]
    (entry,) = [item for item in entries if item["source"] == "calibrated"]
    assert entry["document"] == "persona-telco.silver.subscriptions"
    assert entry["calibration"]["before"] == "0.5" and entry["calibration"]["after"] == "0.7"
    # the Simulation's own bytes are written again only for its register
    assert load_bundle(tmp_path / "out") is not None


def test_a_patch_that_moves_nothing_copies_the_bundle_byte_for_byte(tmp_path: Path) -> None:
    bundle = make(tmp_path)
    write(bundle, tmp_path / "out", [], parameters(bundle, tmp_path))
    assert snapshot(tmp_path / "out") == snapshot(bundle)


def test_the_header_comment_of_the_simulation_survives(tmp_path: Path) -> None:
    bundle = make(tmp_path)
    path = bundle / SIMULATION
    path.write_text("# written by fluxsim init\n# keep me\n" + path.read_text(encoding="utf-8"))
    write(
        bundle,
        tmp_path / "out",
        [Operation(SIMULATION, NULLS, "0.02")],
        parameters(bundle, tmp_path),
    )
    text = (tmp_path / "out" / SIMULATION).read_text(encoding="utf-8")
    assert text.startswith("# written by fluxsim init\n# keep me\nfluxVersion:")


def test_the_new_bundle_plans_applies_and_draws_the_calibrated_share(
    calibrated: Dict[str, Any], tmp_path: Path
) -> None:
    result = run(calibrated["written"].path, state_dir=tmp_path / "state", profile=PROFILE)
    assert result.success, result.stderr
    run_id = result.artifacts["plan"]["run_id"]
    lines = (tmp_path / "state" / "runs" / run_id / "emits" / "subscriptions.ndjson").read_text()
    rows = [json.loads(line) for line in lines.splitlines() if line]
    nulls = Fraction(len([row for row in rows if row[4] is None]), len(rows))
    assert abs(nulls - Fraction(2, 100)) < Fraction(1, 100)
    register = (tmp_path / "state" / "runs" / run_id / "parameter-register.json").read_text()
    assert json.loads(register)["counts"]["calibrated"] == 1


def test_the_entries_the_fit_writes_are_read_back_and_the_bundle_ships(
    calibrated: Dict[str, Any], tmp_path: Path
) -> None:
    from flux_engine.seam.parameters import read_parameters_block
    from flux_engine.seam.resolve import FLX_3105
    from flux_engine.stages.ship import ship

    entries = list(calibrated["written"].entries)
    assert entries and all(entry["source"] == "calibrated" for entry in entries)
    read = read_parameters_block(entries, pinned=True, where="written", code=FLX_3105)
    assert [entry.source for entry in read] == [Source.CALIBRATED] * len(entries)

    state = tmp_path / "state"
    made = run(calibrated["written"].path, state_dir=state, profile=PROFILE)
    assert made.success, made.stderr
    out = tmp_path / "shipped"
    out.mkdir()
    shipped = ship(
        calibrated["written"].path,
        run_id=made.artifacts["plan"]["run_id"],
        out_dir=out,
        state_dir=state,
        profile=PROFILE,
    )
    assert shipped.success, shipped.stderr
    evidence = state / "runs" / made.artifacts["plan"]["run_id"] / "EVIDENCE.md"
    assert "calibrated values, which the bundle declares" in evidence.read_text(encoding="utf-8")


def test_register_entry_spells_one_moved_value(calibrated: Dict[str, Any]) -> None:
    (parameter,) = calibrated["found"]
    entry = register_entry(
        parameter,
        Operation(SIMULATION, NULLS, "0.02"),
        queries=["a", "b"],
        patch=PATCH,
        targets=EVIDENCE.profile,
        held_out=EVIDENCE,
        versions=VERSIONS,
    )
    assert entry["source"] == "calibrated" and entry["calibration"]["queries"] == ["a", "b"]
    assert entry["document"] == parameter.doc_id and entry["pointer"] == NULLS


# ----------------------------------------------------------------------------- refusals


def test_an_output_that_exists_is_refused_and_untouched(tmp_path: Path) -> None:
    bundle = make(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(FluxError) as error:
        write(bundle, tmp_path / "out", [], parameters(bundle, tmp_path))
    assert error.value.code.code == "FLX-3710"
    assert [path.name for path in (tmp_path / "out").iterdir()] == ["keep.txt"]


def test_a_document_that_aliases_values_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    bundle = make(tmp_path)
    path = bundle / SIMULATION
    simulation = yaml.safe_load(path.read_text(encoding="utf-8"))
    simulation["extensions"][NAMESPACE]["consumers"]["calibration"]["parameters"] = [
        {
            "document": "persona.flux.yml",
            "pointer": "/spec/traits/digitalFluency",
            "min": "0",
            "max": "1",
            "step": "0.1",
        }
    ]
    path.write_text(yaml.safe_dump(simulation, sort_keys=False), encoding="utf-8")
    persona = bundle / "persona.flux.yml"
    text = persona.read_text(encoding="utf-8")
    persona.write_text(
        text.replace("digitalFluency: 0.5", "digitalFluency: 0.5\n    other: &a 1\n    again: *a"),
        encoding="utf-8",
    )
    found = parameters(bundle, tmp_path)
    operation = Operation("persona.flux.yml", "/spec/traits/digitalFluency", "0.7")
    with pytest.raises(FluxError) as error:
        write(bundle, tmp_path / "out", [operation], found)
    assert error.value.code.code == "FLX-3711" and "alias" in str(error.value)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle"]


def test_a_bundle_that_does_not_plan_is_not_written_and_no_scratch_is_left(tmp_path: Path) -> None:
    bundle = make(tmp_path)
    found = parameters(bundle, tmp_path)
    with pytest.raises(FluxError):
        write(bundle, tmp_path / "out", [Operation(SIMULATION, NULLS, "7")], found)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle"]


def test_the_input_bundle_is_never_written_even_when_the_write_fails(tmp_path: Path) -> None:
    bundle = make(tmp_path)
    found = parameters(bundle, tmp_path)
    before = snapshot(bundle)
    with pytest.raises(FluxError):
        write(bundle, tmp_path / "out", [Operation(SIMULATION, NULLS, "7")], found)
    assert snapshot(bundle) == before


def test_an_operation_no_parameter_declares_and_a_missing_directory_are_refused(
    tmp_path: Path,
) -> None:
    bundle = make(tmp_path)
    found = parameters(bundle, tmp_path)
    with pytest.raises(FluxError) as error:
        write(bundle, tmp_path / "out", [Operation(SIMULATION, "/spec/seed", "7")], found)
    assert error.value.code.code == "FLX-3711" and "no declared parameter" in str(error.value)
    with pytest.raises(FluxError) as error:
        write(bundle, tmp_path / "missing" / "out", [], found)
    assert error.value.code.code == "FLX-3710"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle"]
