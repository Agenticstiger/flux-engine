# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --contract --profile``: a bundle for your own data, every number labelled.

Pinned here, through the command and a real run:

* a profile of column metrics alone gives a bundle that validates and runs
  with the profile it pins, and the ``mean/1`` ``fluid profile`` writes for a
  number column gives one that passes its check against the profile;
* every number the run reads is decided by an entry of the parameter register,
  so none is a default no entry names, and each entry's source is the one the
  rules give it: the profile, the contract, an assumption or a template default;
* the declarations the check reads (``range``, ``support``) rest only on
  published values or the contract, so a guess can never refuse real data;
* a profile with statistics gives properties the run is scored on, an enum
  the contract declares is cited from the contract, and the record is register
  format 1-3-0, since init lists the statistics no number rests on and says what would replace each
  assumed number (``replacedBy``);
* the same inputs write the same bytes;
* an input that cannot be used is refused, and nothing is written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest
import yaml

from flux_engine.author.columns import ASSUMED_CHECK_REASON
from flux_engine.cli._init import run_init
from flux_engine.evidence.register import REGISTER_SCHEMA_URL_1_3, REGISTER_SCHEMA_URL_1_4
from flux_engine.seam.parameters import parse_pointer
from flux_engine.stages.run import run
from flux_engine.stages.validate import validate

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "author"
ORDERS_CONTRACT = FIXTURES / "orders.fluid.yaml"
ORDERS_PROFILE = FIXTURES / "orders.profile.json"
#: The same profile with the ``mean/1`` statistic ``fluid profile`` writes for a number column.
ORDERS_MEAN_PROFILE = FIXTURES / "orders-mean.profile.json"
KEYED_CONTRACT = FIXTURES / "subscriber-payments.fluid.yaml"
EXAMPLE_PROFILE = REPO_ROOT / "docs" / "spec" / "profile-example.json"
EXAMPLE_PROFILE_1_3 = REPO_ROOT / "docs" / "spec" / "profile-1-3-example.json"
NAMESPACE = "com.agentics.flux/1"


def _init(directory: Path, contract: Path, profile: Path) -> Any:
    return run_init(str(directory), contract=str(contract), profile=str(profile))


