# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` never writes a bundle it then refuses: a family of small worlds.

A library has branches and members, the loans of its members, perhaps a link table between
branches and members made only of foreign keys, and a reference table of areas that a member's
columns can be drawn given. Each world differs from the next in what its profile withholds, and
``init`` meets it in one of two ways, whichever the profile leaves it:

* it exits ``0`` and the bundle plans, with every number it could not read labelled; or
* it exits non-zero before writing, says what was withheld or what to change, and leaves no bundle
  directory behind.

What it must never do is write a bundle its own validation then refuses: that exits non-zero with
the message of its own check, and it is the one failure the family looks for. The worlds vary:

* one parent of the loans, or two;
* each table's ``rowCount``, published or withheld;
* each relationship's children-per-parent histogram, published or withheld;
* the link table, there or not;
* the members' ``tier``, drawn given the zone of the area they lie in, or given a boolean column of
  their own row, or neither;
* a number column drawn over its published histogram, or only over the range its contract declares,
  and drawn given another column of its row or of a linked row;
* a rank correlation of two number columns;
* a profile released under differential privacy, or a plain one.

A refusal this family was written from is pinned by a test of its own (``test_init_children_given_linked``,
``test_init_unwritten_child``, ``test_init_ranged_properties``), and each is an explicit example
here; the drawn worlds are the check that no other mixture of the same withheld numbers finds
another.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Tuple

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from flux_engine.author.bundle import AuthoredExpose, _drop_unkept_properties
from flux_engine.author.columns import ColumnPlan, PropertyPlan
from flux_engine.cli._init import run_init
from flux_engine.seam.contract import LogicalKind
from flux_engine.stages.plan import plan
from flux_stats.profile import SCHEMA_URL_1_4_0
from tests.author import rank_profile as rp
from tests.author.test_init_children_given import _metric
from tests.author.test_init_unwritten_child import _NOISE

pytestmark = pytest.mark.integration

#: The message of init's own check of the bundle it wrote: the refusal this family exists to rule out.
SELF_REFUSAL = "the bundle written from these inputs"

AREAS = {"a1": "north", "a2": "north", "a3": "south"}
ZONES = ("north", "south")
TIERS = ("basic", "plus")
EDGES = ["0", "1", "3", "10"]
AGE_EDGES = ["0", "25", "50", "75", "100"]
VISIT_EDGES = ["0", "10", "20", "30", "40"]
SAMPLE = {"min": 1, "max": 99999}
OPEN_SAMPLE = {"min": 0}
#: Each count below is of this many records, so that no table is small enough for exact allocation.
UNIT = 40
ROWS = {"branch": 50, "member": 240 * UNIT, "loan": 250 * UNIT, "branch_member": 200 * UNIT}
TABLES = tuple(ROWS)
#: Each relationship by its child table and its parent, with the key of the child that points at it.
RELATIONSHIPS = {
    "loan_member": ("loan", "member"),
    "loan_branch": ("loan", "branch"),
    "link_member": ("branch_member", "member"),
    "link_branch": ("branch_member", "branch"),
}
#: The children per parent, over the classes 0, 1 to 2 and 3 to 9 children.
PER_PARENT = [100, 130, 10]


@dataclass(frozen=True)
class World:
    """What one library's profile holds and what it withholds."""

    two_parents: bool
    link: bool
    rows: FrozenSet[str]
    histograms: FrozenSet[str]
    tier_given_zone: bool
    tier_given_active: bool
    age_histogram: bool
    visits_histogram: bool
    age_given: str
    rank_pair: bool
    noisy: bool

    @property
    def relationships(self) -> Tuple[str, ...]:
        found = ["loan_member"]
        if self.two_parents:
            found.append("loan_branch")
        if self.link:
            found += ["link_member", "link_branch"]
        return tuple(found)

    @property
    def tables(self) -> Tuple[str, ...]:
        return ("branch", "member", "loan") + (("branch_member",) if self.link else ())


