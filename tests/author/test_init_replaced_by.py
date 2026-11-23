# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``replacedBy`` and ``<name>.assumptions.profile-request.yml`` (ADR 0030 section 8).

Pinned here, through the command and a real run:

* every assumed number a profile statistic would answer says which, in ``replacedBy`` (a pointer
  and a definition into a profile of the contract), and no other entry does: a tolerance, a bound
  of a committed state and a default are settings, which no profile statistic replaces;
* the ledger lists the statistics, and ``init`` writes a profile request (format 1-0-0) beside the
  bundle that asks a profiler for exactly those statistics, at the pointers ``replacedBy`` names;
* ``init`` again with a profile that answers the request cites each answered number as the
  profile's, at the pointer the first bundle named;
* the register is format 1-3-0, runs through plan, apply and verify, and ``EVIDENCE.md`` names what
  would replace each assumption;
* the request is never written over a file, and a refusal writes nothing.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from flux_engine.cli import main
from flux_engine.cli._init import run_init
from flux_engine.stages.ship import ship
from flux_stats.request import load_request_text
from tests.overlays import ORDERS_CONTRACT, ORDERS_MEAN_PROFILE, ORDERS_PROFILE

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
REQUEST = ".assumptions.profile-request.yml"


def _entries(bundle: Path) -> List[Dict[str, Any]]:
    document = yaml.safe_load((bundle / "simulation.flux.yml").read_text(encoding="utf-8"))
    entries = document["extensions"][NAMESPACE]["parameters"]
    assert isinstance(entries, list)
    return entries


def _request(root: Path, name: str = "bundle") -> Any:
    return load_request_text((root / f"{name}{REQUEST}").read_text(encoding="utf-8"))


def _write_profile(root: Path, change: Any) -> Path:
    document = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    change(document)
    path = root / "answered.profile.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _answer(document: Dict[str, Any]) -> None:
    """A profile that answers the request: the levels of ``country`` and the nulls of two columns."""
    columns = document["tables"]["orders"]["columns"]
    columns["country"]["statistics"] = [
        {
            "definition": "levelHistogram/1",
            "levels": ["DE", "FR", "GB", "IE", "US"],
            "sampleSize": {"min": 598, "max": 602},
            "disclosure": "acro-default",
            "groups": [
                {
                    "group": "",
                    "k": 600,
                    "levelCounts": [100, 100, 150, 150, 100],
                    "cellStatus": ["F", "F", "F", "F", "F"],
                    "status": "F",
                }
            ],
        }
    ]
    for name in ("amount", "created_at"):
        columns[name]["metrics"]["nullCount"] = {
            "definition": "nullCount/1",
            "disclosure": "acro-default",
            "sampleSize": {"min": 598, "max": 602},
            "status": "F",
            "value": 30,
        }


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, Any]:
    root = tmp_path_factory.mktemp("replaced")
    result = run_init(
        str(root / "bundle"), contract=str(ORDERS_CONTRACT), profile=str(ORDERS_PROFILE)
    )
    assert result.exit_code == 0, result.stderr
    return root, result


# ---------------------------------------------------------------------------
# replacedBy
# ---------------------------------------------------------------------------


def test_an_assumed_number_a_statistic_would_answer_says_which(written: Tuple[Path, Any]) -> None:
    root, _ = written
    replaced = {
        entry["pointer"].rsplit("/emits/", 1)[1]: entry["replacedBy"]
        for entry in _entries(root / "bundle")
        if "replacedBy" in entry
    }
    assert replaced["orders/fidelity/columns/country/levels"] == {
        "pointer": "/tables/orders/columns/country/statistics/0",
        "definition": "levelHistogram/1",
    }
    assert replaced["orders/fidelity/columns/amount/nullFraction"] == {
        "pointer": "/tables/orders/columns/amount/metrics/nullCount",
        "definition": "nullCount/1",
    }
    assert replaced["orders/fidelity/columns/amount/bins/1"] == {
        "pointer": "/tables/orders/columns/amount/statistics/0",
        "definition": "marginalHistogram/1",
    }


