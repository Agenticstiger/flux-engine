# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes a catalogue and the rows that follow it (ADR 0023 section 5).

A 26-row catalogue of products, each with a category, is published whole under ``references``, and
the order lines are counted by their size (``small`` or ``large``) grouped by the category of the
product they name. The profile is of format 1-3-0. Pinned here, through the command and a real run:

* the catalogue is written as ``reference: true`` and nothing else, and the lines' foreign key
  declares the catalogue keys it counts;
* the size is drawn ``given: product.category`` with the grouped counts as its weights, and the
  property it is scored on is grouped by the same link;
* the bundle plans and runs on two seeds, and its run passes its own profile's properties;
* each grouping the generator would refuse is listed as unused with its reason, and the bundle
  still runs;
* a profile of format 1-2-0 writes the bundle it wrote before.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run
from flux_engine.stages.verify import verify
from flux_stats.profile import SCHEMA_URL, SCHEMA_URL_1_3_0

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
LINES = 1200
CATEGORIES = {"tools": 10, "paint": 8, "garden": 5, "lighting": 3}
LARGE = {"tools": "0.8", "paint": "0.3", "garden": "0.5", "lighting": "0.1"}
BAND = {"min": 1000, "max": 9999}

_BINDING = (
    "    binding:\n      platform: aws\n      format: glue_table\n"
    "      location: {{ database: shop, table: {name}, region: eu-west-1 }}\n"
)


def contract_text(*, line_extra: str = "", foreign: str = "required: true") -> str:
    return f"""\
fluidVersion: "0.7.7"
kind: DataProduct
id: shop.catalogue
name: Catalogue
metadata:
  owner:
    team: growth-lab
exposes:
  - exposeId: product
    kind: table
{_BINDING.format(name="product")}    contract:
      schema:
        - {{ name: sku, type: STRING, required: true }}
        - {{ name: category, type: STRING, required: true }}
        - {{ name: price, type: "DECIMAL(10,2)", required: true }}
    semantics:
      entities:
        - {{ name: product, type: primary, expr: sku }}
  - exposeId: line
    kind: table
{_BINDING.format(name="line")}    contract:
      schema:
        - {{ name: l_id, type: STRING, required: true }}
        - {{ name: l_sku, type: STRING, {foreign} }}
        - {{ name: l_size, type: STRING, required: false }}
{line_extra}    semantics:
      entities:
        - {{ name: line, type: primary, expr: l_id }}
      relationships:
        - name: product
          to: product
          fromColumns: [l_sku]
          toColumns: [sku]
          cardinality: many_to_one
"""


def _skus() -> List[str]:
    return [f"P{number:02d}" for number in range(1, 27)]


def _category_of() -> Dict[str, str]:
    names = [name for name, count in CATEGORIES.items() for _ in range(count)]
    return dict(zip(_skus(), names, strict=True))


def _counts_per_sku() -> Dict[str, int]:
    weights = {sku: 1 + position % 3 for position, sku in enumerate(_skus())}
    total = sum(weights.values())
    counts = {sku: LINES * weight // total for sku, weight in weights.items()}
    counts[_skus()[0]] += LINES - sum(counts.values())
    return counts


def _histogram(
    levels: List[str], counts: List[int], *, link: Optional[Dict[str, Any]] = None
) -> Any:
    return {
        "definition": "levelHistogram/1",
        "levels": levels,
        "sampleSize": BAND,
        "disclosure": "none",
        "groups": [
            {"group": "", "k": sum(counts), "levelCounts": counts, "status": "F"},
        ],
    }


def profile_document(*, link_column: str = "category") -> Dict[str, Any]:
    """A profile of format 1-3-0: the catalogue published whole, and the lines counted."""
    category_of = _category_of()
    per_sku = _counts_per_sku()
    per_category: Dict[str, int] = {}
    for sku, count in per_sku.items():
        per_category[category_of[sku]] = per_category.get(category_of[sku], 0) + count
    large = {name: round(Fraction(LARGE[name]) * total) for name, total in per_category.items()}
    sizes = ["large", "small"]
    grouped = {
        "definition": "levelHistogram/1",
        "levels": sizes,
        "sampleSize": BAND,
        "disclosure": "none",
        "groupByLink": {
            "parent": {"table": "product", "columns": ["sku"]},
            "columns": ["l_sku"],
            "column": link_column,
        },
        "groups": [
            {
                "group": name,
                "k": total,
                "levelCounts": [large[name], total - large[name]],
                "status": "F",
            }
            for name, total in sorted(per_category.items())
        ],
    }
    return {
        "_producer": "urn:example:catalogue-profiler",
        "_schemaURL": SCHEMA_URL_1_3_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "line": {
                "rowCount": LINES,
                "columns": {
                    "l_id": {"domain": "category"},
                    "l_sku": {
                        "domain": "category",
                        "statistics": [
                            _histogram(_skus(), [per_sku[sku] for sku in _skus()]),
                        ],
                    },
                    "l_size": {
                        "domain": "category",
                        "statistics": [
                            _histogram(sizes, [sum(large.values()), LINES - sum(large.values())]),
                            grouped,
                        ],
                    },
                },
            }
        },
        "references": {
            "product": {
                "disclosure": "none",
                "columns": ["sku", "category", "price"],
                "rows": [
                    [sku, category_of[sku], f"{5 + position}.5"]
                    for position, sku in enumerate(_skus())
                ],
            }
        },
    }