def _simulation(bundle: Path) -> Dict[str, Any]:
    text = (bundle / "simulation.flux.yml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert isinstance(document, dict)
    return document


def _block(bundle: Path) -> Dict[str, Any]:
    return _simulation(bundle)["extensions"][NAMESPACE]


def _record(state: Path, run_id: str) -> Dict[str, Any]:
    text = (state / "runs" / run_id / "parameter-register.json").read_text(encoding="utf-8")
    return json.loads(text)


class Authored:
    """One bundle written by ``fluxsim init --contract --profile`` and run with its profile."""

    def __init__(self, root: Path, contract: Path, profile: Path) -> None:
        self.bundle = root / "bundle"
        self.profile = profile
        result = _init(self.bundle, contract, profile)
        assert result.exit_code == 0, result.stderr
        self.init = result
        self.state = root / "state"
        outcome = run(self.bundle, state_dir=self.state, profile=profile)
        assert outcome.success, outcome.stderr
        self.run_id: str = outcome.artifacts["plan"]["run_id"]
        self.record = _record(self.state, self.run_id)


@pytest.fixture(scope="module")
def orders(tmp_path_factory: pytest.TempPathFactory) -> Authored:
    return Authored(tmp_path_factory.mktemp("orders"), ORDERS_CONTRACT, ORDERS_PROFILE)


@pytest.fixture(scope="module")
def orders_mean(tmp_path_factory: pytest.TempPathFactory) -> Authored:
    return Authored(tmp_path_factory.mktemp("orders-mean"), ORDERS_CONTRACT, ORDERS_MEAN_PROFILE)


@pytest.fixture(scope="module")
def keyed(tmp_path_factory: pytest.TempPathFactory) -> Authored:
    return Authored(tmp_path_factory.mktemp("keyed"), KEYED_CONTRACT, EXAMPLE_PROFILE)


# ---------------------------------------------------------------------------
# Column metrics alone
# ---------------------------------------------------------------------------


def test_the_bundle_holds_the_contract_unchanged_and_three_documents(orders: Authored) -> None:
    names = sorted(path.name for path in orders.bundle.iterdir())
    assert names == [
        "orders.fluid.yaml",
        "persona.flux.yml",
        "requests",
        "simulation.flux.yml",
        "world.flux.yml",
    ]
    assert (orders.bundle / "orders.fluid.yaml").read_bytes() == ORDERS_CONTRACT.read_bytes()
    assert validate(orders.bundle).exit_code == 0


def test_the_simulation_pins_the_profile_and_emits_its_table(orders: Authored) -> None:
    from flux_engine.seam.profile import load_profile

    block = _block(orders.bundle)
    assert block["profile"] == {"digest": load_profile(ORDERS_PROFILE).digest.value}
    assert block["emits"]["orders"]["rows"] == 600
    emits = _simulation(orders.bundle)["spec"]["emits"]
    assert [emit["exposeId"] for emit in emits] == ["orders"]
    assert emits[0]["provenance"]["contractDigest"].startswith("sha256:")


def test_every_number_the_run_reads_is_decided_by_an_entry(orders: Authored) -> None:
    record = orders.record
    assert (
        record["_schemaURL"] == REGISTER_SCHEMA_URL_1_3
    )  # init writes replacedBy, and lists the unused statistics
    assert record["declared"] == sum(record["counts"].values()) > 0
    unnamed = [row["pointer"] for row in record["parameters"] if row["entry"] is None]
    assert unnamed == []
    # country declares no assumed nullFraction: an absent one draws no null (ADR 0014).
    assert record["counts"] == {
        "profile": 12,
        "contract": 0,
        "overlay": 0,
        "assumption": 17,
        "default": 3,
        "calibrated": 0,
    }


def test_published_metrics_become_cited_declarations(orders: Authored) -> None:
    columns = _block(orders.bundle)["emits"]["orders"]["fidelity"]["columns"]
    assert columns["amount"]["range"] == {"min": "10", "max": "100"}
    # The quartile bins, shaped to the published mean (sum / count): each keeps a quarter.
    assert columns["amount"]["bins"][::2] == ["10", "20", "35", "50", "100"]
    assert columns["amount"]["binWeights"] == ["0", "0.25"] * 4
    assert columns["order_id"] == {"unique": True}
    cited = {
        entry["pointer"].rsplit("/columns/", 1)[-1]: entry["statistic"]
        for entry in _block(orders.bundle)["parameters"]
        if entry["source"] == "profile" and "/columns/" in entry["pointer"]
    }
    assert cited["amount/range/min"] == {
        "pointer": "/tables/orders/columns/amount/metrics/min",
        "definition": "min/1",
    }
    assert cited["amount/bins/4"]["pointer"] == (
        "/tables/orders/columns/amount/metrics/quantiles/values/0.5"
    )
    assert "amount/bins/3" not in cited  # a cut the shape adds is an assumption


def test_what_the_profile_withholds_is_an_assumption_with_its_reason(orders: Authored) -> None:
    entries = _block(orders.bundle)["parameters"]
    assumed = {
        entry["pointer"]: entry["reason"] for entry in entries if entry["source"] == "assumption"
    }
    prefix = f"/extensions/{NAMESPACE.replace('/', '~1')}/emits/orders/fidelity/columns/"
    assert "as many opaque levels" in assumed[prefix + "country/levels"]
    assert not any(char.isdigit() for char in assumed[prefix + "country/levels"])
    assert "withheld nullCount (status A)" in assumed[prefix + "amount/nullFraction"]
    assert all(reason.strip() for reason in assumed.values())
    # A timestamp the profile gives no order statistic for is left to the run's
    # window, which is text the register does not count: the command says so.
    assert "created_at" not in _block(orders.bundle)["emits"]["orders"]["fidelity"]["columns"] or (
        "range" not in _block(orders.bundle)["emits"]["orders"]["fidelity"]["columns"]["created_at"]
    )
    assert "spec.timeBounds" in orders.init.stdout


def test_the_check_reads_only_published_or_contract_facts(
    orders: Authored, keyed: Authored
) -> None:
    for authored in (orders, keyed):
        entries = _block(authored.bundle)["parameters"]
        sources = {entry["pointer"]: entry["source"] for entry in entries}
        for expose_id, emit in _block(authored.bundle)["emits"].items():
            for name, declared in emit["fidelity"]["columns"].items():
                base = (
                    f"/extensions/{NAMESPACE.replace('/', '~1')}/emits/{expose_id}"
                    f"/fidelity/columns/{name}"
                )
                # A table the profile gives nothing to range-check holds one assumed check:
                # the span of the column's bins, said to be an assumption.
                assumed_check = declared.get("range") == {
                    "min": str(declared["bins"][0]) if "bins" in declared else None,
                    "max": str(declared["bins"][-1]) if "bins" in declared else None,
                } and (ASSUMED_CHECK_REASON in authored.init.stdout)
                if (
                    "range" in declared
                    and "T" not in str(declared["range"]["min"])
                    and not assumed_check
                ):
                    assert sources.get(base + "/range/min") == "profile"
                    assert sources.get(base + "/range/max") == "profile"
                if "support" in declared:
                    assert sources.get(base + "/maxCardinality") in ("profile", "contract")


def test_a_bundle_init_wrote_for_the_example_profile_passes_its_own_verify(
    tmp_path: Path,
) -> None:
    # paid_at publishes only min and max, so no histogram of it can be scored: init writes
    # no property over it, and the table's one check is one the profile can score.
    authored = Authored(tmp_path, KEYED_CONTRACT, EXAMPLE_PROFILE_1_3)
    payment = _block(authored.bundle)["emits"]["payment"]["fidelity"]
    assert [prop["property"] for prop in payment["properties"]] == ["mean"]
    assert "has no statistic in the profile" not in authored.init.stdout
    verified = run(
        authored.bundle,
        state_dir=tmp_path / "verified",
        profile=EXAMPLE_PROFILE_1_3,
        verify=True,
    )
    assert verified.success, verified.stdout + verified.stderr
    assert "FLX-5037" not in verified.stdout + verified.stderr


def test_a_timestamp_range_is_the_published_min_and_max(keyed: Authored) -> None:
    # A timestamp bound is RFC 3339 text, which the register does not count, so it
    # is held to the profile here instead.
    paid_at = _block(keyed.bundle)["emits"]["payment"]["fidelity"]["columns"]["paid_at"]
    metrics = json.loads(EXAMPLE_PROFILE.read_text(encoding="utf-8"))["tables"]["payment"][
        "columns"
    ]["paid_at"]["metrics"]
    from flux_engine.core.clock import format_rfc3339_ms

    assert paid_at["range"] == {
        "min": format_rfc3339_ms(int(metrics["min"]["value"])),
        "max": format_rfc3339_ms(int(metrics["max"]["value"])),
    }
    window = _simulation(keyed.bundle)["spec"]["timeBounds"]
    assert window == {"start": paid_at["range"]["min"], "end": paid_at["range"]["max"]}


def test_the_report_names_what_came_from_where_and_the_next_command(orders: Authored) -> None:
    out = orders.init.stdout
    assert (
        "parameters: 32 declared -- 12 from the pinned profile, 0 from the contract, 17 assumed, "
        "3 default" in out
    )
    assert "country, amount, created_at: The profile withheld nullCount (status A)" in out
    # The metrics give no statistic a property can be scored on, so the property is left out.
    assert "left out 1 property" in out and "marginalHistogram of amount (orders)" in out
    assert "FLX-5037" in out
    assert out.rstrip().endswith(f"run {orders.bundle} --profile {ORDERS_PROFILE}")
    artifacts = orders.init.artifacts
    assert artifacts["files"] == [
        "orders.fluid.yaml",
        "world.flux.yml",
        "persona.flux.yml",
        "simulation.flux.yml",
    ]
    assert artifacts["parameters"]["counts"] == orders.record["counts"]
    assert [expose["exposeId"] for expose in artifacts["exposes"]] == ["orders"]


def test_the_same_inputs_write_the_same_bytes(tmp_path: Path, orders: Authored) -> None:
    again = tmp_path / "again"
    assert _init(again, ORDERS_CONTRACT, ORDERS_PROFILE).exit_code == 0
    for path in orders.bundle.iterdir():
        if path.is_dir():
            continue
        assert (again / path.name).read_bytes() == path.read_bytes(), path.name


# ---------------------------------------------------------------------------
# A profile with statistics, and a contract with keys and an enum
# ---------------------------------------------------------------------------


def test_statistics_give_properties_the_run_is_scored_on(keyed: Authored) -> None:
    from flux_engine.stages.verify import verify

    fidelity = _block(keyed.bundle)["emits"]["subscriber"]["fidelity"]
    kinds = sorted(prop["property"] for prop in fidelity["properties"])
    assert kinds == ["levelHistogram", "marginalHistogram", "mean"]
    outcome = verify(
        keyed.bundle, run_id=keyed.run_id, state_dir=keyed.state, profile=keyed.profile
    )
    assert "fidelity subscriber: passed -- 3 of 3 declared properties" in outcome.stdout


def test_an_enum_the_contract_declares_is_cited_from_the_contract(keyed: Authored) -> None:
    market = _block(keyed.bundle)["emits"]["subscriber"]["fidelity"]["columns"]["market"]
    assert market["support"] == "declared"
    assert market["maxCardinality"] == 4
    assert sorted(market["levels"]) == ["DE", "FR", "GB", "IE"]
    cited = [entry for entry in _block(keyed.bundle)["parameters"] if entry["source"] == "contract"]
    assert cited == [
        {
            "document": "sim-telco.silver.subscriber_payments",
            "pointer": (
                f"/extensions/{NAMESPACE.replace('/', '~1')}/emits/subscriber/fidelity"
                "/columns/market/maxCardinality"
            ),
            "source": "contract",
            "contract": {
                "productRef": "telco.silver.subscriber_payments",
                "pointer": "/exposes/0/contract/schema/1/type",
            },
        }
    ]
    contract = yaml.safe_load(KEYED_CONTRACT.read_text(encoding="utf-8"))
    node: Any = contract
    for token in parse_pointer(cited[0]["contract"]["pointer"]):
        node = node[int(token)] if isinstance(node, list) else node[token]
    assert node == "enum('DE','FR','GB','IE')"
    assert keyed.record["_schemaURL"] == REGISTER_SCHEMA_URL_1_4
    assert keyed.record["counts"]["contract"] == 1


def test_a_key_the_contract_declares_is_not_restated(keyed: Authored) -> None:
    columns = _block(keyed.bundle)["emits"]["subscriber"]["fidelity"]["columns"]
    assert "subscriber_id" not in columns  # an identifier the engine derives from the contract
    assert all("unique" not in declared for declared in columns.values())


def test_withheld_cells_are_half_the_minimum_cell(keyed: Authored) -> None:
    tenure = _block(keyed.bundle)["emits"]["subscriber"]["fidelity"]["columns"]["tenure_months"]
    # profile-example.json withholds the last bin's count (A); ACRO's minimum cell is 10.
    # PROFILE-SPEC section 7: k less the published cells does not count a withheld one.
    # The bins are also shaped to the published mean: every count is kept, in order.
    assert [weight for weight in tenure["binWeights"] if weight != "0"] == [
        "305",
        "260",
        "465",
        "5",
    ]
    assert set(["0", "12", "24", "48", "120"]) <= set(tenure["bins"])
    assert (tenure["bins"][0], tenure["bins"][-1]) == ("0", "120")


# ---------------------------------------------------------------------------
# Refusals: nothing is written
# ---------------------------------------------------------------------------


def test_a_profile_without_a_contract_is_a_usage_error(tmp_path: Path) -> None:
    result = run_init(str(tmp_path / "b"), profile=str(ORDERS_PROFILE))
    assert result.exit_code == 2
    assert "--profile needs --contract" in result.stderr
    assert not (tmp_path / "b").exists()


def test_a_directory_that_holds_anything_is_refused(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine", encoding="utf-8")
    result = _init(tmp_path, ORDERS_CONTRACT, ORDERS_PROFILE)
    assert result.exit_code == 1
    assert "never overwrites" in result.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == ["keep.txt"]


def test_a_profile_that_cannot_be_read_says_so(tmp_path: Path) -> None:
    result = _init(tmp_path / "b", ORDERS_CONTRACT, tmp_path / "missing.profile.json")
    assert result.exit_code == 1
    assert "missing.profile.json could not be read: No such file or directory" in result.stderr
    assert "do not pair" not in result.stderr
    assert not (tmp_path / "b").exists()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (None, "cannot read the contract"),
        (b"\xff\xfe not text", "is not UTF-8 text"),
        (b"a: [unclosed", "is not YAML"),
        (b"a: bell \x07\n", "is not YAML"),
        (b"just: a mapping", "holds 0 FLUID contracts"),
        # The read's bounds hold a contract as they hold a bundle's documents.
        (b"x: " + b"[" * 70 + b"]" * 70 + b"\n", "nests deeper than 64 levels"),
        (b"x: " + b"[" * 2000 + b"]" * 2000 + b"\n", "nests too deeply to read"),
        (b"r: &r [*r]\n", "holds itself through an alias"),
    ],
)
def test_a_contract_that_cannot_be_used_is_refused_plainly(
    tmp_path: Path, data: Any, message: str
) -> None:
    contract = tmp_path / "c.fluid.yaml"
    if data is not None:
        contract.write_bytes(data)
    result = _init(tmp_path / "b", contract, ORDERS_PROFILE)
    assert result.exit_code == 1
    assert message in result.stderr
    assert not (tmp_path / "b").exists()