worlds = st.builds(
    World,
    two_parents=st.booleans(),
    link=st.booleans(),
    rows=st.frozensets(st.sampled_from(TABLES)),
    histograms=st.frozensets(st.sampled_from(sorted(RELATIONSHIPS))),
    tier_given_zone=st.booleans(),
    tier_given_active=st.booleans(),
    age_histogram=st.booleans(),
    visits_histogram=st.booleans(),
    age_given=st.sampled_from(("none", "tier", "zone")),
    rank_pair=st.booleans(),
    noisy=st.booleans(),
)


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def _expose(name: str, columns: List[str], semantics: str = "") -> str:
    schema = "\n".join(f"        - {column}" for column in columns)
    return (
        f"  - exposeId: {name}\n"
        "    kind: table\n"
        "    binding:\n"
        "      platform: local\n"
        "      format: parquet\n"
        f"      location: {{ path: ./out/{name}.parquet }}\n"
        "    contract:\n"
        "      schema:\n"
        f"{schema}\n" + (f"    semantics:\n{semantics}" if semantics else "")
    )


def _entity(name: str, column: str) -> str:
    return f"      entities:\n        - {{ name: {name}, type: primary, expr: {column} }}\n"


def _relationship(name: str, to: str, column: str) -> str:
    return (
        f"        - name: {name}\n"
        f"          to: {to}\n"
        f"          fromColumns: [{column}]\n"
        f"          toColumns: [{column}]\n"
        "          cardinality: many_to_one\n"
    )


def _ranged(name: str, high: int) -> str:
    return (
        f"{{ name: {name}, type: INT, required: true, validationRules: "
        f"[{{ type: range, constraint: between 0 and {high} }}] }}"
    )


def _contract(world: World) -> str:
    text = (
        'fluidVersion: "0.7.7"\nkind: DataProduct\nid: town.library\nname: Library\n'
        "metadata:\n  owner:\n    team: town\nexposes:\n"
    )
    text += _expose(
        "area",
        [
            "{ name: area_id, type: STRING, required: true }",
            "{ name: zone, type: STRING, required: true }",
        ],
        _entity("area", "area_id"),
    )
    text += _expose(
        "branch",
        ["{ name: branch_id, type: INT, required: true }"],
        _entity("branch", "branch_id"),
    )
    text += _expose(
        "member",
        [
            "{ name: member_id, type: INT, required: true }",
            "{ name: area_id, type: STRING, required: true }",
            "{ name: tier, type: STRING, required: true }",
            _ranged("age", 100),
            _ranged("visits", 40),
            "{ name: active, type: BOOL, required: true }",
        ],
        _entity("member", "member_id")
        + "      relationships:\n"
        + _relationship("member_area", "area", "area_id"),
    )
    loan = [
        "{ name: loan_id, type: INT, required: true }",
        "{ name: member_id, type: INT, required: true }",
    ]
    links = _relationship("loan_member", "member", "member_id")
    if world.two_parents:
        loan.append("{ name: branch_id, type: INT, required: true }")
        links += _relationship("loan_branch", "branch", "branch_id")
    text += _expose("loan", loan, _entity("loan", "loan_id") + "      relationships:\n" + links)
    if world.link:
        text += _expose(
            "branch_member",
            [
                "{ name: member_id, type: INT, required: true }",
                "{ name: branch_id, type: INT, required: true }",
            ],
            "      relationships:\n"
            + _relationship("link_member", "member", "member_id")
            + _relationship("link_branch", "branch", "branch_id"),
        )
    return text


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


def _statistic(
    definition: str,
    noisy: bool,
    groups: List[Tuple[str, List[int]]],
    **named: Any,
) -> Dict[str, Any]:
    """One statistic of *definition*, a count per cell for each group, plain or released noisy."""
    key = "levelCounts" if definition == "levelHistogram/1" else "binCounts"
    groups = [(group, [count * UNIT for count in counts]) for group, counts in groups]
    statistic: Dict[str, Any] = {
        "definition": definition,
        "sampleSize": OPEN_SAMPLE if noisy else SAMPLE,
        "disclosure": "noisy" if noisy else "none",
        **named,
    }
    if noisy:
        statistic["noise"] = {**_NOISE, "privacyLoss": {"rho": "100"}}
        statistic["groups"] = [
            {"group": group, "status": "F", "noisyCounts": [str(count) for count in counts]}
            for group, counts in groups
        ]
    else:
        statistic["groups"] = [
            {"group": group, "k": sum(counts), key: counts, "status": "F"}
            for group, counts in groups
        ]
    return statistic