def test_only_an_assumption_a_statistic_would_replace_names_one(written: Tuple[Path, Any]) -> None:
    root, _ = written
    for entry in _entries(root / "bundle"):
        if "replacedBy" in entry:
            assert entry["source"] == "assumption", entry
    settings = [
        entry
        for entry in _entries(root / "bundle")
        if entry["source"] == "assumption"
        and ("/tolerance/" in entry["pointer"] or entry["pointer"].endswith("/fidelity/records"))
    ]
    assert settings and all("replacedBy" not in entry for entry in settings)
    defaults = [entry for entry in _entries(root / "bundle") if entry["source"] == "default"]
    assert defaults and all("replacedBy" not in entry for entry in defaults)


def test_an_assumed_row_count_is_replaced_by_the_tables_row_count(tmp_path: Path) -> None:
    def without_rows(document: Dict[str, Any]) -> None:
        del document["tables"]["orders"]["rowCount"]
        for column in document["tables"]["orders"]["columns"].values():
            column["metrics"].pop("count", None)

    profile = _write_profile(tmp_path, without_rows)
    result = run_init(str(tmp_path / "bundle"), contract=str(ORDERS_CONTRACT), profile=str(profile))
    assert result.exit_code == 0, result.stderr
    (rows,) = [e for e in _entries(tmp_path / "bundle") if e["pointer"].endswith("/orders/rows")]
    assert rows["source"] == "assumption"
    assert rows["replacedBy"] == {"pointer": "/tables/orders/rowCount"}
    assert _request(tmp_path).table("orders").row_count is True


# ---------------------------------------------------------------------------
# The ledger and the request
# ---------------------------------------------------------------------------


def test_the_ledger_lists_the_statistics_that_would_replace_assumptions(
    written: Tuple[Path, Any],
) -> None:
    root, result = written
    listed = {
        (item["pointer"], item["definition"]): item["assumptions"]
        for item in result.artifacts["ledger"]["replacedBy"]
    }
    assert listed[("/tables/orders/columns/country/statistics/0", "levelHistogram/1")] == 1
    assert listed[("/tables/orders/columns/amount/statistics/0", "marginalHistogram/1")] == 8
    assert "a profile of the contract would replace assumptions" in result.stdout
    assert f"{root / ('bundle' + REQUEST)} asks a profiler for them" in result.stdout
    assert (
        "  - /tables/orders/columns/country/statistics/0 (levelHistogram/1): 1 assumed number"
        in (result.stdout)
    )
    assert result.artifacts["assumptionsRequest"] == str(root / ("bundle" + REQUEST))


def test_the_request_asks_for_exactly_the_statistics_replacedBy_names(
    written: Tuple[Path, Any],
) -> None:
    root, result = written
    asked = _request(root)
    # Read by the request reader (format 1-0-0), beside the bundle and never inside it.
    assert not (root / "bundle" / ("bundle" + REQUEST)).exists()
    assert sorted(path.name for path in (root / "bundle").iterdir()) == [
        "orders.fluid.yaml",
        "persona.flux.yml",
        "simulation.flux.yml",
        "world.flux.yml",
    ]
    assert asked.relationships == () and asked.facts == ()
    pointers = set()
    for table in asked.tables:
        if table.row_count:
            pointers.add((f"/tables/{table.expose_id}/rowCount", None))
        for column in table.columns:
            base = f"/tables/{table.expose_id}/columns/{column.name}"
            pointers |= {(f"{base}/metrics/{name}", None) for name in column.metrics}
            pointers |= {
                (f"{base}/statistics/{index}", identifier)
                for index, identifier in enumerate(column.statistics)
            }
    named = {
        (entry["replacedBy"]["pointer"], entry["replacedBy"].get("definition"))
        for entry in _entries(root / "bundle")
        if "replacedBy" in entry
    }
    # A metric is named by its definition too: the request names it by its short name.
    assert {pointer for pointer, _ in named} == {pointer for pointer, _ in pointers}
    assert {pointer for pointer, definition in named if definition and "statistics" in pointer} == {
        pointer for pointer, definition in pointers if definition
    }
    assert {pointer for pointer, _ in named} == {
        item["pointer"] for item in result.artifacts["ledger"]["replacedBy"]
    }