def test_a_file_name_that_is_not_plain_text_cannot_end_the_header_comment(
    tmp_path: Path,
) -> None:
    odd = tmp_path / "x\nname: planted\n#.fluid.yaml"
    shutil.copyfile(ORDERS_CONTRACT, odd)
    result = _init(tmp_path / "b", odd, ORDERS_PROFILE)
    assert result.exit_code == 0, result.stderr
    for name in ("simulation.flux.yml", "world.flux.yml", "persona.flux.yml"):
        lines = (tmp_path / "b" / name).read_text(encoding="utf-8").splitlines()
        assert not any(line.startswith("name: planted") for line in lines), name
    assert not any(line.startswith("name: planted") for line in result.stdout.splitlines())


def test_an_integer_key_spans_the_foreign_keys_of_its_children(tmp_path: Path) -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "shop.sales",
        "name": "Sales",
        "metadata": {"owner": {"team": "shop"}},
        "exposes": [
            {
                "exposeId": "customer",
                "kind": "table",
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "c"}},
                "contract": {"schema": [{"name": "customer_sk", "type": "INT"}]},
                "semantics": {
                    "entities": [{"name": "customer", "type": "primary", "expr": "customer_sk"}]
                },
            },
            {
                "exposeId": "sale",
                "kind": "table",
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "s"}},
                "contract": {"schema": [{"name": "customer_sk", "type": "INT"}]},
                "semantics": {
                    "relationships": [
                        {
                            "name": "sale_to_customer",
                            "to": "customer",
                            "fromColumns": ["customer_sk"],
                            "toColumns": ["customer_sk"],
                            "cardinality": "many_to_one",
                            "joinType": "inner",
                        }
                    ]
                },
            },
        ],
    }
    path = tmp_path / "sales.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    amount = profile["tables"]["orders"]["columns"]["amount"]["metrics"]
    fk = {name: dict(amount[name]) for name in ("min", "max", "quantiles", "count")}
    fk["min"]["value"], fk["max"]["value"] = "5001", "5400"
    fk["quantiles"]["values"] = {"0.25": "5100", "0.5": "5200", "0.75": "5300"}
    profile["tables"] = {
        "customer": {"rowCount": 300, "columns": {"customer_sk": {"domain": "number"}}},
        "sale": {
            "rowCount": 600,
            "columns": {"customer_sk": {"domain": "number", "metrics": fk}},
        },
    }
    profiled = tmp_path / "sales.profile.json"
    profiled.write_text(json.dumps(profile), encoding="utf-8")
    result = _init(tmp_path / "b", path, profiled)
    assert result.exit_code == 0, result.stderr
    emits = _block(tmp_path / "b")["emits"]
    key = emits["customer"]["fidelity"]["columns"]["customer_sk"]
    assert key["bins"] == ["5001", "5400"]  # the children's span, which holds 300 values
    assert "nullFraction" not in key
    foreign = emits["sale"]["fidelity"]["columns"]["customer_sk"]
    assert "nullFraction" not in foreign  # an inner join references a parent on every row
    assert foreign["range"] == {"min": "5001", "max": "5400"}


