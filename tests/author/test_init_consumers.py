# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --consumers``: what the consumers' SQL needs, declared and reported (ADR 0028).

Pinned here, through the command and a real run on ``tests/fixtures/consumers``:

* the Simulation embeds the block with ``compiledWith`` (the sqlglot version) beside it;
* a level a query filters on, on a column whose levels the profile gives, becomes a declared level
  with its share, and its register entry is an assumption that names the query; a level the profile
  withholds then appears in the rows of a run;
* a level outside a contract's ``enum`` is unmet, a pattern is met on a categorical column and left to
  ``verify`` on an identifier, a sentinel needs a link, nulls need a null fraction, a range needs a
  declared range or the time window;
* nothing is refused for an unmet requirement, and a hostile or malformed file writes nothing.
"""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

pytest.importorskip("sqlglot")

from flux_engine.cli._init import run_init  # noqa: E402
from flux_engine.consumers.check import SQLGLOT_VERSION  # noqa: E402
from flux_engine.seam.resolve import declared_consumers  # noqa: E402
from flux_engine.stages.run import run  # noqa: E402

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "consumers"
CONTRACT = FIXTURE / "subscriptions.fluid.yaml"
PROFILE = FIXTURE / "subscriptions.profile.json"
CONSUMERS = FIXTURE / "consumers.yml"
NAMESPACE = "com.agentics.flux/1"


def _plain_plan(tmp_path: Path, listed: Optional[Dict[str, int]] = None) -> Dict[str, Path]:
    """The fixture with ``plan`` a plain STRING whose histogram lists only *listed* (of 1,200 rows)."""
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    for column in contract["exposes"][0]["contract"]["schema"]:
        if column["name"] == "plan":
            column["type"] = "STRING"
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    statistic = profile["tables"]["subscriptions"]["columns"]["plan"]["statistics"][0]
    listed = listed if listed is not None else {"basic": 600, "standard": 360}
    statistic["levels"] = sorted(listed)
    statistic["groups"][0]["levelCounts"] = [listed[name] for name in sorted(listed)]
    contract_path = tmp_path / "subscriptions.fluid.yaml"
    profile_path = tmp_path / "subscriptions.profile.json"
    contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    return {"contract": contract_path, "profile": profile_path}


def _consumers(tmp_path: Path, queries: List[Dict[str, Any]], **block: Any) -> Path:
    path = tmp_path / "consumers.yml"
    path.write_text(
        yaml.safe_dump({"queries": queries, **block}, sort_keys=False), encoding="utf-8"
    )
    return path


def _query(name: str, sql: str, **more: Any) -> Dict[str, Any]:
    return {"id": name, "sql": sql, "unit": "subscriptions", **more}


def _init(
    tmp_path: Path,
    consumers: Path,
    *,
    contract: Path = CONTRACT,
    profile: Path = PROFILE,
    name: str = "bundle",
) -> Any:
    return run_init(
        str(tmp_path / name), contract=str(contract), profile=str(profile), consumers=str(consumers)
    )


def _block(bundle: Path) -> Dict[str, Any]:
    simulation = yaml.safe_load((bundle / "simulation.flux.yml").read_text(encoding="utf-8"))
    return simulation["extensions"][NAMESPACE]


def _status(result: Any, query: str, column: str) -> str:
    for item in result.artifacts["consumers"]["requirements"]:
        if item["query"] == query and item["column"] == column:
            return str(item["status"])
    raise AssertionError(f"no requirement of {query} on {column}")


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


def test_the_fixture_block_is_embedded_with_the_version_that_read_it(tmp_path: Path) -> None:
    result = _init(tmp_path, CONSUMERS)
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path / "bundle")
    declared = block["consumers"]
    assert declared["compiledWith"] == {"sqlglot": SQLGLOT_VERSION}
    assert {key: declared[key] for key in declared if key != "compiledWith"} == yaml.safe_load(
        CONSUMERS.read_text(encoding="utf-8")
    )
    simulation = yaml.safe_load((tmp_path / "bundle" / "simulation.flux.yml").read_text("utf-8"))
    registry = declared_consumers(simulation, where="simulation")
    assert registry is not None and registry.compiled_sqlglot == SQLGLOT_VERSION


def test_the_report_says_which_requirements_are_met(tmp_path: Path) -> None:
    result = _init(tmp_path, CONSUMERS)
    assert result.exit_code == 0, result.stderr
    assert (
        f"consumers: 3 queries read with sqlglot {SQLGLOT_VERSION}, 2 requirements -- 2 met, "
        "0 unmet, 0 left to verify"
    ) in result.stdout
    assert (
        "  met       premium-renewed: subscriptions.plan needs the level 'premium'" in result.stdout
    )
    assert "  met       renewed-since: subscriptions.renewed_at needs the range '>=2025-03-01'" in (
        result.stdout
    )
    document = result.artifacts["consumers"]
    assert document["counts"] == {"met": 2, "unmet": 0, "verify": 0}
    assert document["sqlglot"] == SQLGLOT_VERSION


def _after_the_window(tmp_path: Path) -> Path:
    """The fixture's consumers file with the held-out query's day moved past the run's dates."""
    path = tmp_path / "late.consumers.yml"
    path.write_text(
        CONSUMERS.read_text(encoding="utf-8").replace("2025-03-01", "2026-01-01"), encoding="utf-8"
    )
    return path