def _levels(noisy: bool, levels: Tuple[str, ...], counts: List[int]) -> Dict[str, Any]:
    return _statistic("levelHistogram/1", noisy, [("", counts)], levels=list(levels))


def _histogram(noisy: bool, edges: List[str], counts: List[int]) -> Dict[str, Any]:
    return _statistic("marginalHistogram/1", noisy, [("", counts)], bins=edges)


def _given_zone(world: World, definition: str, **named: Any) -> Dict[str, Any]:
    """A statistic of a member's column, grouped by the zone of the area it lies in."""
    cells = len(named["levels"]) if "levels" in named else len(named["bins"]) - 1
    return _statistic(
        definition,
        world.noisy,
        [(zone, [3 + place + cell for cell in range(cells)]) for place, zone in enumerate(ZONES)],
        groupByLink={
            "parent": {"table": "area", "columns": ["area_id"]},
            "columns": ["area_id"],
            "column": "zone",
        },
        **named,
    )


def _given_tier(world: World, edges: List[str]) -> Dict[str, Any]:
    """A statistic of a member's number column, grouped by the member's tier."""
    cells = len(edges) - 1
    return _statistic(
        "marginalHistogram/1",
        world.noisy,
        [(tier, [4 + place + cell for cell in range(cells)]) for place, tier in enumerate(TIERS)],
        bins=edges,
        groupBy="tier",
    )


def _tier(world: World) -> Dict[str, Any]:
    statistics = [_levels(world.noisy, TIERS, [150, 90])]
    if world.tier_given_zone:
        statistics.append(_given_zone(world, "levelHistogram/1", levels=list(TIERS)))
    if world.tier_given_active:
        statistics.append(
            _statistic(
                "levelHistogram/1",
                world.noisy,
                [("false", [60, 20]), ("true", [90, 70])],
                levels=list(TIERS),
                groupBy="active",
            )
        )
    return {"domain": "category", "statistics": statistics}


def _number(world: World, edges: List[str], shown: bool, given_by: str) -> Dict[str, Any]:
    """A number column of the members: its own histogram when *shown*, and a grouped one if asked."""
    column: Dict[str, Any] = {"domain": "number"}
    statistics: List[Dict[str, Any]] = []
    if shown:
        statistics.append(_histogram(world.noisy, edges, [30, 90, 70, 50][: len(edges) - 1]))
    if given_by == "zone":
        statistics.append(_given_zone(world, "marginalHistogram/1", bins=edges))
    elif given_by == "tier":
        statistics.append(_given_tier(world, edges))
    if statistics:
        column["statistics"] = statistics
    if not world.noisy:
        column["metrics"] = {"count": _metric("count/1", ROWS["member"])}
    return column


def _member(world: World) -> Dict[str, Any]:
    table: Dict[str, Any] = {
        "columns": {
            "member_id": {"domain": "number"},
            "area_id": {
                "domain": "category",
                "statistics": [_levels(world.noisy, tuple(AREAS), [100, 80, 60])],
            },
            "tier": _tier(world),
            "active": {
                "domain": "category",
                "statistics": [_levels(world.noisy, ("false", "true"), [80, 160])],
            },
            "age": _number(world, AGE_EDGES, world.age_histogram, world.age_given),
            "visits": _number(world, VISIT_EDGES, world.visits_histogram, "none"),
        }
    }
    if world.rank_pair and world.noisy:
        # The pair's release spends what a count table of its row floor does, so it states enough.
        pair = rp.noisy_pair("0.4", floor=100)
        pair["noise"]["privacyLoss"] = {"rho": "1000"}
        table["columnPairs"] = [{"columns": ["age", "visits"], "statistics": [pair]}]
    return table


def _per_parent(world: World, relationship: str) -> List[Dict[str, Any]]:
    """The children per parent of one relationship: all of them, and by tier for the members."""
    statistics = [_histogram(world.noisy, EDGES, PER_PARENT)]
    if relationship == "loan_member":
        groups = [
            (tier, [PER_PARENT[0] + 5 * place, PER_PARENT[1], PER_PARENT[2] - place])
            for place, tier in enumerate(TIERS)
        ]
        statistics.append(
            _statistic("marginalHistogram/1", world.noisy, groups, bins=EDGES, groupBy="tier")
        )
    return statistics