def test_a_document_that_is_not_a_profile_is_flx_5036(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"tables": {}}), encoding="utf-8")
    result = _init(tmp_path / "b", ORDERS_CONTRACT, other)
    assert result.exit_code == 1
    assert result.error_code == "FLX-5036"


def test_a_profile_of_other_tables_names_both_sides(tmp_path: Path) -> None:
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    profile["tables"] = {"shipments": profile["tables"]["orders"]}
    other = tmp_path / "shipments.profile.json"
    other.write_text(json.dumps(profile), encoding="utf-8")
    result = _init(tmp_path / "b", ORDERS_CONTRACT, other)
    assert result.exit_code == 1
    assert "orders: the profile has no table for it" in result.stderr
    assert "The profile describes: shipments" in result.stderr
    assert not (tmp_path / "b").exists()


def test_a_contract_the_engine_refuses_writes_nothing(tmp_path: Path) -> None:
    contract = yaml.safe_load(ORDERS_CONTRACT.read_text(encoding="utf-8"))
    del contract["exposes"][0]["binding"]
    broken = tmp_path / "broken.fluid.yaml"
    broken.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    result = _init(tmp_path / "b", broken, ORDERS_PROFILE)
    assert result.exit_code == 1
    assert (
        "does not validate" in result.stderr and "'binding' is a required property" in result.stderr
    )
    assert not (tmp_path / "b").exists()


def test_a_contract_file_with_another_name_is_renamed_for_the_loader(tmp_path: Path) -> None:
    plain = tmp_path / "orders-contract.yaml"
    shutil.copyfile(ORDERS_CONTRACT, plain)
    result = _init(tmp_path / "b", plain, ORDERS_PROFILE)
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "b" / "orders-contract.fluid.yaml").read_bytes() == plain.read_bytes()


def _assumption_subjects(result: Any) -> List[str]:
    return [
        item["subject"]
        for expose in result.artifacts["exposes"]
        for item in expose.get("assumptions", [])
    ]


def test_the_json_envelope_lists_assumptions_by_subject(orders: Authored) -> None:
    subjects = _assumption_subjects(orders.init)
    assert subjects.count("country") == 2  # the opaque levels and the null count
    notes: List[Mapping[str, str]] = orders.init.artifacts["notes"]
    assert [note["subject"] for note in notes] == ["time window"]


def test_a_table_with_nothing_published_is_written_with_no_property(tmp_path: Path) -> None:
    contract = yaml.safe_load(ORDERS_CONTRACT.read_text(encoding="utf-8"))
    contract["exposes"][0]["contract"]["schema"] = [
        {"name": "reason_sk", "type": "int", "required": False},
        {"name": "reason_desc", "type": "varchar", "required": False},
    ]
    small = tmp_path / "reasons.fluid.yaml"
    small.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    withheld = {
        "definition": "min/1",
        "status": "A",
        "disclosure": "acro-default",
        "sampleSize": {"min": 35, "max": 35},
    }
    published = {
        "status": "F",
        "disclosure": "acro-default",
        "sampleSize": {"min": 35, "max": 35},
    }
    profile["tables"]["orders"] = {
        "rowCount": 35,
        "columns": {
            # No order statistic: only the mean (sum / count) and how many values.
            "reason_sk": {
                "domain": "number",
                "metrics": {
                    "min": withheld,
                    "sum": dict(published, definition="sum/1", value="630"),
                    "count": dict(published, definition="count/1", value=35),
                    "distinctCount": dict(published, definition="distinctCount/1", value=35),
                },
            },
            "reason_desc": {"domain": "category", "metrics": {}},
        },
    }
    profiled = tmp_path / "reasons.profile.json"
    profiled.write_text(json.dumps(profile), encoding="utf-8")
    result = _init(tmp_path / "b", small, profiled)
    assert result.exit_code == 0, result.stderr
    fidelity = _block(tmp_path / "b")["emits"]["orders"]["fidelity"]
    # The profile holds no mean/1 to score a check against, so the table writes none (FLX-5037),
    # and a range no statistic gives is not made up to hang one on.
    assert fidelity["properties"] == []
    assert "range" not in fidelity["columns"]["reason_sk"]
    assert "nothing to verify" in result.stdout