def test_a_range_past_the_runs_dates_is_unmet(tmp_path: Path) -> None:
    result = _init(tmp_path, _after_the_window(tmp_path))
    assert result.exit_code == 0, result.stderr
    assert "2 requirements -- 1 met, 1 unmet, 0 left to verify" in result.stdout
    assert "  unmet     renewed-since: subscriptions.renewed_at needs the range '>=2026-01-01'" in (
        result.stdout
    )
    assert result.artifacts["consumers"]["counts"] == {"met": 1, "unmet": 1, "verify": 0}


def test_an_unmet_requirement_refuses_nothing_and_the_bundle_runs(tmp_path: Path) -> None:
    result = _init(tmp_path, _after_the_window(tmp_path))
    assert result.exit_code == 0, result.stderr
    outcome = run(tmp_path / "bundle", state_dir=tmp_path / "state", profile=PROFILE)
    assert outcome.success, outcome.stderr


def test_the_bundle_without_the_file_is_the_bundle_it_was(tmp_path: Path) -> None:
    plain = run_init(str(tmp_path / "plain"), contract=str(CONTRACT), profile=str(PROFILE))
    asked = _init(tmp_path, CONSUMERS, name="asked")
    assert plain.exit_code == 0 and asked.exit_code == 0
    for name in ("world.flux.yml", "persona.flux.yml", "subscriptions.fluid.yaml"):
        assert (tmp_path / "plain" / name).read_bytes() == (tmp_path / "asked" / name).read_bytes()
    assert "consumers" not in _block(tmp_path / "plain")
    assert "consumers:" not in plain.stdout
    assert "consumers" not in plain.artifacts


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


def _rows(state: Path, outcome: Any) -> List[Dict[str, Any]]:
    run_dir = state / "runs" / outcome.artifacts["plan"]["run_id"]
    attestation = json.loads(
        (run_dir / "seam-attestation-subscriptions.json").read_text(encoding="utf-8")
    )
    lines = (run_dir / "emits" / "subscriptions.ndjson").read_text(encoding="utf-8").splitlines()
    return [
        dict(zip(attestation["columns"], json.loads(line), strict=True)) for line in lines if line
    ]


