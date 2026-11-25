# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --contract --profile --overlay FILE`` (ADR 0030 sections 7 and 8).

Pinned here, through the command and a real run:

* each overlay file is read with the namespace's exact reader, and gets a bundle in a directory
  named after it, written from the overlaid profile, with the overlay in its Simulation, the
  profile pinned as published, and its register citing the overlay for the numbers it changed
  and no other;
* the bundles plan, apply and verify, and the scorecard names the overlay;
* an overlay file the reader refuses, two files that name one overlay, a patch that names what
  the profile does not hold, and a directory that cannot take a bundle write nothing;
* without ``--overlay``, ``init`` writes the bytes it wrote before.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest
import yaml

from flux_engine.cli import main
from flux_engine.cli._init import run_init
from flux_engine.core.errors import FluxError
from flux_engine.seam.overlay import read_overlay_file
from flux_engine.seam.profile import load_profile
from flux_engine.seam.resolve import FLX_3105
from tests.overlays import ORDERS_CONTRACT, ORDERS_MEAN_PROFILE, ORDERS_PROFILE

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
DOUBLE = "name: double-orders\npatch:\n  tables:\n    orders:\n      rowCount: 1200\n"
PRICIER = """name: pricier
patch:
  tables:
    orders:
      columns:
        amount:
          statistics:
          - definition: mean/1
            disclosure: acro-default
            sampleSize: {min: 598, max: 602}
            groups:
            - {Sx: '1887436800', group: '', k: 600, status: F}
"""

#: ``init`` without ``--overlay`` writes these bytes for the orders contract and profile: the
#: simulation differs from what it was before by the ``replacedBy`` of each assumption, and by the
#: ``amount`` histogram property, which the profile has no statistic to score (it is left out), and
#: each document by its header, which no longer names the profile's file.
UNCHANGED = {
    "orders.fluid.yaml": "65cf48e26642e0f7b0664ad134157292c2a3c20226214c95d895a25f9fdb8be2",
    "persona.flux.yml": "df39c8a70a44a947445827a6c844014636971aec1e4877a1516af42b1391662b",
    "simulation.flux.yml": "9131587b411f026de3a944a90fe855e2cb5ee10be8b33ca6c0db0fca7fcfe492",
    "world.flux.yml": "08b6c2dda4defc044dbd29cc08a42eea427f3c0a7eaeff5f1b827311a616b205",
}


def _file(root: Path, name: str, text: str) -> str:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _init(root: Path, *overlays: str, profile: Path = ORDERS_MEAN_PROFILE, **more: Any) -> Any:
    return run_init(
        str(root / "bundles"),
        contract=str(ORDERS_CONTRACT),
        profile=str(profile),
        overlays=overlays,
        **more,
    )


def _simulation(bundle: Path) -> Dict[str, Any]:
    document = yaml.safe_load((bundle / "simulation.flux.yml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, Any]:
    root = tmp_path_factory.mktemp("overlays")
    result = _init(root, _file(root, "double.yml", DOUBLE), _file(root, "pricier.yml", PRICIER))
    assert result.exit_code == 0, result.stderr
    return root / "bundles", result


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_an_overlay_file_is_read_as_the_namespace_reads_it(tmp_path: Path) -> None:
    overlay = read_overlay_file(Path(_file(tmp_path, "d.yml", DOUBLE)), code=FLX_3105)
    assert overlay.name == "double-orders"
    assert overlay.patch == {"tables": {"orders": {"rowCount": 1200}}}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("name: Double\npatch: {}\n", "name matches [a-z][a-z0-9-]*"),
        ("name: x\n", "names its 'patch'"),
        ("name: x\npatch: {privacy: null}\n", "touches 'privacy'"),
        ("name: x\npatch:\n  tables:\n    orders:\n      rowCount: 1.5\n", "written as a decimal"),
        ("name: x\npatch: {}\n---\nname: y\npatch: {}\n", "holds one document, and this holds 2"),
        ("", "holds one document, and this holds 0"),
        ("- name: x\n", "holds one document"),
        ("name: [unclosed\n", "d.yml"),
    ],
)
def test_an_overlay_file_the_reader_refuses_is_refused(
    tmp_path: Path, text: str, problem: str
) -> None:
    with pytest.raises(FluxError) as refused:
        read_overlay_file(Path(_file(tmp_path, "d.yml", text)), code=FLX_3105)
    assert refused.value.code == FLX_3105 and problem in str(refused.value)