def test_a_profile_with_a_mean_gives_a_run_that_passes_against_it(orders_mean: Authored) -> None:
    """The whole path with no hand edit: the bundle, the run, and its check against the profile."""
    from flux_engine.stages.verify import verify

    fidelity = _block(orders_mean.bundle)["emits"]["orders"]["fidelity"]
    assert [prop["property"] for prop in fidelity["properties"]] == ["mean"]
    assert "checks:" not in orders_mean.init.stdout  # every property can be scored
    outcome = verify(
        orders_mean.bundle,
        run_id=orders_mean.run_id,
        state_dir=orders_mean.state,
        profile=orders_mean.profile,
    )
    assert "fidelity orders: passed -- 1 of 1 declared property" in outcome.stdout
    assert "scorecard: overall" in outcome.stdout


# ---------------------------------------------------------------------------
# Small integers, booleans, keys and what a plan can carry
# ---------------------------------------------------------------------------


def _orders_contract(tmp_path: Path, schema: List[Dict[str, Any]]) -> Path:
    contract = yaml.safe_load(ORDERS_CONTRACT.read_text(encoding="utf-8"))
    contract["exposes"][0]["contract"]["schema"] = schema
    path = tmp_path / "orders.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _published(definition: str, value: Any, rows: int) -> Dict[str, Any]:
    key = "values" if definition == "quantiles/1" else "value"
    return {
        "definition": definition,
        "status": "F",
        "disclosure": "acro-default",
        "sampleSize": {"min": rows, "max": rows},
        key: value,
    }


def _orders_profile(tmp_path: Path, rows: int, columns: Dict[str, Any]) -> Path:
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    profile["tables"] = {"orders": {"rowCount": rows, "columns": columns}}
    path = tmp_path / "orders.profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


def test_a_small_integer_mean_and_boolean_counts_pass_their_own_check(tmp_path: Path) -> None:
    """Shaped on the grid the generator draws on, a mean of 4.5 over whole numbers holds."""
    from flux_engine.stages.verify import verify

    rows = 2000
    contract = _orders_contract(
        tmp_path,
        [
            {"name": "items", "type": "int", "required": True},
            {"name": "gift", "type": "boolean", "required": True},
        ],
    )
    items = {
        "domain": "number",
        "metrics": {
            "count": _published("count/1", rows, rows),
            "min": _published("min/1", "1", rows),
            "max": _published("max/1", "20", rows),
            "quantiles": _published("quantiles/1", {"0.25": "2", "0.5": "3", "0.75": "5"}, rows),
            "sum": _published("sum/1", "9000", rows),
        },
        "statistics": [
            {
                "definition": "mean/1",
                "disclosure": "acro-default",
                "sampleSize": {"min": rows, "max": rows},
                "groups": [{"group": "", "k": rows, "Sx": str(9000 * 2**16), "status": "F"}],
            }
        ],
    }
    gift = {
        "domain": "category",
        "statistics": [
            {
                "definition": "levelHistogram/1",
                "levels": ["false", "true"],
                "sampleSize": {"min": rows, "max": rows},
                "disclosure": "acro-default",
                "groups": [
                    {
                        "group": "",
                        "k": rows,
                        "levelCounts": [1500, 500],
                        "cellStatus": ["F", "F"],
                        "status": "F",
                    }
                ],
            }
        ],
    }
    authored = Authored(
        tmp_path, contract, _orders_profile(tmp_path, rows, {"items": items, "gift": gift})
    )
    columns = _block(authored.bundle)["emits"]["orders"]["fidelity"]["columns"]
    assert columns["gift"] == {
        "levels": {"false": "0.75", "true": "0.25"},
        "allocation": "exact",  # the table is small
    }
    outcome = verify(
        authored.bundle,
        run_id=authored.run_id,
        state_dir=authored.state,
        profile=authored.profile,
    )
    assert "fidelity orders: passed -- 1 of 1 declared property" in outcome.stdout, outcome.stdout


def test_an_integer_a_row_cannot_carry_still_gives_a_bundle_that_runs(tmp_path: Path) -> None:
    contract = _orders_contract(
        tmp_path,
        [
            {"name": "event_id", "type": "bigint", "required": True},
            {"name": "amount", "type": "int", "required": True},
        ],
    )
    rows = 600
    columns = {
        "event_id": {
            "domain": "number",
            "metrics": {
                "min": _published("min/1", "1", rows),
                "max": _published("max/1", "20000000000000000", rows),
            },
        },
        "amount": {
            "domain": "number",
            "metrics": {
                "min": _published("min/1", "1", rows),
                "max": _published("max/1", "9", rows),
            },
        },
    }
    profile = _orders_profile(tmp_path, rows, columns)
    result = _init(tmp_path / "b", contract, profile)
    assert result.exit_code == 0, result.stderr
    declared = _block(tmp_path / "b")["emits"]["orders"]["fidelity"]["columns"]
    assert declared["event_id"] == {
        "bins": ["0", "99"],
        "binWeights": ["1"],
        "allocation": "exact",  # the table is small
    }
    assert "2^53" in result.stdout
    # Drawn as the profile says, a value past 2^53 is refused when the rows are written.
    outcome = run(tmp_path / "b", state_dir=tmp_path / "state", profile=profile)
    assert outcome.success, outcome.stderr