class Authored:
    """A bundle written by ``fluxsim init`` from the catalogue profile, and its first run."""

    def __init__(
        self,
        root: Path,
        document: Optional[Dict[str, Any]] = None,
        contract: Optional[str] = None,
    ) -> None:
        self.root = root
        self.document = document or profile_document()
        self.profile = root / "profile.json"
        self.profile.write_text(json.dumps(self.document), encoding="utf-8")
        self.contract = root / "catalogue.fluid.yml"
        self.contract.write_text(contract or contract_text(), encoding="utf-8")
        self.bundle = root / "bundle"
        self.result = run_init(
            str(self.bundle), contract=str(self.contract), profile=str(self.profile)
        )
        assert self.result.exit_code == 0, self.result.stderr

    @property
    def simulation(self) -> Dict[str, Any]:
        text = (self.bundle / "simulation.flux.yml").read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        assert isinstance(document, dict)
        return document

    @property
    def block(self) -> Dict[str, Any]:
        return self.simulation["extensions"][NAMESPACE]  # type: ignore[no-any-return]

    def unused(self) -> Dict[str, str]:
        return {
            item["statistic"]["pointer"]: item["reason"] for item in self.block.get("unused", [])
        }

    def run(self, name: str = "state", *, seed: Optional[int] = None) -> Any:
        bundle = self.bundle
        if seed is not None:
            bundle = self.root / f"bundle-{seed}"
            bundle.mkdir(exist_ok=True)
            for source in self.bundle.iterdir():
                (bundle / source.name).write_text(source.read_text("utf-8"), "utf-8")
            text = (bundle / "simulation.flux.yml").read_text("utf-8")
            assert "seed: 42" in text
            (bundle / "simulation.flux.yml").write_text(
                text.replace("seed: 42", f"seed: {seed}"), "utf-8"
            )
        state = self.root / name
        outcome = run(bundle, state_dir=state, profile=self.profile)
        assert outcome.success, outcome.stderr
        verified = verify(
            bundle,
            run_id=outcome.artifacts["plan"]["run_id"],
            state_dir=state,
            profile=self.profile,
        )
        return outcome, verified


@pytest.fixture(scope="module")
def authored(tmp_path_factory: pytest.TempPathFactory) -> Authored:
    return Authored(tmp_path_factory.mktemp("catalogue"))


def test_the_catalogue_is_written_as_a_reference_expose_and_nothing_else(
    authored: Authored,
) -> None:
    emits = authored.block["emits"]
    assert emits["product"] == {"reference": True}
    assert set(emits["line"]) == {"rows", "fidelity"}
    assert emits["line"]["rows"] == LINES