def test_a_level_the_profile_withholds_is_declared_with_its_share_and_appears_in_rows(
    tmp_path: Path,
) -> None:
    files = _plain_plan(tmp_path)
    queries = [_query("premium-renewed", "SELECT * FROM subscriptions WHERE plan = 'premium'")]
    consumers = _consumers(tmp_path, queries)

    # Without the file the profile's two levels are all the column draws.
    plain = run_init(
        str(tmp_path / "plain"), contract=str(files["contract"]), profile=str(files["profile"])
    )
    assert plain.exit_code == 0, plain.stderr
    before = _block(tmp_path / "plain")["emits"]["subscriptions"]["fidelity"]["columns"]["plan"]
    assert set(before["levels"]) == {"basic", "standard"}
    outcome = run(tmp_path / "plain", state_dir=tmp_path / "state-plain", profile=files["profile"])
    assert outcome.success, outcome.stderr
    assert {row["plan"] for row in _rows(tmp_path / "state-plain", outcome)} == {
        "basic",
        "standard",
    }

    result = _init(tmp_path, consumers, contract=files["contract"], profile=files["profile"])
    assert result.exit_code == 0, result.stderr
    namespace = _block(tmp_path / "bundle")
    plan = namespace["emits"]["subscriptions"]["fidelity"]["columns"]["plan"]
    # 1,200 records, 960 published: premium takes the 240 the published counts leave.
    assert plan["levels"] == {"basic": "0.5", "premium": "0.2", "standard": "0.3"}
    assert plan["maxCardinality"] == 3
    assert plan["support"] == {"values": ["basic", "standard", "premium"]}
    assert _status(result, "premium-renewed", "plan") == "met"

    entry = next(
        item
        for item in namespace["parameters"]
        if item["pointer"].endswith("/columns/plan/levels/premium")
    )
    assert entry["source"] == "assumption"
    assert "'premium-renewed'" in entry["reason"] and "filters on this level" in entry["reason"]
    cardinality = next(
        item
        for item in namespace["parameters"]
        if item["pointer"].endswith("/columns/plan/maxCardinality")
    )
    assert cardinality["source"] == "assumption" and "'premium-renewed'" in cardinality["reason"]
    published = next(
        item
        for item in namespace["parameters"]
        if item["pointer"].endswith("/columns/plan/levels/basic")
    )
    assert published["source"] == "profile"

    outcome = run(tmp_path / "bundle", state_dir=tmp_path / "state", profile=files["profile"])
    assert outcome.success, outcome.stderr
    seen = {row["plan"] for row in _rows(tmp_path / "state", outcome)}
    assert seen == {"basic", "premium", "standard"}


def test_levels_the_profile_does_not_list_share_the_remainder_never_zero(tmp_path: Path) -> None:
    files = _plain_plan(tmp_path)
    queries = [
        _query("a", "SELECT * FROM subscriptions WHERE plan IN ('premium', 'platinum')"),
        _query("b", "SELECT * FROM subscriptions WHERE plan = 'platinum'"),
        _query("c", "SELECT * FROM subscriptions WHERE plan = 'basic'"),
    ]
    result = _init(
        tmp_path,
        _consumers(tmp_path, queries),
        contract=files["contract"],
        profile=files["profile"],
    )
    assert result.exit_code == 0, result.stderr
    namespace = _block(tmp_path / "bundle")
    plan = namespace["emits"]["subscriptions"]["fidelity"]["columns"]["plan"]
    # 240 records are left for two levels the profile does not list: 120 each, 10 percent.
    assert plan["levels"] == {
        "basic": "0.5",
        "platinum": "0.1",
        "premium": "0.1",
        "standard": "0.3",
    }
    assert Decimal(sum(Decimal(share) for share in plan["levels"].values())) == 1
    reasons = {
        item["pointer"].rsplit("/", 1)[1]: item["reason"]
        for item in namespace["parameters"]
        if "/columns/plan/levels/" in item["pointer"] and item["source"] == "assumption"
    }
    assert set(reasons) == {"platinum", "premium"}
    assert "'a', 'b'" in reasons["platinum"] and "'a'" in reasons["premium"]
    assert "'b'" not in reasons["premium"]
    assert all(_status(result, name, "plan") == "met" for name in ("a", "b", "c"))