def _chain_contract(tmp_path: Path) -> Path:
    def expose(expose_id: str, schema: List[str], **semantics: Any) -> Dict[str, Any]:
        return {
            "exposeId": expose_id,
            "kind": "table",
            "binding": {"platform": "local", "format": "parquet", "location": {"path": expose_id}},
            "contract": {"schema": [{"name": name, "type": "INT"} for name in schema]},
            "semantics": semantics,
        }

    def link(name: str, to: str, source: str, target: str, cardinality: str) -> Dict[str, Any]:
        return {
            "name": name,
            "to": to,
            "fromColumns": [source],
            "toColumns": [target],
            "cardinality": cardinality,
            "joinType": "left",
        }

    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "shop.chain",
        "name": "Chain",
        "metadata": {"owner": {"team": "shop"}},
        "exposes": [
            expose("a", ["id"], entities=[{"name": "a", "type": "primary", "expr": "id"}]),
            expose(
                "b",
                ["a_id"],
                entities=[{"name": "b", "type": "primary", "expr": "a_id"}],
                relationships=[link("b_to_a", "a", "a_id", "id", "one_to_one")],
            ),
            expose(
                "c",
                ["b_id", "a_ref"],
                relationships=[
                    link("c_to_b", "b", "b_id", "a_id", "many_to_one"),
                    link("c_to_a", "a", "a_ref", "id", "many_to_one"),
                ],
            ),
        ],
    }
    path = tmp_path / "chain.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def test_key_spans_carry_down_a_chain_and_hold_every_foreign_key(tmp_path: Path) -> None:
    rows = 50
    b_id = {
        "domain": "number",
        "metrics": {
            "min": _published("min/1", "1", rows),
            "max": _published("max/1", "1000", rows),
        },
    }
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    profile["tables"] = {
        "a": {"rowCount": 10, "columns": {"id": {"domain": "number"}}},
        "b": {"rowCount": 10, "columns": {"a_id": {"domain": "number"}}},
        "c": {
            "rowCount": rows,
            "columns": {"b_id": b_id, "a_ref": {"domain": "number"}},
        },
    }
    profiled = tmp_path / "chain.profile.json"
    profiled.write_text(json.dumps(profile), encoding="utf-8")
    result = _init(tmp_path / "bundle", _chain_contract(tmp_path), profiled)
    assert result.exit_code == 0, result.stderr
    emits = _block(tmp_path / "bundle")["emits"]
    # c.b_id spans 1 to 1000, so b.a_id does, and so does a.id, which b.a_id references.
    assert emits["b"]["fidelity"]["columns"]["a_id"]["bins"] == ["1", "1000"]
    assert emits["a"]["fidelity"]["columns"]["id"]["bins"] == ["1", "1000"]
    # A foreign key the profile says nothing of draws over the key it references, not 0.
    assert emits["c"]["fidelity"]["columns"]["a_ref"]["bins"] == ["1", "1000"]


def test_an_assumed_key_under_a_published_key_draws_inside_it(tmp_path: Path) -> None:
    """b.a_id is b's key and references a.id, whose min and max are published."""
    contract = yaml.safe_load(_chain_contract(tmp_path).read_text(encoding="utf-8"))
    path = tmp_path / "rooted.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    rows = 10
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    a_id = {
        "domain": "number",
        "metrics": {
            "min": _published("min/1", "100", rows),
            "max": _published("max/1", "109", rows),
        },
    }
    profile["tables"] = {
        "a": {"rowCount": rows, "columns": {"id": a_id}},
        "b": {"rowCount": rows, "columns": {"a_id": {"domain": "number"}}},
        "c": {
            "rowCount": 40,
            "columns": {"b_id": {"domain": "number"}, "a_ref": {"domain": "number"}},
        },
    }
    profiled = tmp_path / "rooted.profile.json"
    profiled.write_text(json.dumps(profile), encoding="utf-8")
    result = _init(tmp_path / "bundle", path, profiled)
    assert result.exit_code == 0, result.stderr
    emits = _block(tmp_path / "bundle")["emits"]
    assert emits["a"]["fidelity"]["columns"]["id"]["bins"] == ["100", "109"]
    # b.a_id moves into a.id's span, and c.b_id, which says nothing, follows it there.
    assert emits["b"]["fidelity"]["columns"]["a_id"]["bins"] == ["100", "109"]
    assert emits["c"]["fidelity"]["columns"]["b_id"]["bins"] == ["100", "109"]
    assert emits["c"]["fidelity"]["columns"]["a_ref"]["bins"] == ["100", "109"]