def test_the_foreign_key_declares_the_catalogue_keys_it_counts(authored: Authored) -> None:
    column = authored.block["emits"]["line"]["fidelity"]["columns"]["l_sku"]
    assert sorted(column["levels"]) == _skus()
    shares = {sku: Fraction(share) for sku, share in column["levels"].items()}
    assert sum(shares.values()) == 1
    counts = _counts_per_sku()
    for sku, count in counts.items():
        assert abs(shares[sku] - Fraction(count, LINES)) < Fraction(1, 10**6)


def test_the_size_is_drawn_given_the_category_of_the_product(authored: Authored) -> None:
    fidelity = authored.block["emits"]["line"]["fidelity"]
    size = fidelity["columns"]["l_size"]
    assert size["given"] == "product.category"
    assert sorted(size["levelWeightsGiven"]) == sorted(CATEGORIES)
    for name, expected in LARGE.items():
        weights = {
            level: Fraction(share) for level, share in size["levelWeightsGiven"][name].items()
        }
        assert abs(weights["large"] - Fraction(expected)) < Fraction(1, 50)
    grouped = [p for p in fidelity["properties"] if p.get("groupBy") == "product.category"]
    assert [(p["property"], p["column"]) for p in grouped] == [("levelHistogram", "l_size")]
    assert fidelity["columns"]["product.category"]["support"] == {
        "values": ["tools", "paint", "garden", "lighting"]
    }
    assert authored.unused() == {} or all(
        "groupByLink" not in pointer for pointer in authored.unused()
    )


def test_the_grouping_is_used_not_listed_as_unused(authored: Authored) -> None:
    assert "/tables/line/columns/l_size/statistics/1" not in authored.unused()


@pytest.mark.slow
@pytest.mark.parametrize("seed", [None, 7])
def test_the_bundle_runs_and_passes_its_own_profile(
    authored: Authored, seed: Optional[int]
) -> None:
    outcome, verified = authored.run(f"state-{seed}", seed=seed)
    text = verified.stdout + verified.stderr
    assert verified.success, text
    assert "fidelity line: passed -- 3 of 3 declared properties within the margin" in text
    assert "rules product: published-rows 26 of 26" in text


@pytest.mark.slow
def test_the_catalogue_stream_is_the_published_rows_on_every_seed(authored: Authored) -> None:
    streams = []
    for seed in (None, 7):
        outcome, _ = authored.run(f"state-rows-{seed}", seed=seed)
        run_dir = (
            authored.root / f"state-rows-{seed}" / "runs" / outcome.artifacts["plan"]["run_id"]
        )
        streams.append((run_dir / "emits" / "product.ndjson").read_bytes())
    assert streams[0] == streams[1]
    published = authored.document["references"]["product"]["rows"]
    assert [json.loads(line)[0] for line in streams[0].decode().splitlines()] == [
        row[0] for row in published
    ]


# -- what cannot be drawn is listed as unused, with its reason ---------------------------------


def test_a_link_that_may_reach_no_row_is_listed_as_unused(tmp_path: Path) -> None:
    contract = contract_text(foreign="required: false")
    document = profile_document()
    stats = document["tables"]["line"]["columns"]["l_sku"]
    stats["metrics"] = {
        "nullCount": {
            "definition": "nullCount/1",
            "status": "F",
            "value": 60,
            "sampleSize": BAND,
            "disclosure": "none",
        }
    }
    authored = Authored(tmp_path, document, contract)
    reason = authored.unused()["/tables/line/columns/l_size/statistics/1"]
    assert "may reach no row" in reason and "FLX-3402" in reason
    assert "given" not in authored.block["emits"]["line"]["fidelity"]["columns"]["l_size"]


def test_a_column_of_the_catalogue_the_contract_does_not_link_is_listed_as_unused(
    tmp_path: Path,
) -> None:
    document = profile_document()
    grouping = document["tables"]["line"]["columns"]["l_size"]["statistics"][1]
    grouping["groupByLink"]["columns"] = ["l_id"]
    document["tables"]["line"]["columns"]["l_id"] = {"domain": "category"}
    authored = Authored(tmp_path, document)
    reason = authored.unused()["/tables/line/columns/l_size/statistics/1"]
    assert "declares no relationship" in reason