def test_a_file_that_is_missing_or_not_text_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FluxError, match="cannot read"):
        read_overlay_file(tmp_path / "absent.yml", code=FLX_3105)
    with pytest.raises(FluxError, match="not a regular file"):
        read_overlay_file(tmp_path, code=FLX_3105)
    (tmp_path / "binary.yml").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(FluxError, match="not UTF-8"):
        read_overlay_file(tmp_path / "binary.yml", code=FLX_3105)


# ---------------------------------------------------------------------------
# The bundles
# ---------------------------------------------------------------------------


def test_each_overlay_gets_a_bundle_in_a_directory_named_after_it(
    written: Tuple[Path, Any],
) -> None:
    root, result = written
    assert sorted(path.name for path in root.iterdir() if path.is_dir()) == [
        "double-orders",
        "pricier",
    ]
    for name in ("double-orders", "pricier"):
        assert sorted(path.name for path in (root / name).iterdir()) == [
            "orders.fluid.yaml",
            "persona.flux.yml",
            "requests",
            "simulation.flux.yml",
            "world.flux.yml",
        ]
    assert "created 2 bundles" in result.stdout
    assert [item["overlay"]["name"] for item in result.artifacts["overlays"]] == [
        "double-orders",
        "pricier",
    ]


def test_the_simulation_declares_the_overlay_and_pins_the_profile_as_published(
    written: Tuple[Path, Any],
) -> None:
    root, _ = written
    pinned = load_profile(ORDERS_MEAN_PROFILE).digest.value
    block = _simulation(root / "double-orders")["extensions"][NAMESPACE]
    assert block["profile"] == {"digest": pinned}
    assert block["overlay"] == {
        "name": "double-orders",
        "patch": {"tables": {"orders": {"rowCount": 1200}}},
    }
    assert _simulation(root / "pricier")["extensions"][NAMESPACE]["profile"] == {"digest": pinned}


def test_the_numbers_come_from_the_overlaid_profile(written: Tuple[Path, Any]) -> None:
    root, _ = written
    assert (
        _simulation(root / "double-orders")["extensions"][NAMESPACE]["emits"]["orders"]["rows"]
        == 1200
    )
    assert _simulation(root / "pricier")["extensions"][NAMESPACE]["emits"]["orders"]["rows"] == 600


def test_the_register_cites_the_overlay_for_the_numbers_it_changed_and_no_other(
    written: Tuple[Path, Any],
) -> None:
    root, result = written
    entries = _simulation(root / "double-orders")["extensions"][NAMESPACE]["parameters"]
    cited = [entry for entry in entries if entry["source"] == "overlay"]
    assert len(cited) == 1
    assert cited[0]["overlay"] == "double-orders"
    assert cited[0]["statistic"] == {"pointer": "/tables/orders/rowCount"}
    assert cited[0]["pointer"].endswith("/emits/orders/rows")
    # Every other profile citation is of a statistic the patch left alone.
    assert all(
        entry["statistic"]["pointer"] != "/tables/orders/rowCount"
        for entry in entries
        if entry["source"] == "profile"
    )
    counts = result.artifacts["overlays"][0]["parameters"]["counts"]
    assert counts["overlay"] == 1
    assert "1 from the overlay" in result.stdout
    # The patch of `pricier` restates a statistic no number of the bundle rests on.
    other = _simulation(root / "pricier")["extensions"][NAMESPACE]["parameters"]
    assert not [entry for entry in other if entry["source"] == "overlay"]
    assert result.artifacts["overlays"][1]["parameters"]["counts"]["overlay"] == 0


