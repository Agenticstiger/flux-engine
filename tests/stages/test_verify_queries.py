# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify`` counts every consumer query on the run (ADR 0028 sections 3, 4 and 7).

The bundle is ``tests/fixtures/consumers/bundle``: three queries (two calibration, one held out) and one
view over a single ``subscriptions`` expose, pinning the profile beside it. Every expected count is
worked out here from the emitted rows with plain Python, never from the engine's own counting.

* **End to end.** ``fluxsim plan``, ``apply`` and ``verify --held-out`` write the ``queries`` dimension,
  which validates against the format 1-4-0 schema, with each rate, target and requirement.
* **Apart.** The held-out targets come from ``--held-out`` only; a pinned profile that names a held-out query
  and a held-out profile that names a calibration query are each refused, naming the query.
* **Ceilings and flags.** A bundle may only lower the engine's ceilings; the flags raise them, and a value
  outside the range is refused with a named code.
* **Versions and the extra.** A bundle that recorded another sqlglot version has its queries never compared,
  and without the ``[sql]`` extra the dimension is not measured and a threshold on it fails.
"""

from __future__ import annotations

import json
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest
import yaml
from jsonschema import Draft202012Validator

pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from flux_engine.cli import main  # noqa: E402
from flux_engine.concordance.scorecard import SCHEMA_URL_1_4_0  # noqa: E402
from flux_engine.consumers import runner  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.seam.profile import load_profile  # noqa: E402
from flux_engine.stages import _queries  # noqa: E402
from flux_engine.stages.layout import RunLayout  # noqa: E402
from flux_engine.stages.run import run  # noqa: E402
from flux_engine.stages.verify import verify  # noqa: E402

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "consumers"
SCHEMA_1_4 = json.loads(
    (REPO_ROOT / "docs" / "spec" / "scorecard-1-4.schema.json").read_text(encoding="utf-8")
)
NAMESPACE = "com.agentics.flux/1"
PROFILE = FIXTURES / "subscriptions.profile.json"
HELD_OUT = FIXTURES / "subscriptions.held-out-profile.json"
COLUMNS = ["subscription_id", "plan", "region", "offered", "renewed", "renewed_at", "amount"]


def _json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class Built:
    """A copy of the fixture bundle, planned and applied, with the pinned profile it names."""

    def __init__(
        self,
        root: Path,
        *,
        consumers: Optional[Mapping[str, Any]] = None,
        extension: Optional[Mapping[str, Any]] = None,
        profile: Optional[Dict[str, Any]] = None,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.bundle = root / "bundle"
        shutil.copytree(FIXTURES / "bundle", self.bundle)
        self.profile = root / "pinned.profile.json"
        self.profile.write_text(
            json.dumps(profile or _json(PROFILE), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        path = self.bundle / "simulation.flux.yml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        namespace = document["extensions"][NAMESPACE]
        namespace["profile"] = {"digest": load_profile(self.profile).digest.value}
        for key, value in (consumers or {}).items():
            if value is None:
                namespace["consumers"].pop(key, None)  # None drops a key of the fixture's block
            else:
                namespace["consumers"][key] = value
        namespace.update(extension or {})
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        self.state = root / "state"
        applied = run(self.bundle, state_dir=self.state, profile=self.profile)
        assert applied.success, applied.stderr
        self.run_id: str = applied.artifacts["plan"]["run_id"]
        self.layout = RunLayout.for_state_dir(self.state, self.run_id)

    def rows(self) -> List[Dict[str, Any]]:
        lines = self.layout.emit_path("subscriptions").read_text(encoding="utf-8").splitlines()
        return [dict(zip(COLUMNS, json.loads(line), strict=True)) for line in lines if line]

    def verify(self, **kwargs: Any) -> Any:
        kwargs.setdefault("profile", self.profile)
        return verify(self.bundle, run_id=self.run_id, state_dir=self.state, **kwargs)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Built:
    """The fixture bundle with a tolerance on the held-out query, so that both shares exist."""
    return Built(
        tmp_path_factory.mktemp("queries"),
        consumers={"calibration": {"heldOutTolerance": "0.5"}},
    )


@pytest.fixture(scope="module")
def counted(built: Built) -> Any:
    result = built.verify(held_out=HELD_OUT)
    assert result.success, result.stderr
    return result


def queries_of(result: Any) -> Dict[str, Any]:
    return result.artifacts["queries"]


def _by_name(member: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["name"]: row for row in member["queries"]}


def _exact(value: Fraction) -> str:
    from flux_engine.concordance.compiler import render_exact

    return render_exact(value)


# ---------------------------------------------------------------- end to end


def test_plan_apply_and_verify_with_a_held_out_profile_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built = Built(tmp_path, consumers={"calibration": {"heldOutTolerance": "0.5"}})
    # `Built` planned through the stage; the command line does the same loop on a second state directory.
    state = tmp_path / "cli-state"
    common = ["--state-dir", str(state)]
    assert main(["plan", str(built.bundle), *common, "--profile", str(built.profile)]) == 0
    run_id = next(p.name for p in (state / "runs").iterdir() if (p / "plan.json").is_file())
    assert main(["apply", str(built.bundle), *common, "--run-id", run_id]) == 0
    capsys.readouterr()
    code = main(
        [
            "--json",
            "verify",
            str(built.bundle),
            *common,
            "--run-id",
            run_id,
            "--profile",
            str(built.profile),
            "--held-out",
            str(HELD_OUT),
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0, envelope
    document = envelope["artifacts"]["scorecard"]["document"]
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0
    Draft202012Validator(SCHEMA_1_4).validate(document)
    dimension = {item["dimension"]: item for item in document["dimensions"]}["queries"]
    assert dimension["status"] == "measured"
    assert dimension["queries"] == envelope["artifacts"]["queries"]
    assert [row["name"] for row in dimension["queries"]["queries"]] == [
        "premium-renewed",
        "renewed-offered",
        "renewed-since",
    ]
    # The text a person reads names both sets, the shares and the grades.
    code = main(
        [
            "verify",
            str(built.bundle),
            *common,
            "--run-id",
            run_id,
            "--profile",
            str(built.profile),
            "--held-out",
            str(HELD_OUT),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "queries: calibration" in out and "held-out" in out and "profile grade P1" in out
    assert "query premium-renewed (calibration): rate" in out


def test_every_rate_is_the_rows_over_the_units_rows_of_the_emitted_data(
    built: Built, counted: Any
) -> None:
    rows = built.rows()
    plan_rows = [r for r in rows if r["plan"] == "premium" and r["renewed"]]
    member = queries_of(counted)
    assert member["status"] == "counted"
    by_name = _by_name(member)

    premium = by_name["premium-renewed"]
    assert (premium["set"], premium["rows"], premium["unitRows"]) == (
        "calibration",
        len(plan_rows),
        len(rows),
    )
    rate = Fraction(len(plan_rows), len(rows))
    assert premium["rate"] == _exact(rate)
    assert premium["target"] == "0.15" and premium["tolerance"] == "0.02"
    assert premium["gap"] == _exact(rate - Fraction(3, 20))
    assert premium["status"] == "measured"
    assert premium["met"] is (abs(rate - Fraction(3, 20)) <= Fraction(1, 50))
    assert premium["digest"].startswith("sha256:") and len(premium["digest"]) == 71

    since = by_name["renewed-since"]
    after = [r for r in rows if r["renewed_at"] is not None and r["renewed_at"] >= "2025-03-01"]
    assert after, "the held-out query returns rows on the run"
    assert (since["set"], since["rows"], since["unitRows"]) == ("heldOut", len(after), len(rows))
    assert since["target"] == "0.6" and since["tolerance"] == "0.5"
    assert since["met"] is (abs(Fraction(len(after), len(rows)) - Fraction(3, 5)) <= Fraction(1, 2))


def test_a_grouped_query_counts_each_group_and_has_no_target_from_an_ungrouped_entry(
    built: Built, counted: Any
) -> None:
    rows = built.rows()
    offered = [r for r in rows if r["offered"]]
    grouped = _by_name(queries_of(counted))["renewed-offered"]
    assert (grouped["rows"], grouped["unitRows"]) == (
        len([r for r in offered if r["renewed"]]),
        len(offered),
    )
    levels = sorted({r["plan"] for r in offered})
    assert [g["group"] for g in grouped["groups"]] == list(range(len(levels)))
    assert not any(g["nullGroup"] for g in grouped["groups"])
    for ordinal, level in enumerate(levels):
        group = grouped["groups"][ordinal]
        here = [r for r in offered if r["plan"] == level]
        assert (group["rows"], group["unitRows"]) == (
            len([r for r in here if r["renewed"]]),
            len(here),
        )
        assert group["target"] is None and group["status"] == "noTarget"
    assert grouped["status"] == "noTarget" and grouped["met"] is None
    assert "key" not in json.dumps(grouped) and "basic" not in json.dumps(grouped)


def test_grouped_targets_come_from_a_profile_entry_grouped_by_the_column(tmp_path: Path) -> None:
    document = _json(PROFILE)
    entry = next(q for q in document["queries"] if q["name"] == "renewed-offered")
    entry["statistics"][0]["groupBy"] = "plan"
    entry["statistics"][0]["groups"] = [
        {"group": "basic", "k": 400, "levelCounts": [200, 200], "status": "F"},
        {"group": "premium", "k": 100, "levelCounts": [50, 50], "status": "F"},
        {"group": "standard", "status": "S"},
    ]
    built = Built(
        tmp_path,
        consumers={
            "queries": [
                {**q, **({"tolerance": "0.25"} if q["id"] == "renewed-offered" else {})}
                for q in yaml.safe_load((FIXTURES / "consumers.yml").read_text())["queries"]
            ]
        },
        profile=document,
    )
    result = built.verify()
    assert result.success, result.stderr
    grouped = _by_name(queries_of(result))["renewed-offered"]
    rows = [r for r in built.rows() if r["offered"]]
    levels = sorted({r["plan"] for r in rows})
    assert levels == ["basic", "premium", "standard"]
    expected_targets = {"basic": Fraction(1, 2), "premium": Fraction(1, 2), "standard": None}
    for ordinal, level in enumerate(levels):
        group = grouped["groups"][ordinal]
        here = [r for r in rows if r["plan"] == level]
        rate = Fraction(len([r for r in here if r["renewed"]]), len(here))
        target = expected_targets[level]
        assert group["rate"] == _exact(rate)
        if target is None:
            assert (group["target"], group["status"], group["met"]) == (None, "withheld", None)
        else:
            assert group["target"] == _exact(target)
            assert group["gap"] == _exact(rate - target)
            assert group["met"] is (abs(rate - target) <= Fraction(1, 4))
    assert grouped["tolerance"] == "0.25"
    decided = [g["met"] for g in grouped["groups"] if g["met"] is not None]
    assert grouped["met"] is all(decided)


def test_a_requirement_is_met_when_the_rows_that_meet_it_exist(built: Built, counted: Any) -> None:
    rows = built.rows()
    requirements = {(r["query"], r["kind"]): r for r in queries_of(counted)["requirements"]}
    level = requirements[("premium-renewed", "level")]
    assert level["status"] == ("met" if any(r["plan"] == "premium" for r in rows) else "unmet")
    assert (level["exposeId"], level["column"]) == ("subscriptions", "plan")
    # The held-out query's day is inside the run's dates, so rows meet its range.
    after = requirements[("renewed-since", "range")]
    assert after["status"] == "met" and after["met"] is True
    assert any(r["renewed_at"] is not None and r["renewed_at"] >= "2025-03-01" for r in rows)


def test_the_shares_the_score_the_versions_and_the_grades_are_stated(counted: Any) -> None:
    member = queries_of(counted)
    by_name = _by_name(member)
    calibration = [
        r for r in by_name.values() if r["set"] == "calibration" and r["met"] is not None
    ]
    within = [r for r in calibration if r["met"]]
    assert member["shares"]["calibration"]["of"] == len(calibration)
    assert member["shares"]["calibration"]["covered"] == len(within)
    assert member["shares"]["heldOut"] == {"covered": 1, "of": 1, "share": "1"}
    shares = [
        Fraction(member["shares"][key]["covered"], member["shares"][key]["of"])
        for key in ("calibration", "heldOut")
        if member["shares"][key]["of"]
    ]
    dimension = {
        item["dimension"]: item for item in counted.artifacts["scorecard"]["document"]["dimensions"]
    }["queries"]
    assert dimension["score"] == _exact(min(shares))
    assert set(member["versions"]) == {"duckdb", "sqlglot", "pyarrow"}
    assert member["versions"]["duckdb"].startswith("2.0")
    assert member["grades"] == {"pinned": "P1", "heldOut": "P1"}


def test_a_run_with_no_held_out_profile_leaves_the_held_out_query_without_a_target(
    built: Built,
) -> None:
    result = built.verify()
    since = _by_name(queries_of(result))["renewed-since"]
    assert (since["status"], since["target"], since["met"]) == ("noTarget", None, None)
    assert queries_of(result)["grades"]["heldOut"] is None
    assert queries_of(result)["shares"]["heldOut"] == {"covered": 0, "of": 0, "share": None}


def test_a_run_that_is_handed_no_profile_has_no_calibration_targets_and_no_scorecard(
    built: Built,
) -> None:
    result = built.verify(profile=None)
    assert result.artifacts["scorecard"]["status"] == "not checked"
    premium = _by_name(queries_of(result))["premium-renewed"]
    assert (premium["status"], premium["target"]) == ("noTarget", None)
    assert any(line.startswith("  queries: ") for line in result.stdout.splitlines())
    assert queries_of(result)["grades"]["pinned"] == "P1", "the plan records the grade it pinned"


def test_a_bundle_that_declares_no_consumers_has_no_queries_and_refuses_a_held_out_profile(
    tmp_path: Path,
) -> None:
    from .conftest import FIXTURE_BUNDLE

    state = tmp_path / "state"
    applied = run(FIXTURE_BUNDLE, state_dir=state)
    assert applied.success, applied.stderr
    run_id = applied.artifacts["plan"]["run_id"]
    plain = verify(FIXTURE_BUNDLE, run_id=run_id, state_dir=state)
    assert "queries" not in plain.artifacts
    refused = verify(FIXTURE_BUNDLE, run_id=run_id, state_dir=state, held_out=HELD_OUT)
    assert refused.error_code == "FLX-3702" and not refused.success


# ---------------------------------------------------------------- the two sets apart


def test_a_pinned_profile_that_names_a_held_out_query_is_refused(tmp_path: Path) -> None:
    document = _json(PROFILE)
    document["queries"].append(_json(HELD_OUT)["queries"][0])
    built = Built(tmp_path, profile=document)
    result = built.verify()
    assert (result.exit_code, result.error_code) == (1, "FLX-3701")
    assert "'renewed-since'" in result.stderr
    assert "queries" not in result.artifacts, "nothing was counted"


def test_a_held_out_profile_that_names_a_calibration_query_is_refused(
    built: Built, tmp_path: Path
) -> None:
    document = _json(HELD_OUT)
    document["queries"].append(_json(PROFILE)["queries"][0])
    path = tmp_path / "bad.held-out.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    result = built.verify(held_out=path)
    assert (result.exit_code, result.error_code) == (1, "FLX-3702")
    assert "'premium-renewed'" in result.stderr


def test_a_held_out_file_that_is_not_a_profile_is_refused_with_the_readers_code(
    built: Built, tmp_path: Path
) -> None:
    path = tmp_path / "not-a-profile.json"
    path.write_text('{"hello": 1}\n', encoding="utf-8")
    assert built.verify(held_out=path).error_code == "FLX-5036"
    assert built.verify(held_out=tmp_path / "missing.json").error_code == "FLX-3109"


# ---------------------------------------------------------------- ceilings and flags


@pytest.fixture()
def spy(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """The limits every batch is run with; the real child still counts."""
    seen: List[Any] = []
    real = runner.run_counts

    def watching(tables: Any, requests: Any, limits: Any, **kwargs: Any) -> Any:
        seen.append(limits)
        return real(tables, requests, limits, **kwargs)

    monkeypatch.setattr(runner, "run_counts", watching)
    return seen


def test_the_bundles_own_limits_are_the_limits_the_queries_run_with(
    built: Built, spy: List[Any]
) -> None:
    assert built.verify().success
    assert [(limits.seconds, limits.memory) for limits in spy] == [(30, 1 << 30)]


@pytest.fixture(scope="module")
def unlimited(tmp_path_factory: pytest.TempPathFactory) -> Built:
    """The fixture bundle with the block's ``limits`` removed: it declares none of its own."""
    return Built(tmp_path_factory.mktemp("unlimited"), consumers={"limits": None})


