# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Consumer SQL is checked when a bundle is validated and planned (ADR 0028 section 2).

The bundle is ``tests/fixtures/consumers/bundle`` with a view whose name needs quotes added to its
``consumers`` block.

* **Refused early.** A query that cannot be checked is refused by ``validate`` and by ``plan``, naming
  the query, the identifier and the checker's code, with the hint to write a name that needs quotes
  quoted. The same view quoted is accepted at plan and counted at ``verify``.
* **One reading.** ``plan`` and ``verify`` refuse the same texts with the same code and message, and
  a bundle that was checked is checked once in a process.
* **Without the extra.** ``validate`` and ``plan`` proceed and say the queries were not checked.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from flux_engine.cli import main  # noqa: E402
from flux_engine.consumers import check  # noqa: E402
from flux_engine.seam.profile import load_profile  # noqa: E402
from flux_engine.stages import _consumer_check, _queries  # noqa: E402
from flux_engine.stages.layout import RunLayout  # noqa: E402
from flux_engine.stages.plan import plan  # noqa: E402
from flux_engine.stages.run import run  # noqa: E402
from flux_engine.stages.validate import validate  # noqa: E402
from flux_engine.stages.verify import verify  # noqa: E402

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "consumers"
NAMESPACE = "com.agentics.flux/1"
PROFILE = FIXTURES / "subscriptions.profile.json"
UNQUOTED = "SELECT plan FROM not-offered WHERE renewed"
QUOTED = 'SELECT plan FROM "not-offered" WHERE renewed'
INSTALL = "pip install 'flux-sim-engine[sql]'"


def _bundle(
    root: Path,
    *,
    sql: Optional[str] = None,
    extra_queries: Tuple[Dict[str, Any], ...] = (),
) -> Tuple[Path, Path]:
    """A copy of the fixture bundle with the view ``not-offered`` and a query that reads it."""
    root.mkdir(parents=True, exist_ok=True)
    bundle = root / "bundle"
    shutil.copytree(FIXTURES / "bundle", bundle)
    profile = root / "pinned.profile.json"
    shutil.copyfile(PROFILE, profile)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    namespace = document["extensions"][NAMESPACE]
    namespace["profile"] = {"digest": load_profile(profile).digest.value}
    block = namespace["consumers"]
    queries = list(block["queries"])
    if sql is not None:
        block["views"] = [
            *block["views"],
            {"name": "not-offered", "sql": "SELECT * FROM subscriptions WHERE NOT offered"},
        ]
        queries.append(
            {"id": "renewed-not-offered", "sql": sql, "unit": "not-offered", "groupBy": "plan"}
        )
    block["queries"] = [*queries, *extra_queries]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle, profile


def _replace(bundle: Path, sql: str) -> None:
    """Make the query ``premium-renewed`` of *bundle* read *sql*."""
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for query in document["extensions"][NAMESPACE]["consumers"]["queries"]:
        if query["id"] == "premium-renewed":
            query["sql"] = sql
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------- a name that needs quotes


def test_a_hyphenated_view_used_unquoted_is_refused_at_validate_naming_the_hint(
    tmp_path: Path,
) -> None:
    bundle, _ = _bundle(tmp_path, sql=UNQUOTED)
    result = validate(bundle)
    assert result.exit_code == 1 and result.error_code in {"FLX-3618", "FLX-3624"}
    assert "renewed-not-offered" in result.stderr
    assert 'must be written quoted, for example "not-offered"' in result.stderr


def test_a_hyphenated_view_used_unquoted_is_refused_at_plan_naming_the_hint(
    tmp_path: Path,
) -> None:
    bundle, profile = _bundle(tmp_path, sql=UNQUOTED)
    result = plan(bundle, state_dir=tmp_path / "state", profile=profile)
    assert result.exit_code == 1 and result.error_code in {"FLX-3618", "FLX-3624"}
    assert "renewed-not-offered" in result.stderr
    assert 'must be written quoted, for example "not-offered"' in result.stderr
    assert not (tmp_path / "state" / "runs").exists()


def test_the_command_line_refuses_it_at_validate_and_at_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, profile = _bundle(tmp_path, sql=UNQUOTED)
    assert main(["validate", str(bundle)]) == 1
    assert '"not-offered"' in capsys.readouterr().err
    state = ["--state-dir", str(tmp_path / "state")]
    assert main(["plan", str(bundle), *state, "--profile", str(profile)]) == 1
    err = capsys.readouterr().err
    assert "renewed-not-offered" in err and '"not-offered"' in err


def test_a_text_that_has_no_such_name_gets_no_quoting_hint(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path, sql=QUOTED)
    _replace(bundle, "SELECT * FROM nowhere")
    result = validate(bundle)
    assert result.error_code == "FLX-3624" and "quoted" not in result.stderr


def test_the_same_view_quoted_is_accepted_at_plan_and_counted_at_verify(tmp_path: Path) -> None:
    bundle, profile = _bundle(tmp_path, sql=QUOTED)
    assert validate(bundle).exit_code == 0
    applied = run(bundle, state_dir=tmp_path / "state", profile=profile)
    assert applied.success, applied.stderr
    run_id = applied.artifacts["plan"]["run_id"]
    result = verify(bundle, run_id=run_id, state_dir=tmp_path / "state", profile=profile)
    assert result.success, result.stderr
    member = result.artifacts["queries"]
    assert member["status"] == "counted"
    row = next(item for item in member["queries"] if item["name"] == "renewed-not-offered")
    assert row["rows"] is not None and row["unitRows"] is not None


# ---------------------------------------------------------------- plan and verify read one text