def test_the_request_is_the_format_main_reads_and_names_no_value(written: Tuple[Path, Any]) -> None:
    root, _ = written
    text = (root / ("bundle" + REQUEST)).read_text(encoding="utf-8")
    assert "profile-request/1-0-0/FluxProfileRequest.json" in text
    asked = _request(root)  # the reader of format 1-0-0 takes it
    assert [table.expose_id for table in asked.tables] == ["orders"]
    assert [column.name for column in asked.tables[0].columns] == [
        "amount",
        "country",
        "created_at",
    ]
    assert asked.tables[0].row_count is False  # the profile publishes the row count


# ---------------------------------------------------------------------------
# A profile that answers it
# ---------------------------------------------------------------------------


def test_init_again_with_a_profile_that_answers_the_request_cites_the_profile(
    written: Tuple[Path, Any], tmp_path: Path
) -> None:
    root, _ = written
    before = {entry["pointer"]: entry for entry in _entries(root / "bundle")}
    answered = _write_profile(tmp_path, _answer)
    result = run_init(str(tmp_path / "again"), contract=str(ORDERS_CONTRACT), profile=str(answered))
    assert result.exit_code == 0, result.stderr
    after = {entry["pointer"]: entry for entry in _entries(tmp_path / "again")}
    first = {key: value for key, value in before.items() if "replacedBy" in value}
    # The levels of `country` are assumed as one entry, and published as an entry for each level.
    levels = next(key for key in first if key.endswith("orders/fidelity/columns/country/levels"))
    assert before[levels]["source"] == "assumption"
    published = [entry for key, entry in after.items() if key.startswith(levels + "/")]
    assert published and all(entry["source"] == "profile" for entry in published)
    wanted = before[levels]["replacedBy"]
    assert all(
        entry["statistic"]["pointer"].startswith(wanted["pointer"])
        and entry["statistic"]["definition"] == wanted["definition"]
        for entry in published
    )
    for suffix in (
        "orders/fidelity/columns/amount/nullFraction",
        "orders/fidelity/columns/created_at/nullFraction",
    ):
        pointer = next(key for key in first if key.endswith(suffix))
        assert before[pointer]["source"] == "assumption"
        assert after[pointer]["source"] == "profile", suffix
        assert after[pointer]["statistic"] == before[pointer]["replacedBy"], suffix
    # What the profile still does not answer stays an assumption, with the statistic that would.
    remaining = next(key for key in after if key.endswith("orders/fidelity/columns/amount/bins/1"))
    assert after[remaining]["source"] == "assumption" and "replacedBy" in after[remaining]
    assert result.artifacts["parameters"]["counts"]["assumption"] < (
        written[1].artifacts["parameters"]["counts"]["assumption"]
    )
    # The request of the second bundle asks for less.
    smaller = _request(tmp_path, "again")
    first = _request(root)
    assert {c.name for c in smaller.tables[0].columns} < {c.name for c in first.tables[0].columns}


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Tuple[int, str]:
    capsys.readouterr()
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_init_plan_apply_verify_and_ship_with_the_replacements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "bundle"
    # The profile with a mean statistic, so the run is scored and ships.
    assert (
        _cli(
            capsys,
            "init",
            str(bundle),
            "--contract",
            str(ORDERS_CONTRACT),
            "--profile",
            str(ORDERS_MEAN_PROFILE),
        )[0]
        == 0
    )
    state = tmp_path / "state"
    common = ["--profile", str(ORDERS_MEAN_PROFILE), "--state-dir", str(state)]
    code, out = _cli(capsys, "plan", str(bundle), *common, "--json")
    assert code == 0, out
    run_id = json.loads(out)["artifacts"]["run_id"]
    assert _cli(capsys, "apply", str(bundle), "--run-id", run_id, "--state-dir", str(state))[0] == 0
    record = json.loads((state / "runs" / run_id / "parameter-register.json").read_text("utf-8"))
    assert record["_schemaURL"].endswith("/parameter-register/1-3-0/FluxParameterRegister.json")
    assert sorted(record["counts"]) == [
        "assumption",
        "calibrated",
        "contract",
        "default",
        "overlay",
        "profile",
    ]
    assert record["counts"]["overlay"] == 0
    replaced = [entry for entry in record["entries"] if "replacedBy" in entry]
    assert replaced and all(entry["source"] == "assumption" for entry in replaced)
    code, out = _cli(capsys, "verify", str(bundle), "--run-id", run_id, *common)
    assert code == 0, out
    assert "from the overlay" not in out and "parameters: " in out
    shipped = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        profile=ORDERS_MEAN_PROFILE,
    )
    assert shipped.success, shipped.stderr
    with tarfile.open(shipped.artifacts["archive"]) as packed:
        member = next(name for name in packed.getnames() if name.endswith("/run/EVIDENCE.md"))
        extracted = packed.extractfile(member)
        assert extracted is not None
        report = extracted.read().decode("utf-8")
    assert (
        "A profile statistic, `/tables/orders/columns/country/statistics/0` "
        "(`levelHistogram/1`), would replace it."
    ) in report
    assert "from the overlay" not in report