def test_with_no_limits_declared_the_queries_run_under_the_default_or_the_raised_ceiling(
    unlimited: Built, spy: List[Any]
) -> None:
    assert unlimited.verify().success
    assert unlimited.verify(max_query_seconds=120).success
    assert unlimited.verify(max_query_memory=2 << 30).success
    assert unlimited.verify(max_query_seconds=600, max_query_memory=16 << 30).success
    assert [(limits.seconds, limits.memory) for limits in spy] == [
        (30, 1 << 30),
        (120, 1 << 30),
        (30, 2 << 30),
        (600, 16 << 30),
    ]


def test_a_limit_the_bundle_declares_is_kept_whatever_the_flags_raise(
    tmp_path: Path, spy: List[Any]
) -> None:
    lowered = Built(tmp_path, consumers={"limits": {"seconds": 10}})
    assert lowered.verify(max_query_seconds=120, max_query_memory=4 << 30).success
    # The bundle chose its seconds; it gave no memory, so the raised ceiling is the memory.
    assert [(limits.seconds, limits.memory) for limits in spy] == [(10, 4 << 30)]


@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"max_query_seconds": 29}, "--max-query-seconds"),
        ({"max_query_seconds": 601}, "--max-query-seconds"),
        ({"max_query_seconds": 0}, "--max-query-seconds"),
        ({"max_query_memory": (1 << 30) - (1 << 20)}, "--max-query-memory"),
        ({"max_query_memory": (16 << 30) + (1 << 20)}, "--max-query-memory"),
    ],
)
def test_a_flag_outside_its_range_is_refused_with_a_named_code(
    built: Built, kwargs: Dict[str, int], flag: str, spy: List[Any]
) -> None:
    result = built.verify(**kwargs)
    assert (result.exit_code, result.error_code) == (2, "FLX-3700")
    assert flag in result.stderr
    assert spy == [], "nothing ran"