HOSTILE = [
    "SELECT * FROM subscriptions; DROP TABLE subscriptions",
    "DELETE FROM subscriptions",
    "SELECT * FROM nowhere",
    "SELECT * FROM read_csv('x.csv')",
    "SELECT FROM WHERE",
    "SELECT * FROM subscriptions WHERE renewed_at >= :missing",
    "SELECT * FROM subscriptions LIMIT 5",
    "SELECT nonesuch FROM subscriptions",
]


@pytest.fixture(scope="module")
def applied(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, Path, Path, str]:
    """A good bundle, planned and applied, for the verify side of the comparison."""
    root = tmp_path_factory.mktemp("good")
    bundle, profile = _bundle(root, sql=QUOTED)
    result = run(bundle, state_dir=root / "state", profile=profile)
    assert result.success, result.stderr
    return bundle, profile, root / "state", result.artifacts["plan"]["run_id"]


def _verify_leg(bundle: Path, applied: Tuple[Path, Path, Path, str]) -> Any:
    """What ``verify`` makes of *bundle*'s queries, on the run of the good bundle in *applied*."""
    _, good_profile, state, run_id = applied
    layout = RunLayout.for_state_dir(state, run_id)
    receipt = json.loads(layout.receipt_path.read_text(encoding="utf-8"))
    plan_document = json.loads(layout.plan_path.read_text(encoding="utf-8"))
    prepared = _queries.prepare_queries(
        bundle,
        receipt,
        plan_document,
        pinned=load_profile(good_profile),
        held_out=None,
        ceilings=_queries.query_ceilings(None, None),
    )
    assert prepared is not None
    return _queries.count_queries(
        prepared,
        layout,
        receipt,
        plan_document,
        pinned=load_profile(good_profile),
        artifacts_ok=True,
    )


@pytest.mark.parametrize("text", HOSTILE)
def test_plan_and_verify_refuse_the_same_text_with_the_same_code(
    tmp_path: Path, applied: Tuple[Path, Path, Path, str], text: str
) -> None:
    bundle, profile = _bundle(tmp_path / "bad", sql=QUOTED)
    _replace(bundle, text)

    planned = plan(bundle, state_dir=tmp_path / "bad-state", profile=profile)
    assert planned.exit_code == 1 and planned.error_code is not None

    leg = _verify_leg(bundle, applied)
    assert leg.error is not None
    assert leg.error_code == planned.error_code
    assert leg.error.detail in planned.stderr


# ---------------------------------------------------------------- a query that doubles its own tree


def _doubling(steps: int) -> str:
    projections = ["amount AS a0"] + [f"a{i - 1} + a{i - 1} AS a{i}" for i in range(1, steps + 1)]
    return f"SELECT {', '.join(projections)} FROM subscriptions WHERE a{steps} > 0"


def test_a_query_whose_aliases_double_its_tree_is_refused_at_validate_plan_and_verify(
    tmp_path: Path, applied: Tuple[Path, Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlglot import exp

    from flux_engine.consumers import check as check_module

    doubled: List[bool] = []
    real = check_module.qualify

    def spy(tree: exp.Expr, *args: Any, **kwargs: Any) -> exp.Expr:
        doubled.append(any(a.alias == "a40" for a in tree.find_all(exp.Alias)))
        return real(tree, *args, **kwargs)

    monkeypatch.setattr(check_module, "qualify", spy)
    bundle, profile = _bundle(tmp_path / "bad", sql=QUOTED)
    _replace(bundle, _doubling(40))

    checked = validate(bundle)
    assert checked.exit_code == 1 and checked.error_code == "FLX-3713", checked.stderr
    assert "premium-renewed" in checked.stderr and "'a0'" in checked.stderr

    planned = plan(bundle, state_dir=tmp_path / "bad-state", profile=profile)
    assert planned.exit_code == 1 and planned.error_code == "FLX-3713", planned.stderr

    leg = _verify_leg(bundle, applied)
    assert leg.error is not None and leg.error_code == "FLX-3713"

    # The other queries of the fixture are qualified as usual; the doubling one never is.
    assert doubled and not any(doubled)


# ---------------------------------------------------------------- once per process


def test_a_bundle_is_checked_once_in_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, profile = _bundle(tmp_path, sql=QUOTED)
    calls: List[int] = []
    real = check.check_registry

    def counting(registry: Any, schema: Any) -> Any:
        calls.append(1)
        return real(registry, schema)

    monkeypatch.setattr(check, "check_registry", counting)
    monkeypatch.setattr(_consumer_check, "_last", None)
    assert validate(bundle).exit_code == 0
    planned = plan(bundle, state_dir=tmp_path / "state", profile=profile)
    assert planned.success, planned.stderr
    assert len(calls) == 1


# ---------------------------------------------------------------- without the extra


def test_without_the_extra_validate_and_plan_proceed_and_say_the_queries_were_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, profile = _bundle(tmp_path, sql=UNQUOTED)
    monkeypatch.setattr(_consumer_check, "is_available", lambda name: name != "sqlglot")

    checked = validate(bundle)
    assert checked.exit_code == 0, checked.stderr
    assert "consumer queries were not checked" in checked.stdout and INSTALL in checked.stdout

    planned = plan(bundle, state_dir=tmp_path / "state", profile=profile)
    assert planned.success, planned.stderr
    assert "consumer queries were not checked" in planned.stdout and INSTALL in planned.stdout
    assert planned.artifacts["consumer_queries_checked"] is False


def test_with_the_extra_a_checked_plan_says_nothing_about_it(tmp_path: Path) -> None:
    bundle, profile = _bundle(tmp_path, sql=QUOTED)
    planned = plan(bundle, state_dir=tmp_path / "state", profile=profile)
    assert planned.success, planned.stderr
    assert "not checked" not in planned.stdout
    assert "consumer_queries_checked" not in planned.artifacts
    assert "not checked" not in validate(bundle).stdout