def test_when_the_published_counts_leave_nothing_a_level_still_has_a_share(tmp_path: Path) -> None:
    files = _plain_plan(tmp_path, {"basic": 600, "standard": 600})
    queries = [_query("q", "SELECT * FROM subscriptions WHERE plan = 'premium'")]
    result = _init(
        tmp_path,
        _consumers(tmp_path, queries),
        contract=files["contract"],
        profile=files["profile"],
    )
    assert result.exit_code == 0, result.stderr
    plan = _block(tmp_path / "bundle")["emits"]["subscriptions"]["fidelity"]["columns"]["plan"]
    assert Decimal(plan["levels"]["premium"]) > 0


def test_a_level_the_contracts_enum_declares_and_the_profile_withholds_names_the_query(
    tmp_path: Path,
) -> None:
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    statistic = profile["tables"]["subscriptions"]["columns"]["plan"]["statistics"][0]
    statistic["levels"], statistic["groups"][0]["levelCounts"] = ["basic", "standard"], [600, 360]
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    consumers = _consumers(
        tmp_path, [_query("q", "SELECT * FROM subscriptions WHERE plan = 'premium'")]
    )
    result = _init(tmp_path, consumers, profile=path)
    assert result.exit_code == 0, result.stderr
    namespace = _block(tmp_path / "bundle")
    entry = next(
        item
        for item in namespace["parameters"]
        if item["pointer"].endswith("/columns/plan/levels/premium")
    )
    assert entry["source"] == "assumption" and "'q'" in entry["reason"]
    assert _status(result, "q", "plan") == "met"


def test_a_level_outside_the_contracts_enum_is_unmet_and_not_declared(tmp_path: Path) -> None:
    consumers = _consumers(
        tmp_path, [_query("q", "SELECT * FROM subscriptions WHERE plan = 'platinum'")]
    )
    result = _init(tmp_path, consumers)
    assert result.exit_code == 0, result.stderr
    assert _status(result, "q", "plan") == "unmet"
    assert "the contract's enum does not list the level" in result.stdout
    plan = _block(tmp_path / "bundle")["emits"]["subscriptions"]["fidelity"]["columns"]["plan"]
    assert set(plan["levels"]) == {"basic", "premium", "standard"}


def test_a_boolean_level_is_met_by_the_declared_levels(tmp_path: Path) -> None:
    consumers = _consumers(
        tmp_path, [_query("q", "SELECT * FROM subscriptions WHERE renewed = FALSE")]
    )
    result = _init(tmp_path, consumers)
    assert _status(result, "q", "renewed") == "met"


def test_a_level_on_an_identifier_column_is_unmet(tmp_path: Path) -> None:
    consumers = _consumers(
        tmp_path, [_query("q", "SELECT * FROM subscriptions WHERE subscription_id = 'S-1'")]
    )
    result = _init(tmp_path, consumers)
    assert _status(result, "q", "subscription_id") == "unmet"


# ---------------------------------------------------------------------------
# Patterns, values, nulls and ranges
# ---------------------------------------------------------------------------


def test_a_pattern_is_met_on_a_categorical_column_and_left_to_verify_on_an_identifier(
    tmp_path: Path,
) -> None:
    queries = [
        _query("cat", "SELECT * FROM subscriptions WHERE plan LIKE 'pre%'"),
        _query("none", "SELECT * FROM subscriptions WHERE plan LIKE 'gold%'"),
        _query("case", "SELECT * FROM subscriptions WHERE region ILIKE 'NORTH'"),
        _query("ident", "SELECT * FROM subscriptions WHERE subscription_id LIKE 'S-%'"),
    ]
    result = _init(tmp_path, _consumers(tmp_path, queries))
    assert result.exit_code == 0, result.stderr
    assert _status(result, "cat", "plan") == "met"
    assert _status(result, "none", "plan") == "unmet"
    assert _status(result, "case", "region") == "met"
    assert _status(result, "ident", "subscription_id") == "verify"
    assert (
        "  to verify ident: subscriptions.subscription_id needs values LIKE 'S-%'" in result.stdout
    )