def test_the_header_says_the_overlay_is_a_scenario(written: Tuple[Path, Any]) -> None:
    root, _ = written
    text = (root / "double-orders" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert "# The overlay double-orders patches the profile" in text
    assert "not a measurement" in text


# ---------------------------------------------------------------------------
# End to end, by the command line
# ---------------------------------------------------------------------------


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Tuple[int, str]:
    capsys.readouterr()
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_init_with_an_overlay_then_plan_apply_and_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _cli(
        capsys,
        "init",
        str(tmp_path / "bundles"),
        "--contract",
        str(ORDERS_CONTRACT),
        "--profile",
        str(ORDERS_MEAN_PROFILE),
        "--overlay",
        _file(tmp_path, "double.yml", DOUBLE),
        "--overlay",
        _file(tmp_path, "pricier.yml", PRICIER),
        "--json",
    )
    assert code == 0, out
    assert len(json.loads(out)["artifacts"]["overlays"]) == 2
    bundle = tmp_path / "bundles" / "double-orders"
    state = tmp_path / "state"
    common = ["--profile", str(ORDERS_MEAN_PROFILE), "--state-dir", str(state)]
    code, out = _cli(capsys, "plan", str(bundle), *common, "--json")
    assert code == 0, out
    planned = json.loads(out)["artifacts"]
    run_id = planned["run_id"]
    assert planned["parameter_register"]["counts"]["overlay"] == 1
    code, out = _cli(capsys, "apply", str(bundle), "--run-id", run_id, "--state-dir", str(state))
    assert code == 0, out
    receipt = json.loads((state / "runs" / run_id / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["emits"]["orders"]["rows"] == 1200
    code, out = _cli(capsys, "verify", str(bundle), "--run-id", run_id, *common, "--json")
    assert code == 0, out
    scorecard = json.loads(out)["artifacts"]["scorecard"]["document"]
    assert scorecard["overlay"]["name"] == "double-orders"
    assert scorecard["_schemaURL"].endswith("/scorecard/1-4-0/FluxScorecard.json")
    record = json.loads((state / "runs" / run_id / "parameter-register.json").read_text("utf-8"))
    assert record["_schemaURL"].endswith("/parameter-register/1-3-0/FluxParameterRegister.json")
    assert record["counts"]["overlay"] == 1


# ---------------------------------------------------------------------------
# Refusals write nothing
# ---------------------------------------------------------------------------


def _nothing_written(root: Path) -> bool:
    return not (root / "bundles").exists() or not list((root / "bundles").iterdir())


def test_an_overlay_the_reader_refuses_writes_nothing(tmp_path: Path) -> None:
    good = _file(tmp_path, "double.yml", DOUBLE)
    bad = _file(tmp_path, "bad.yml", "name: x\npatch: {encoding: {fractionalBits: 8}}\n")
    result = _init(tmp_path, good, bad)
    assert (result.exit_code, result.error_code) == (1, "FLX-3105")
    assert "bad.yml" in result.stderr and "touches 'encoding'" in result.stderr
    assert _nothing_written(tmp_path)


def test_two_files_that_name_one_overlay_write_nothing(tmp_path: Path) -> None:
    result = _init(tmp_path, _file(tmp_path, "a.yml", DOUBLE), _file(tmp_path, "b.yml", DOUBLE))
    assert result.exit_code == 1 and "both name the overlay double-orders" in result.stderr
    assert _nothing_written(tmp_path)


def test_a_patch_that_names_a_table_the_profile_lacks_writes_nothing(tmp_path: Path) -> None:
    other = "name: stray\npatch: {tables: {shipments: {rowCount: 3}}}\n"
    result = _init(tmp_path, _file(tmp_path, "a.yml", DOUBLE), _file(tmp_path, "b.yml", other))
    assert (result.exit_code, result.error_code) == (1, "FLX-3105")
    assert "table 'shipments'" in result.stderr and "stray" in result.stderr
    assert _nothing_written(tmp_path)


def test_a_directory_that_cannot_take_a_bundle_removes_the_ones_written_before(
    tmp_path: Path,
) -> None:
    (tmp_path / "bundles" / "pricier").mkdir(parents=True)
    (tmp_path / "bundles" / "pricier" / "held.txt").write_text("mine", encoding="utf-8")
    result = _init(tmp_path, _file(tmp_path, "a.yml", DOUBLE), _file(tmp_path, "b.yml", PRICIER))
    assert result.exit_code == 1 and "is not empty" in result.stderr
    assert sorted(path.name for path in (tmp_path / "bundles").iterdir()) == ["pricier"]
    assert (tmp_path / "bundles" / "pricier" / "held.txt").read_text(encoding="utf-8") == "mine"


def test_an_overlay_needs_a_contract_and_a_profile(tmp_path: Path) -> None:
    overlay = _file(tmp_path, "a.yml", DOUBLE)
    only_contract = run_init(
        str(tmp_path / "x"), contract=str(ORDERS_CONTRACT), overlays=(overlay,)
    )
    assert only_contract.exit_code == 2 and "--overlay patches the profile" in only_contract.stderr
    nothing = run_init(str(tmp_path / "y"), overlays=(overlay,))
    assert nothing.exit_code == 2


def test_strict_is_checked_for_every_bundle_before_any_is_written(tmp_path: Path) -> None:
    result = _init(tmp_path, _file(tmp_path, "a.yml", DOUBLE), strict=True)
    assert result.exit_code == 1 and "--strict refuses" in result.stderr
    assert "the overlay double-orders" in result.stderr
    assert _nothing_written(tmp_path)


# ---------------------------------------------------------------------------
# Without an overlay
# ---------------------------------------------------------------------------


def test_without_an_overlay_init_writes_the_bytes_it_wrote_before(tmp_path: Path) -> None:
    result = run_init(
        str(tmp_path / "bundle"), contract=str(ORDERS_CONTRACT), profile=str(ORDERS_PROFILE)
    )
    assert result.exit_code == 0, result.stderr
    found = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (tmp_path / "bundle").iterdir()
        if path.is_file()
    }
    assert found == UNCHANGED
    assert "with the overlay" not in result.stdout and "overlay" not in dict(result.artifacts)


def test_a_bundle_init_wrote_for_an_overlay_ships_with_its_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``init --overlay``, plan, apply, verify and ship: G agrees at plan and at ship's recompile.

    The overlay enters ``H`` and not ``G`` (ADR 0030 section 7), in the seam and in the fidelity
    compiler alike, so ``ship`` raises no ``FLX-8001`` on ``contract.plannedG``.
    """
    import tarfile

    from flux_engine.stages.ship import ship

    code, out = _cli(
        capsys,
        "init",
        str(tmp_path / "bundles"),
        "--contract",
        str(ORDERS_CONTRACT),
        "--profile",
        str(ORDERS_MEAN_PROFILE),
        "--overlay",
        _file(tmp_path, "double.yml", DOUBLE),
    )
    assert code == 0, out
    bundle = tmp_path / "bundles" / "double-orders"
    state = tmp_path / "state"
    common = ["--profile", str(ORDERS_MEAN_PROFILE), "--state-dir", str(state)]
    code, out = _cli(capsys, "plan", str(bundle), *common, "--json")
    assert code == 0, out
    run_id = json.loads(out)["artifacts"]["run_id"]
    assert _cli(capsys, "apply", str(bundle), "--run-id", run_id, "--state-dir", str(state))[0] == 0
    code, out = _cli(capsys, "verify", str(bundle), "--run-id", run_id, *common)
    assert code == 0, out
    shipped = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        profile=ORDERS_MEAN_PROFILE,
    )
    assert shipped.success, shipped.stderr
    assert shipped.error_code is None and "FLX-8001" not in shipped.stderr
    with tarfile.open(shipped.artifacts["archive"]) as packed:
        member = next(name for name in packed.getnames() if name.endswith("/run/EVIDENCE.md"))
        extracted = packed.extractfile(member)
        assert extracted is not None
        report = extracted.read().decode("utf-8")
    assert "`double-orders`" in report and "the maintainers' scenario, not measured" in report


# ---------------------------------------------------------------------------
# An overlay on a mean is the mean the run is drawn to and scored against
# ---------------------------------------------------------------------------

#: The published ``sum / count`` is 46.25. An overlay that restates the mean as 40 (``Sx`` is
#: ``mean * 600 * 65536``) is more than a tolerance away, so a run drawn to the published sum fails
#: the ``mean`` property the overlay's own statistic gives.
LEANER = PRICIER.replace("pricier", "leaner").replace("1887436800", "1572864000")


def test_an_overlay_that_restates_the_mean_is_the_mean_the_run_passes_its_own_check_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _cli(
        capsys,
        "init",
        str(tmp_path / "bundles"),
        "--contract",
        str(ORDERS_CONTRACT),
        "--profile",
        str(ORDERS_MEAN_PROFILE),
        "--overlay",
        _file(tmp_path, "leaner.yml", LEANER),
    )
    assert code == 0, out
    bundle = tmp_path / "bundles" / "leaner"
    state = tmp_path / "state"
    common = ["--profile", str(ORDERS_MEAN_PROFILE), "--state-dir", str(state)]
    code, out = _cli(capsys, "plan", str(bundle), *common, "--json")
    assert code == 0, out
    run_id = json.loads(out)["artifacts"]["run_id"]
    assert _cli(capsys, "apply", str(bundle), "--run-id", run_id, "--state-dir", str(state))[0] == 0
    code, out = _cli(capsys, "verify", str(bundle), "--run-id", run_id, *common)
    assert code == 0, out
    assert "outside the declared margin" not in out