def test_two_relationships_over_the_same_columns_leave_the_grouping_unnamed() -> None:
    """The generator refuses such a contract, so the rule is read straight off the keys."""
    from flux_engine.author.catalogue import _relationship_of
    from flux_engine.seam.keys import ContractKeys, ExposeKeys, Relationship

    def link(name: str) -> Relationship:
        return Relationship(name, "line", "product", ("l_sku",), ("sku",), "many_to_one", "left")

    keys = ContractKeys(
        "0.7.7", {"line": ExposeKeys("line", relationships=(link("one"), link("two")))}
    )
    grouping = {"parent": {"table": "product", "columns": ["sku"]}, "columns": ["l_sku"]}
    found, why = _relationship_of(keys, "line", grouping)
    assert found is None and why is not None and "two relationships" in why


def test_a_grouping_by_a_column_that_is_no_text_is_listed_as_unused(tmp_path: Path) -> None:
    document = profile_document(link_column="price")
    authored = Authored(tmp_path, document)
    assert "not text" in authored.unused()["/tables/line/columns/l_size/statistics/1"]


def test_a_foreign_key_counted_over_values_that_are_no_keys_declares_no_level(
    tmp_path: Path,
) -> None:
    document = profile_document()
    histogram = document["tables"]["line"]["columns"]["l_sku"]["statistics"][0]
    histogram["levels"] = ["A-0"] + histogram["levels"][1:]
    authored = Authored(tmp_path, document)
    column = authored.block["emits"]["line"]["fidelity"]["columns"].get("l_sku", {})
    assert "levels" not in column
    assert "uniformly" in authored.unused()["/tables/line/columns/l_sku/statistics/0"]


# -- a profile of an earlier format --------------------------------------------------------------


def test_a_profile_of_format_1_2_0_writes_the_bundle_it_wrote_before(tmp_path: Path) -> None:
    """The catalogue's rows are no references there, and the lines' grouping is unknown to it."""
    document = profile_document()
    older = copy.deepcopy(document)
    older["_schemaURL"] = SCHEMA_URL
    del older["references"]
    del older["tables"]["line"]["columns"]["l_size"]["statistics"][1]
    older["tables"]["product"] = {"rowCount": 26, "columns": {"sku": {"domain": "category"}}}
    authored = Rationale(tmp_path, older)
    emits = authored.block["emits"]
    assert "reference" not in emits.get("product", {})
    assert "given" not in emits["line"]["fidelity"]["columns"]["l_size"]
    assert not any("product." in name for name in emits["line"]["fidelity"]["columns"])


class Rationale(Authored):
    """An earlier format's profile: one that has no reference rows to pin by digest."""


def _generated_parent(document: Dict[str, Any]) -> Dict[str, Any]:
    """The same profile with the catalogue counted as a table, not published whole."""
    older = copy.deepcopy(document)
    del older["references"]
    names = sorted(CATEGORIES)
    counts = [CATEGORIES[name] for name in names]
    older["tables"]["product"] = {
        "rowCount": 26,
        "columns": {
            "sku": {
                "domain": "category",
                "metrics": {
                    name: {
                        "definition": f"{name}/1",
                        "status": "F",
                        "value": 26,
                        "sampleSize": BAND,
                        "disclosure": "none",
                    }
                    for name in ("count", "distinctCount")
                },
            },
            "category": {"domain": "category", "statistics": [_histogram(names, counts)]},
            "price": {"domain": "number"},
        },
    }
    older["tables"]["line"]["columns"]["l_sku"]["statistics"] = []
    return older


def test_a_parent_the_bundle_generates_is_given_its_levels_too(tmp_path: Path) -> None:
    document = _generated_parent(profile_document())
    authored = Authored(tmp_path, document)
    emits = authored.block["emits"]
    assert "reference" not in emits["product"]
    size = emits["line"]["fidelity"]["columns"]["l_size"]
    assert size["given"] == "product.category"
    assert sorted(size["levelWeightsGiven"]) == sorted(CATEGORIES)
    _, verified = authored.run("generated-parent")
    # The 26 products draw their own categories, so only the lines' check is the one under test.
    assert verified.artifacts["fidelity"]["line"]["status"] == "passed", verified.stdout