def test_a_value_needs_a_declaration_that_produces_it(tmp_path: Path) -> None:
    queries = [_query("q", "SELECT * FROM subscriptions WHERE renewed_at = DATE '9999-12-31'")]
    result = _init(tmp_path, _consumers(tmp_path, queries))
    assert _status(result, "q", "renewed_at") == "unmet"
    assert "links[].sentinels" in result.stdout


def test_nulls_are_met_when_the_profile_counts_them(tmp_path: Path) -> None:
    queries = [_query("q", "SELECT * FROM subscriptions WHERE renewed_at IS NULL")]
    consumers = _consumers(tmp_path, queries)
    result = _init(tmp_path, consumers)
    assert _status(result, "q", "renewed_at") == "unmet"

    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    metrics = profile["tables"]["subscriptions"]["columns"]["renewed_at"]["metrics"]
    metrics["nullCount"]["value"] = 60
    path = tmp_path / "nulls.profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    result = _init(tmp_path, consumers, profile=path, name="with-nulls")
    assert result.exit_code == 0, result.stderr
    assert _status(result, "q", "renewed_at") == "met"
    column = _block(tmp_path / "with-nulls")["emits"]["subscriptions"]["fidelity"]["columns"]
    assert Decimal(column["renewed_at"]["nullFraction"]) > 0


def test_a_range_is_met_by_the_columns_range_and_not_by_another(tmp_path: Path) -> None:
    queries = [
        _query("inside", "SELECT * FROM subscriptions WHERE renewed_at >= DATE '2025-03-01'"),
        _query("after", "SELECT * FROM subscriptions WHERE renewed_at > DATE '2025-07-01'"),
        _query("amounts", "SELECT * FROM subscriptions WHERE amount BETWEEN 10 AND 20"),
        _query("none", "SELECT * FROM subscriptions WHERE amount > 500"),
    ]
    result = _init(tmp_path, _consumers(tmp_path, queries))
    assert result.exit_code == 0, result.stderr
    assert _status(result, "inside", "renewed_at") == "met"
    assert _status(result, "after", "renewed_at") == "unmet"
    assert _status(result, "amounts", "amount") == "met"
    assert _status(result, "none", "amount") == "unmet"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("queries: [", "FLX-3637"),
        ("- a\n- b\n", "FLX-3637"),
        ("---\nqueries: []\n---\nqueries: []\n", "FLX-3637"),
        ("queries:\n- {id: q, sql: DROP TABLE subscriptions}\n", "FLX-3620"),
        ("queries:\n- {id: q, sql: 'SELECT 1; SELECT 2'}\n", "FLX-3619"),
        ("queries:\n- {id: q, sql: SELECT * FROM nowhere}\n", "FLX-3624"),
        ("queries:\n- {id: q, sql: \"SELECT * FROM read_csv('x')\"}\n", "FLX-3623"),
        ("other: 1\n", "FLX-3601"),
        ("compiledWith: {sqlglot: 1}\n", "FLX-3601"),
    ],
)
def test_a_file_that_cannot_be_used_writes_nothing(tmp_path: Path, text: str, code: str) -> None:
    path = tmp_path / "consumers.yml"
    path.write_text(text, encoding="utf-8")
    result = _init(tmp_path, path)
    assert result.exit_code == 1
    assert code in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_a_missing_file_is_refused_with_its_code(tmp_path: Path) -> None:
    result = _init(tmp_path, tmp_path / "missing.yml")
    assert result.exit_code == 1 and "FLX-3637" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_a_name_printed_back_carries_no_control_character(tmp_path: Path) -> None:
    queries = [_query("q", "SELECT * FROM subscriptions WHERE plan LIKE 'a\u001b[31m%'")]
    result = _init(tmp_path, _consumers(tmp_path, queries))
    assert result.exit_code == 0, result.stderr
    assert "\u001b" not in result.stdout