# ---------------------------------------------------------------------------
# Refusals and overlays
# ---------------------------------------------------------------------------


def test_a_request_file_that_exists_is_never_overwritten_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    (tmp_path / ("bundle" + REQUEST)).write_text("mine", encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"), contract=str(ORDERS_CONTRACT), profile=str(ORDERS_PROFILE)
    )
    assert result.exit_code == 1 and "exists, and init never overwrites" in result.stderr
    assert not (tmp_path / "bundle").exists()
    assert (tmp_path / ("bundle" + REQUEST)).read_text(encoding="utf-8") == "mine"


def test_each_overlay_bundle_gets_its_own_request_beside_it(tmp_path: Path) -> None:
    overlay = tmp_path / "double.yml"
    overlay.write_text(
        "name: double\npatch:\n  tables:\n    orders:\n      rowCount: 1200\n", encoding="utf-8"
    )
    result = run_init(
        str(tmp_path / "bundles"),
        contract=str(ORDERS_CONTRACT),
        profile=str(ORDERS_PROFILE),
        overlays=(str(overlay),),
    )
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "bundles" / ("double" + REQUEST)).is_file()
    assert [path.name for path in (tmp_path / "bundles").iterdir() if path.is_dir()] == ["double"]
    taken = tmp_path / "more"
    taken.mkdir()
    (taken / ("double" + REQUEST)).write_text("mine", encoding="utf-8")
    refused = run_init(
        str(taken),
        contract=str(ORDERS_CONTRACT),
        profile=str(ORDERS_PROFILE),
        overlays=(str(overlay),),
    )
    assert refused.exit_code == 1 and not (taken / "double").exists()


def test_no_request_is_written_when_no_assumption_has_a_statistic(tmp_path: Path) -> None:
    # Nothing is assumed that a profile statistic would answer, so there is nothing to ask for: a
    # profile that publishes the levels, the nulls and a histogram of every column, so no number
    # of the columns is assumed, still assumes settings (tolerances, bounds), which nothing answers.
    result = run_init(
        str(tmp_path / "bundle"), contract=str(ORDERS_CONTRACT), profile=str(ORDERS_PROFILE)
    )
    assert result.exit_code == 0, result.stderr
    replaced = result.artifacts["ledger"]["replacedBy"]
    assert replaced  # this profile leaves numbers a statistic would answer
    assert (tmp_path / ("bundle" + REQUEST)).is_file()
    assert "assumptionsRequest" in result.artifacts