def test_the_profile_is_read_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin and the citations come from the same read, so a pipe works as a file does."""
    import flux_engine.author.bundle as bundle_module

    reads: List[Any] = []
    real = bundle_module.load_profile

    def counted(path: Any) -> Any:
        reads.append(path)
        return real(path)

    monkeypatch.setattr(bundle_module, "load_profile", counted)
    result = _init(tmp_path / "b", ORDERS_CONTRACT, ORDERS_MEAN_PROFILE)
    assert result.exit_code == 0, result.stderr
    assert len(reads) == 1


def test_a_text_foreign_key_into_a_number_key_is_refused_for_its_type(tmp_path: Path) -> None:
    contract = yaml.safe_load(_chain_contract(tmp_path).read_text(encoding="utf-8"))
    # c.a_ref becomes text, with level names that read as numbers. A foreign key holds
    # its parent's values, so `plan` refuses the pair, and init says why without
    # reading the level names as numbers on the way.
    contract["exposes"][2]["contract"]["schema"][1]["type"] = "STRING"
    path = tmp_path / "text-fk.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    rows = 40
    levels = {
        "definition": "levelHistogram/1",
        "levels": ["1E+999999", "7"],
        "sampleSize": {"min": rows, "max": rows},
        "disclosure": "acro-default",
        "groups": [
            {
                "group": "",
                "k": rows,
                "levelCounts": [20, 20],
                "cellStatus": ["F", "F"],
                "status": "F",
            }
        ],
    }
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    profile["tables"] = {
        "a": {"rowCount": 10, "columns": {"id": {"domain": "number"}}},
        "b": {"rowCount": 10, "columns": {"a_id": {"domain": "number"}}},
        "c": {
            "rowCount": rows,
            "columns": {
                "b_id": {"domain": "number"},
                "a_ref": {"domain": "category", "statistics": [levels]},
            },
        },
    }
    profiled = tmp_path / "text-fk.profile.json"
    profiled.write_text(json.dumps(profile), encoding="utf-8")
    result = _init(tmp_path / "bundle", path, profiled)
    assert result.exit_code == 1
    assert "FLX-3403" in result.stderr and "declare both columns with one type" in result.stderr
    assert "beyond what a bundle can declare" not in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "bundle").exists()


def _keys_contract(tmp_path: Path, tables: Dict[str, Dict[str, Any]]) -> Path:
    """A contract of INT columns: per table its columns, its primary key and its links."""
    exposes = []
    for expose_id, table in tables.items():
        semantics: Dict[str, Any] = {}
        if table.get("key"):
            semantics["entities"] = [{"name": expose_id, "type": "primary", "expr": table["key"]}]
        if table.get("links"):
            semantics["relationships"] = [
                {
                    "name": f"{expose_id}_{source}_to_{target}",
                    "to": target,
                    "fromColumns": [source],
                    "toColumns": [column],
                    "cardinality": cardinality,
                    "joinType": "left",
                }
                for source, target, column, cardinality in table["links"]
            ]
        exposes.append(
            {
                "exposeId": expose_id,
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": expose_id},
                },
                "contract": {
                    "schema": [{"name": name, "type": "INT"} for name in table["columns"]]
                },
                "semantics": semantics,
            }
        )
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "shop.keys",
        "name": "Keys",
        "metadata": {"owner": {"team": "shop"}},
        "exposes": exposes,
    }
    path = tmp_path / "keys.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _keys_profile(tmp_path: Path, tables: Dict[str, Dict[str, Any]]) -> Path:
    """Per table its row count and, per column, the min, max and histogram it publishes."""
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    profile["tables"] = {}
    for expose_id, table in tables.items():
        rows = table["rows"]
        columns: Dict[str, Any] = {}
        for name in table["columns"]:
            published = table.get("published", {}).get(name, {})
            entry: Dict[str, Any] = {"domain": "number", "metrics": {}}
            for metric in ("min", "max"):
                if metric in published:
                    entry["metrics"][metric] = _published(f"{metric}/1", published[metric], rows)
            if "bins" in published:
                entry["statistics"] = [
                    {
                        "definition": "marginalHistogram/1",
                        "bins": published["bins"],
                        "sampleSize": {"min": rows, "max": rows},
                        "disclosure": "acro-default",
                        "groups": [
                            {
                                "group": "",
                                "k": rows,
                                "binCounts": published["counts"],
                                "status": "F",
                            }
                        ],
                    }
                ]
            columns[name] = entry
        profile["tables"][expose_id] = {"rowCount": rows, "columns": columns}
    path = tmp_path / "keys.profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


def _spans(tmp_path: Path, tables: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result = _init(
        tmp_path / "bundle", _keys_contract(tmp_path, tables), _keys_profile(tmp_path, tables)
    )
    assert result.exit_code == 0, result.stderr
    return {
        expose_id: emit["fidelity"]["columns"]
        for expose_id, emit in _block(tmp_path / "bundle")["emits"].items()
    }


def test_a_foreign_key_follows_where_its_parent_draws_not_its_checked_range(tmp_path: Path) -> None:
    spans = _spans(
        tmp_path,
        {
            "a": {
                "columns": ["id"],
                "rows": 95,
                "published": {
                    "id": {
                        "min": "1",
                        "max": "100",
                        "bins": ["0", "50", "100"],
                        "counts": [45, 50],
                    }
                },
            },
            "c": {"columns": ["x"], "rows": 600, "links": [("x", "a", "id", "many_to_one")]},
        },
    )
    assert spans["a"]["id"]["bins"] == ["1", "50", "100"]
    assert spans["a"]["id"]["range"] == {"min": "0", "max": "100"}
    assert spans["c"]["x"]["bins"] == ["1", "100"]  # never the 0 a.id does not draw


def test_a_published_key_s_span_passes_down_a_chain_of_assumed_keys(tmp_path: Path) -> None:
    spans = _spans(
        tmp_path,
        {
            "a": {
                "columns": ["id"],
                "key": "id",
                "rows": 10,
                "published": {"id": {"min": "100", "max": "109"}},
            },
            "b": {
                "columns": ["a_id"],
                "key": "a_id",
                "rows": 10,
                "links": [("a_id", "a", "id", "one_to_one")],
            },
            "c": {
                "columns": ["b_id"],
                "key": "b_id",
                "rows": 10,
                "links": [("b_id", "b", "a_id", "one_to_one")],
            },
            "d": {"columns": ["c_id"], "rows": 40, "links": [("c_id", "c", "b_id", "many_to_one")]},
        },
    )
    for expose_id, column in (("b", "a_id"), ("c", "b_id"), ("d", "c_id")):
        assert spans[expose_id][column]["bins"] == ["100", "109"], (expose_id, spans[expose_id])
    # And the rows a run draws: every foreign key value is one a.id can hold.
    outcome = run(
        tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / "keys.profile.json"
    )
    assert outcome.success, outcome.stderr
    emits = tmp_path / "state" / "runs" / outcome.artifacts["plan"]["run_id"] / "emits"
    for expose_id in ("b", "c", "d"):
        lines = (emits / f"{expose_id}.ndjson").read_text(encoding="utf-8").splitlines()
        values = {json.loads(line)[0] for line in lines}
        assert values and values <= set(range(100, 110)), (expose_id, sorted(values))


def test_a_foreign_key_with_two_parents_keeps_to_the_first(tmp_path: Path) -> None:
    # `plan` lets two relationships set one column when the first parent references the
    # second on the same column, so a.id lies inside b.id here.
    tables = {
        "a": {
            "columns": ["id"],
            "key": "id",
            "rows": 5,
            "links": [("id", "b", "id", "one_to_one")],
            "published": {"id": {"min": "105", "max": "109"}},
        },
        "b": {
            "columns": ["id"],
            "key": "id",
            "rows": 10,
            "published": {"id": {"min": "100", "max": "109"}},
        },
        "c": {
            "columns": ["x"],
            "rows": 40,
            "links": [("x", "a", "id", "many_to_one"), ("x", "b", "id", "many_to_one")],
        },
    }
    assert _spans(tmp_path, tables)["c"]["x"]["bins"] == ["105", "109"]


def test_a_composite_foreign_key_takes_its_nulls_from_its_first_column(tmp_path: Path) -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "shop.composite",
        "name": "Composite",
        "metadata": {"owner": {"team": "shop"}},
        "exposes": [
            {
                "exposeId": "a",
                "kind": "table",
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "a"}},
                "contract": {"schema": [{"name": n, "type": "INT"} for n in ("k1", "k2", "size")]},
            },
            {
                "exposeId": "b",
                "kind": "table",
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "b"}},
                "contract": {"schema": [{"name": n, "type": "INT"} for n in ("x1", "x2", "n")]},
                "semantics": {
                    "relationships": [
                        {
                            "name": "b_to_a",
                            "to": "a",
                            "fromColumns": ["x1", "x2"],
                            "toColumns": ["k1", "k2"],
                            "cardinality": "many_to_one",
                            "joinType": "left",
                        }
                    ]
                },
            },
        ],
    }
    path = tmp_path / "composite.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    sized = {
        "domain": "number",
        "metrics": {"min": _published("min/1", "1", 30), "max": _published("max/1", "9", 30)},
    }
    profile = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    profile["tables"] = {
        "a": {
            "rowCount": 30,
            "columns": {"k1": {"domain": "number"}, "k2": {"domain": "number"}, "size": sized},
        },
        "b": {
            "rowCount": 60,
            "columns": {"x1": {"domain": "number"}, "x2": {"domain": "number"}, "n": sized},
        },
    }
    profiled = tmp_path / "composite.profile.json"
    profiled.write_text(json.dumps(profile), encoding="utf-8")
    result = _init(tmp_path / "bundle", path, profiled)
    assert result.exit_code == 0, result.stderr
    emits = _block(tmp_path / "bundle")["emits"]
    child = emits["b"]["fidelity"]["columns"]
    assert "nullFraction" in child["x1"] and "nullFraction" not in child["x2"]
    # The key the relationship targets spans its rows in each of its columns.
    parent = emits["a"]["fidelity"]["columns"]
    assert parent["k1"]["bins"] == ["1", "30"] and parent["k2"]["bins"] == ["1", "30"]
    assert "nullFraction" not in parent["k1"]


def test_a_decimal_finer_than_a_bundle_declares_is_left_out_at_once(tmp_path: Path) -> None:
    import time

    contract = _orders_contract(
        tmp_path,
        [
            {"name": "fine", "type": "decimal(200000,100000)", "required": True},
            {"name": "items", "type": "int", "required": True},
        ],
    )
    rows = 600
    sized = {
        "domain": "number",
        "metrics": {
            "min": _published("min/1", "0", rows),
            "max": _published("max/1", "40", rows),
        },
    }
    start = time.monotonic()
    result = _init(
        tmp_path / "b", contract, _orders_profile(tmp_path, rows, {"fine": sized, "items": sized})
    )
    assert time.monotonic() - start < 30
    assert result.exit_code == 1  # the one expose is left out, and said to be
    assert "has 100000 decimal places, more than the 38 a bundle declares" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_refusal_quoting_the_contract_prints_no_control_character(tmp_path: Path) -> None:
    hostile = "x\x1b[31mEVIL\nFAKE"
    contract = yaml.safe_load(ORDERS_CONTRACT.read_text(encoding="utf-8"))
    contract["exposes"][0]["contract"]["schema"] = [
        {"name": hostile, "type": "decimal(38,28)", "required": True}
    ]
    path = tmp_path / "hostile.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    rows = 600
    histogram = {
        "definition": "marginalHistogram/1",
        "bins": ["0", "10", "20", "30", "40"],
        "sampleSize": {"min": rows, "max": rows},
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": rows, "binCounts": [150, 150, 150, 150], "status": "F"}],
    }
    profile = _orders_profile(
        tmp_path, rows, {hostile: {"domain": "number", "statistics": [histogram]}}
    )
    result = _init(tmp_path / "b", path, profile)
    assert result.exit_code == 1
    assert "\x1b" not in result.stderr and "\n" not in result.stderr.strip()


def test_a_foreign_key_into_a_table_left_out_is_refused_before_a_write(tmp_path: Path) -> None:
    # The profile describes the child only, so the parent is left out. `plan` relates the
    # tables a contract declares and refuses the foreign key, and so do `validate` and the check.
    tables = {
        "a": {"columns": ["id"], "key": "id", "rows": 10},
        "c": {
            "columns": ["x", "n"],
            "rows": 40,
            "links": [("x", "a", "id", "many_to_one")],
            "published": {"n": {"min": "1", "max": "9"}},
        },
    }
    contract = _keys_contract(tmp_path, tables)
    result = _init(tmp_path / "b", contract, _keys_profile(tmp_path, {"c": tables["c"]}))
    assert result.exit_code == 1
    assert "the bundle written from these inputs" in result.stderr
    assert "FLX-3403" in result.stderr and "fidelity block" in result.stderr
    assert not (tmp_path / "b").exists()


def test_a_contract_rule_the_run_refuses_is_refused_before_a_write(tmp_path: Path) -> None:
    # A derive whose percent band needs violating rows is FLX-6210 at `fluxsim run`. `validate`
    # and the pre-flight both plan the contract's rules, so init refuses it before a write.
    rows = 2000
    path = _orders_contract(
        tmp_path,
        [
            {"name": "items", "type": "int", "required": True},
            {"name": "more", "type": "int", "required": True},
        ],
    )
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    contract["fluidVersion"] = "0.7.7"  # the first version with rules
    contract["exposes"][0]["contract"]["invariants"] = [
        {
            "id": "more_of_items",
            "derive": "more",
            "expr": "items + 1",
            "mustBeLessOrEqualTo": 5,
            "unit": "percent",
        }
    ]
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    number = {
        "domain": "number",
        "metrics": {
            "count": _published("count/1", rows, rows),
            "min": _published("min/1", "1", rows),
            "max": _published("max/1", "20", rows),
            "quantiles": _published("quantiles/1", {"0.25": "2", "0.5": "3", "0.75": "5"}, rows),
        },
    }
    profile = _orders_profile(tmp_path, rows, {"items": number, "more": number})
    result = _init(tmp_path / "b", path, profile)
    assert result.exit_code == 1
    assert "FLX-6210" in result.stderr
    assert not (tmp_path / "b").exists()