def test_consumers_without_a_contract_is_a_usage_error(tmp_path: Path) -> None:
    result = run_init(str(tmp_path / "x"), consumers=str(CONSUMERS))
    assert result.exit_code == 2 and "--consumers needs --contract" in result.stderr


def test_without_the_sql_extra_the_command_says_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sqlglot", None)
    result = _init(tmp_path, CONSUMERS)
    assert result.exit_code == 1
    assert "FLX-3501" in result.stderr and "pip install 'flux-sim-engine[sql]'" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_the_same_inputs_write_the_same_bytes(tmp_path: Path) -> None:
    first = _init(tmp_path, CONSUMERS, name="one")
    second = _init(tmp_path, CONSUMERS, name="two")
    assert first.exit_code == 0 and second.exit_code == 0
    for name in ("simulation.flux.yml", "world.flux.yml", "persona.flux.yml"):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()


# ---------------------------------------------------------------------------
# The first step: no profile yet
# ---------------------------------------------------------------------------


def test_with_no_profile_the_file_is_checked_and_the_request_is_still_written(
    tmp_path: Path,
) -> None:
    result = run_init(str(tmp_path / "b"), contract=str(CONTRACT), consumers=str(CONSUMERS))
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "b.profile-request.yml").is_file()
    assert (
        f"consumers: 3 queries read with sqlglot {SQLGLOT_VERSION}, 2 requirements" in result.stdout
    )
    assert result.artifacts["next"][-1].endswith(f"--consumers {CONSUMERS}")


def test_with_no_profile_a_hostile_file_writes_no_request(tmp_path: Path) -> None:
    path = tmp_path / "consumers.yml"
    path.write_text("queries:\n- {id: q, sql: DROP TABLE subscriptions}\n", encoding="utf-8")
    result = run_init(str(tmp_path / "b"), contract=str(CONTRACT), consumers=str(path))
    assert result.exit_code == 1 and "FLX-3620" in result.stderr
    assert not (tmp_path / "b.profile-request.yml").exists()


def test_the_fixture_registry_is_untouched_by_reading(tmp_path: Path) -> None:
    before = copy.deepcopy(yaml.safe_load(CONSUMERS.read_text(encoding="utf-8")))
    _init(tmp_path, CONSUMERS)
    assert yaml.safe_load(CONSUMERS.read_text(encoding="utf-8")) == before


# ---------------------------------------------------------------------------
# A column drawn given another
# ---------------------------------------------------------------------------


def test_a_level_added_to_a_column_drawn_given_another_is_drawn_in_each_group(
    tmp_path: Path,
) -> None:
    from .test_given import _contract, _profile

    contract = _contract().replace("type: \"enum('gold','silver')\"", "type: STRING")
    (tmp_path / "shop.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(_profile()), encoding="utf-8")
    consumers = tmp_path / "consumers.yml"
    consumers.write_text(
        "queries:\n- {id: bronze, sql: \"SELECT * FROM customer WHERE tier = 'bronze'\"}\n",
        encoding="utf-8",
    )
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "shop.fluid.yaml"),
        profile=str(tmp_path / "shop.profile.json"),
        consumers=str(consumers),
    )
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    tier = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]["customer"]["fidelity"][
        "columns"
    ]["tier"]
    assert tier["given"] == "segment"
    assert tier["support"]["values"] == ["gold", "silver", "bronze"]
    for weights in tier["levelWeightsGiven"].values():
        assert Decimal(weights["bronze"]) > 0
    outcome = run(
        tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / "shop.profile.json"
    )
    assert outcome.success, outcome.stderr