def _private(world: World) -> Dict[str, Any]:
    """The privacy of a noisy profile: a release per member, followed down to the loans and links."""
    # A release follows its unit down to a child only along a relationship the profile publishes.
    followed = [
        child
        for name, (child, parent) in RELATIONSHIPS.items()
        if parent == "member" and name in world.relationships and name in world.histograms
    ]
    unit: Dict[str, Any] = {"expose": "member"}
    if followed:
        unit["follows"] = [
            {
                "parent": {"table": "member", "columns": ["member_id"]},
                "child": {"table": child, "columns": ["member_id"]},
                "maxChildren": 20,
            }
            for child in followed
        ]
    return {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "100000"},
        "privacyUnit": unit,
        "publicStructure": True,
    }


def _profile(world: World) -> Dict[str, Any]:
    """The profile of *world*: what it publishes of each table, and nothing it withholds."""
    loan = ["loan_id", "member_id"] + (["branch_id"] if world.two_parents else [])
    columns: Dict[str, Dict[str, Any]] = {
        "branch": {"branch_id": {"domain": "number"}},
        "loan": {name: {"domain": "number"} for name in loan},
    }
    if world.link:
        columns["branch_member"] = {
            "member_id": {"domain": "number"},
            "branch_id": {"domain": "number"},
        }
    tables: Dict[str, Any] = {name: {"columns": found} for name, found in columns.items()}
    tables["member"] = _member(world)
    for name in world.tables:
        if name in world.rows:
            tables[name]["rowCount"] = ROWS[name]
    document: Dict[str, Any] = {
        "_producer": "urn:example:library-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}, "noisy": {}},
        "tables": tables,
        "references": {
            "area": {
                "disclosure": "none",
                "columns": ["area_id", "zone"],
                "rows": [[name, zone] for name, zone in AREAS.items()],
            }
        },
        "relationships": [
            {
                "parent": {"table": parent, "columns": [f"{parent}_id"]},
                "child": {"table": child, "columns": [f"{parent}_id"]},
                "statistics": _per_parent(world, name),
            }
            for name, (child, parent) in RELATIONSHIPS.items()
            if name in world.relationships and name in world.histograms
        ],
    }
    if world.noisy:
        document["privacy"] = _private(world)
    return document


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


def _meets(world: World) -> None:
    """Run ``init`` on *world*: it writes a bundle that plans, or it refuses before writing, saying why."""
    with tempfile.TemporaryDirectory(prefix="fluxsim-family-") as scratch:
        root = Path(scratch)
        contract, profile, bundle = (
            root / "library.fluid.yaml",
            root / "library.profile.json",
            root / "bundle",
        )
        contract.write_text(_contract(world), encoding="utf-8")
        profile.write_text(json.dumps(_profile(world)), encoding="utf-8")
        result = run_init(str(bundle), contract=str(contract), profile=str(profile))
        if result.exit_code == 0:
            planned = plan(bundle, state_dir=root / "state", profile=profile, stage=False)
            assert (
                planned.success
            ), f"init wrote a bundle that does not plan: {planned.stderr}\n{world}"
            return
        said = result.stderr
        assert "profile document refused" not in said, f"the family wrote a bad profile: {said}"
        assert SELF_REFUSAL not in said, f"init refused the bundle it wrote: {said}\n{world}"
        assert said.startswith("init: "), said
        assert any(
            name in said
            for name in (*TABLES, "area", "tier", "age", "visits", "profile", "contract")
        ), f"a refusal that names nothing to change: {said}\n{world}"
        assert not bundle.exists(), f"init left a bundle behind after refusing: {said}\n{world}"


#: A world that withholds everything it can: plain, one parent, no link, no grouped statistic.
BASE = World(
    two_parents=False,
    link=False,
    rows=frozenset(),
    histograms=frozenset(),
    tier_given_zone=False,
    tier_given_active=False,
    age_histogram=True,
    visits_histogram=True,
    age_given="none",
    rank_pair=False,
    noisy=False,
)