def test_a_flag_that_raises_a_ceiling_is_accepted_at_its_ends(built: Built, spy: List[Any]) -> None:
    assert built.verify(max_query_seconds=600, max_query_memory=16 << 30).success
    assert built.verify(max_query_seconds=30, max_query_memory=1 << 30).success


def test_the_command_line_reads_the_ceiling_flags(
    built: Built, capsys: pytest.CaptureFixture[str]
) -> None:
    base = [
        "verify",
        str(built.bundle),
        "--state-dir",
        str(built.state),
        "--run-id",
        built.run_id,
        "--profile",
        str(built.profile),
    ]
    assert main([*base, "--max-query-seconds", "45", "--max-query-memory", "2GiB"]) == 0
    capsys.readouterr()
    assert main([*base, "--max-query-seconds", "9999"]) == 2
    assert "FLX-3700" in capsys.readouterr().err
    with pytest.raises(SystemExit) as caught:
        main([*base, "--max-query-memory", "plenty"])
    assert caught.value.code == 2


def _variant(built: Built, root: Path, **limits: Any) -> Path:
    """A copy of *built*'s bundle whose consumers block declares *limits* (``calibration`` keys apart)."""
    bundle = root / "variant"
    shutil.copytree(built.bundle, bundle)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = document["extensions"][NAMESPACE]["consumers"]
    calibration = limits.pop("calibration", None)
    block["limits"] = limits
    if calibration is not None:
        block["calibration"] = calibration
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