def test_a_parent_column_that_holds_nulls_is_no_parent_of_a_given(tmp_path: Path) -> None:
    document = _generated_parent(profile_document())
    category = document["tables"]["product"]["columns"]["category"]
    category["metrics"] = {
        "nullCount": {
            "definition": "nullCount/1",
            "status": "F",
            "value": 4,
            "sampleSize": BAND,
            "disclosure": "none",
        },
        "count": {
            "definition": "count/1",
            "status": "F",
            "value": 26,
            "sampleSize": BAND,
            "disclosure": "none",
        },
    }
    contract = contract_text().replace(
        "{ name: category, type: STRING, required: true }",
        "{ name: category, type: STRING, required: false }",
    )
    authored = Authored(tmp_path, document, contract)
    reason = authored.unused()["/tables/line/columns/l_size/statistics/1"]
    assert "no level to condition on" in reason and "FLX-3402" in reason
    assert "given" not in authored.block["emits"]["line"]["fidelity"]["columns"]["l_size"]


# -- a column whose sensitivity sets a minimum grade is no reference expose --------------------


def _labelled(label: str) -> str:
    text = contract_text()
    marked = text.replace(
        "{ name: category, type: STRING, required: true }",
        f"{{ name: category, type: STRING, required: true, sensitivity: {label} }}",
    )
    assert marked != text
    return marked


def _published_and_counted() -> Dict[str, Any]:
    """The catalogue published whole under references, and counted as a table too."""
    document = _generated_parent(profile_document())
    document["references"] = profile_document()["references"]
    return document


def test_a_labelled_catalogue_is_generated_and_its_references_entry_is_listed_as_unused(
    tmp_path: Path,
) -> None:
    authored = Authored(tmp_path, _published_and_counted(), _labelled("internal"))
    emits = authored.block["emits"]
    assert "reference" not in emits["product"]
    assert emits["product"]["rows"] == 26
    reason = authored.unused()["/references/product"]
    assert "released whole" in reason and "sensitivity" in reason and "guarantee" in reason
    _, verified = authored.run("labelled-catalogue")
    assert "published-rows" not in verified.stdout + verified.stderr


def test_a_label_that_sets_no_minimum_grade_keeps_the_reference_expose(tmp_path: Path) -> None:
    authored = Authored(tmp_path, _published_and_counted(), _labelled("none"))
    assert authored.block["emits"]["product"] == {"reference": True}
    assert "/references/product" not in authored.unused()


def test_a_column_named_exactly_like_the_link_keeps_the_grouping_listed_as_unused(
    tmp_path: Path,
) -> None:
    """``given: product.category`` would read the line's own column of that name, not the link.

    A name with a dot that a column holds exactly is that column (SEAM-FIDELITY-SPEC section 2),
    so ``fluxsim init`` writes neither the given nor the group key for the link, and says why. The
    link form beside it is the other bundle of this module, which writes both.
    """
    extra = "        - { name: product.category, type: STRING, required: false }\n"
    authored = Authored(tmp_path, contract=contract_text(line_extra=extra))
    reason = authored.unused()["/tables/line/columns/l_size/statistics/1"]
    assert "named exactly product.category" in reason and "not as the link" in reason
    fidelity = authored.block["emits"]["line"]["fidelity"]
    assert "given" not in fidelity["columns"]["l_size"]
    assert not [p for p in fidelity["properties"] if p.get("groupBy") == "product.category"]


def test_init_plans_the_bundle_on_kernel_0_2_0_only_for_a_given_that_is_a_link() -> None:
    """The pre-flight reads ``given: tier.code`` as a link only when the emit holds no such column."""
    import types

    from flux_engine.author.bundle import _given_across_link

    def emit(own: List[str], given: str) -> Any:
        columns = {"x": {"given": given, "levelWeightsGiven": {}}}
        return types.SimpleNamespace(
            fidelity={"columns": columns},
            compile=lambda: types.SimpleNamespace(column_names=tuple(own)),
        )

    assert _given_across_link(emit(["x"], "tier.code")) is True
    assert _given_across_link(emit(["x", "tier.code"], "tier.code")) is False
    assert _given_across_link(emit(["x", "tier"], "tier")) is False