@settings(max_examples=12, deadline=None, derandomize=True)
@given(worlds)
# A counted child, placed by a column drawn given a row of a linked table.
@example(replace(BASE, histograms=frozenset({"loan_member"}), tier_given_zone=True))
# A link table made only of foreign keys, whose children per parent are published.
@example(
    replace(
        BASE,
        two_parents=True,
        link=True,
        histograms=frozenset({"loan_member", "loan_branch", "link_member", "link_branch"}),
        noisy=True,
    )
)
# Number columns drawn over their contract ranges, and drawn by one copula.
@example(replace(BASE, age_histogram=False, visits_histogram=False, rank_pair=True, noisy=True))
# A number column drawn over its contract range and given a column of its row or of a linked row.
@example(replace(BASE, age_histogram=False, age_given="tier"))
@example(replace(BASE, age_histogram=False, age_given="zone"))
# A column drawn given a boolean column, which declares no levels.
@example(replace(BASE, histograms=frozenset({"loan_member"}), tier_given_active=True))
def test_init_writes_a_bundle_that_plans_or_refuses_before_writing(world: World) -> None:
    _meets(world)


# ---------------------------------------------------------------------------
# The last place the properties are chosen
# ---------------------------------------------------------------------------


def _property(**document: Any) -> PropertyPlan:
    return PropertyPlan({"tolerance": {"type": "count", "max": 0}, **document}, [], scorable=True)


def _declared(*columns: ColumnPlan) -> AuthoredExpose:
    return AuthoredExpose(
        expose_id="member",
        columns=list(columns),
        kinds={column.name: LogicalKind.INT for column in columns},
    )


def test_a_property_naming_a_column_with_no_kept_declaration_is_dropped_with_a_note() -> None:
    """Whatever writer made it: a column kept as a range or as levels is what a property reads."""
    ranged = ColumnPlan("age", {"range": {"min": "0", "max": "9"}, "bins": ["0", "9"]})
    unranged = ColumnPlan("visits", {"bins": ["0", "9"], "binWeights": ["1"]})
    levelled = ColumnPlan("tier", {"support": {"values": ["a"]}, "maxCardinality": 1})
    unlevelled = ColumnPlan("active", {"levels": {"false": "0.5", "true": "0.5"}})
    kept = [
        _property(property="marginalHistogram", column="age", bins=["0", "9"]),
        _property(property="levelHistogram", column="tier", groupBy="tier2"),
        _property(property="marginalHistogram", column="age", groupBy="tier"),
        _property(property="rankCorrelation", columns=["age", "age2"]),
    ]
    gone = [
        # a range column that declares none, a column of levels that declares none, and a
        # grouping by a column of the wrong kind, or by one the block does not declare
        _property(property="marginalHistogram", column="visits", bins=["0", "9"]),
        _property(property="levelHistogram", column="tier", groupBy="active"),
        _property(property="marginalHistogram", column="age", groupBy="age"),
        _property(property="marginalHistogram", column="age", groupByBins={"column": "tier"}),
        _property(property="rankCorrelation", columns=["age", "visits"]),
        _property(property="support", column="visits"),
    ]
    expose = _declared(ranged, unranged, levelled, unlevelled)
    expose.linked = {"tier2": ColumnPlan("tier2", {"support": {"values": ["a"]}})}
    expose.columns.append(ColumnPlan("age2", {"range": {"min": "0", "max": "9"}}))
    expose.properties = [*kept, *gone]
    _drop_unkept_properties(expose)
    assert expose.properties == kept
    # the notes are on the column each dropped property was over, once each, as a choice
    said = [sentence for subject, sentence in expose.assumptions if subject == "visits"]
    assert len(said) == 2 and all("FLX-5034" in sentence for sentence in said)
    assert said == unranged.assumptions == unranged.choices[: len(said)]
    assert any("active" in sentence for _, sentence in expose.assumptions)


def test_a_property_every_column_of_which_is_kept_is_left_as_it_is() -> None:
    expose = _declared(ColumnPlan("age", {"range": {"min": "0", "max": "9"}}))
    expose.properties = [_property(property="mean", column="age")]
    before = list(expose.properties)
    _drop_unkept_properties(expose)
    assert expose.properties == before
    assert expose.assumptions == []