@pytest.mark.parametrize(
    ("limits", "refused"),
    [
        ({"seconds": 10, "memory": "512MiB"}, None),
        ({"seconds": 30, "memory": "1GiB", "calibration": {"evaluations": 64}}, None),
        ({"seconds": 31}, "FLX-3614"),
        ({"seconds": 120}, "FLX-3614"),
        ({"memory": "2GiB"}, "FLX-3615"),
        ({"memory": "17GiB"}, "FLX-3615"),
        ({"calibration": {"evaluations": 65}}, "FLX-3616"),
    ],
)
def test_plan_and_verify_accept_and_refuse_the_same_bundles(
    built: Built, tmp_path: Path, limits: Dict[str, Any], refused: Optional[str]
) -> None:
    """A bundle's limits are read against the engine's ceilings in every stage, flags or none."""
    from flux_engine.stages.plan import plan

    bundle = _variant(built, tmp_path, **limits)
    planned = plan(bundle, state_dir=tmp_path / "state", profile=built.profile)
    assert planned.error_code == refused
    # What `verify` reads the registry with, handed the largest ceilings the flags allow.
    receipt = _json(built.layout.receipt_path)
    ceilings = _queries.query_ceilings(600, 16 << 30)
    if refused is None:
        assert (
            _queries.prepare_queries(
                bundle,
                receipt,
                _json(built.layout.plan_path),
                pinned=None,
                held_out=None,
                ceilings=ceilings,
            )
            is not None
        )
    else:
        with pytest.raises(FluxError) as caught:
            _queries.prepare_queries(
                bundle,
                receipt,
                _json(built.layout.plan_path),
                pinned=None,
                held_out=None,
                ceilings=ceilings,
            )
        assert caught.value.code.code == refused


def test_a_bundle_asking_for_more_than_the_ceiling_is_refused_whatever_the_flags_raise(
    built: Built, tmp_path: Path, spy: List[Any]
) -> None:
    """A run planned from a lawful bundle, then a bundle that asks for more: verify never reads it."""
    bundle = _variant(built, tmp_path, seconds=120)
    # The bundle differs from the run's, so verify refuses it before reading any block.
    result = verify(
        bundle,
        run_id=built.run_id,
        state_dir=built.state,
        profile=built.profile,
        max_query_seconds=120,
    )
    assert not result.success and spy == []


# ---------------------------------------------------------------- versions and the extra


def test_recorded_versions_that_differ_leave_every_query_and_requirement_never_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path, consumers={"compiledWith": {"sqlglot": "0.0.1"}})

    def refuse(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - fails the test if reached
        raise AssertionError("nothing is counted when the versions differ")

    monkeypatch.setattr(runner, "run_counts", refuse)
    result = built.verify(held_out=HELD_OUT)
    member = queries_of(result)
    assert {row["status"] for row in member["queries"]} == {"versionsDiffer"}
    assert all(row["rows"] is None and row["met"] is None for row in member["queries"])
    assert {row["status"] for row in member["requirements"]} == {"versionsDiffer"}
    assert all(row["met"] is None for row in member["requirements"])
    dimension = {
        item["dimension"]: item for item in result.artifacts["scorecard"]["document"]["dimensions"]
    }["queries"]
    assert (dimension["status"], dimension["score"]) == ("not measured", None)
    assert dimension["counts"]["refused"] == 3


def test_without_the_extra_the_dimension_is_not_measured_and_a_threshold_on_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(
        tmp_path,
        extension={
            "scorecard": {
                "thresholds": {"dimensions": {"queries": {"mustBeGreaterOrEqualTo": "0.5"}}}
            }
        },
    )
    held = built.verify(held_out=HELD_OUT)
    assert held.artifacts["queries"]["status"] == "counted"

    monkeypatch.setattr(_queries, "is_available", lambda name: name != "sqlglot")
    result = built.verify(held_out=HELD_OUT)
    member = queries_of(result)
    assert member["status"] == "not counted"
    assert member["install"] == "pip install 'flux-sim-engine[sql]'"
    document = result.artifacts["scorecard"]["document"]
    Draft202012Validator(SCHEMA_1_4).validate(document)
    dimension = {item["dimension"]: item for item in document["dimensions"]}["queries"]
    assert (dimension["status"], dimension["score"]) == ("not measured", None)
    assert result.exit_code == 1 and not result.success
    assert any("dimensions.queries" in line for line in result.stderr.splitlines())
    assert "pip install 'flux-sim-engine[sql]'" in result.stdout


def test_a_threshold_that_is_met_passes_and_one_that_is_missed_fails(tmp_path: Path) -> None:
    met = Built(
        tmp_path / "met",
        consumers={"calibration": {"heldOutTolerance": "0.5"}},
        extension={
            "scorecard": {
                "thresholds": {"dimensions": {"queries": {"mustBeGreaterOrEqualTo": "0"}}}
            }
        },
    )
    assert met.verify(held_out=HELD_OUT).success
    missed = Built(
        tmp_path / "missed",
        consumers={"calibration": {"heldOutTolerance": "0.001"}},
        extension={
            "scorecard": {"thresholds": {"dimensions": {"queries": {"mustBeGreaterThan": "0.99"}}}}
        },
    )
    result = missed.verify(held_out=HELD_OUT)
    assert result.exit_code == 1 and any(
        "dimensions.queries" in line for line in result.stderr.splitlines()
    )


def test_a_run_whose_artifacts_moved_is_not_counted(tmp_path: Path) -> None:
    built = Built(tmp_path)
    path = built.layout.emit_path("subscriptions")
    path.write_bytes(path.read_bytes() + b'["x","basic","north",false,false,null,"1"]\n')
    result = built.verify()
    member = queries_of(result)
    assert member["status"] == "not counted" and "FLX-2110" in member["detail"]
    assert result.error_code == "FLX-2110" and not result.success


def test_a_stream_that_does_not_digest_to_the_receipt_is_refused(tmp_path: Path) -> None:
    built = Built(tmp_path)
    receipt = _json(built.layout.receipt_path)
    plan = _queries.prepare_queries(
        built.bundle,
        receipt,
        _json(built.layout.plan_path),
        pinned=load_profile(built.profile),
        held_out=None,
        ceilings=_queries.query_ceilings(None, None),
    )
    assert plan is not None
    assert len(_queries._digested_streams(plan, built.layout, receipt)) == 1
    receipt["emits"]["subscriptions"]["outputDigest"] = "sha256:" + "0" * 64
    with pytest.raises(FluxError) as caught:
        _queries._digested_streams(plan, built.layout, receipt)
    assert caught.value.code.code == "FLX-3703"
    assert "subscriptions" in str(caught.value)


def test_a_query_that_cannot_be_counted_is_a_failed_leg_with_its_code(
    built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.consumers.runner_errors import FLX_7133

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise FluxError(FLX_7133, "the query process stopped answering")

    monkeypatch.setattr(runner, "run_counts", refuse)
    result = built.verify()
    assert (result.exit_code, result.error_code) == (1, "FLX-7133")
    assert queries_of(result)["status"] == "not counted"
    assert "stopped answering" in result.stderr
