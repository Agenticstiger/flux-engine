# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Children per parent are counted (ADR 0016 sections 1 and 2, KERNEL-SPEC-2 section 8.1).

A ``counts`` entry puts a relationship on the counted layout: its foreign key takes the parent whose
run of consecutive child rows holds the row, and draws nothing. What is pinned, on kernel 0.2.0 (the
only kernel that generates it):

* **The layout is the rows.** Every parent holds the children the layout allots it, the histogram of
  children per parent is the declared one class for class, and a parent's children are consecutive
  rows.
* **Nothing is drawn.** The foreign key column's stream is never read, and the layout is worked out
  once per process.
* **Two paths, one answer.** The column path's rows equal the per-row path's, and a slice anywhere is
  the rows of the whole table.
* **The plan.** The foreign key's document names the pick and the shape; a plan with no entry keeps
  its document and digest.
* **The refusals**, each before any row exists.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator import columns, relations
from flux_engine.generator.counts import (
    class_counts,
    classes_of,
    count_key,
    counted_layout,
    slot_values,
)
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.reference import ReferenceRows, build_reference_plan
from flux_engine.generator.relations import (
    ForeignKey,
    RelationReader,
    relate_plans,
    related_columns,
)
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.rules import plan_rules
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.counts import ChildShape, CountsEntry
from flux_engine.seam.keys import compile_keys, mark_key_columns
from flux_engine.seam.links import LinkEntry

from .test_relations import _column, _expose, _h, _primary, _rel
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

SIM_ID = "sim-counted"
ACCOUNTS = 40
SHAPE = {"levels": ["1", "3", "6"], "levelWeights": ["0.5", "0.3", "0.2"]}
#: The slots of 40 parents under SHAPE take 20 parents of 1 child, 12 of 3 and 8 of 6.
INVOICES = 104


def entry(
    relationship: str = "account",
    shape: Optional[Mapping[str, Sequence[str]]] = SHAPE,
    emit: str = "invoice",
    minimum: Optional[int] = None,
    when: Optional[str] = None,
) -> CountsEntry:
    children = None
    if shape is not None:
        kind = "levels" if "levels" in shape else "bins"
        keys = ("levels", "levelWeights") if kind == "levels" else ("bins", "binWeights")
        children = ChildShape(kind, tuple(shape[keys[0]]), tuple(shape[keys[1]]))
    return CountsEntry(
        emit=emit,
        relationship=relationship,
        children=children,
        min_children=minimum,
        parent_when=when,
    )


COUNTED = {"invoice": {"account": entry()}}
ONE_TO_ONE = {"invoice": {"account": entry(shape=None)}}


def contract(*, account_id: str = "STRING", cardinality: str = "many_to_one") -> Dict[str, Any]:
    account = _expose(
        "account",
        [
            _column("a_id", account_id),
            _column("a_name", "STRING", False),
            _column("a_weight", "INT", False),
        ],
        _primary("account", "a_id"),
    )
    tag = _expose("tag", [_column("t_id", "STRING")], _primary("tag", "t_id"))
    invoice = _expose(
        "invoice",
        [
            _column("i_id", "STRING"),
            _column("i_account", account_id, False),
            _column("i_tag", "STRING", False),
            _column("i_amount", "INT", False),
        ],
        {
            **_primary("invoice", "i_id"),
            "relationships": [
                _rel("account", "account", ["i_account"], ["a_id"], cardinality),
                _rel("tag", "tag", ["i_tag"], ["t_id"]),
            ],
        },
    )
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.counted",
        "exposes": [account, tag, invoice],
    }


def root_for(seed: int = 5) -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), seed, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


def plans_of(
    *,
    counts: Optional[Mapping[str, Mapping[str, CountsEntry]]] = None,
    document: Optional[Dict[str, Any]] = None,
    invoice: Optional[Dict[str, Any]] = None,
    account: Optional[Dict[str, Any]] = None,
    rows: int = INVOICES,
    accounts: int = ACCOUNTS,
    links: Optional[Mapping[str, Sequence[LinkEntry]]] = None,
    bridges: Optional[Mapping[str, Any]] = None,
    weights: Optional[Mapping[str, Mapping[str, str]]] = None,
    references: Optional[Mapping[str, ReferenceRows]] = None,
    counter_based: bool = True,
    namespace: Optional[Mapping[str, Any]] = None,
) -> Dict[str, GeneratorPlan]:
    document = contract() if document is None else document
    keys = compile_keys(document)
    assert keys is not None
    declared: Dict[str, Tuple[Dict[str, Any], int]] = {
        "account": (
            {
                "a_name": {"levels": {"n": "1"}},
                "a_weight": {"range": {"min": "1", "max": "9"}},
                **(account or {}),
            },
            accounts,
        ),
        "tag": ({}, 5),
        "invoice": ({"i_amount": {"range": {"min": "1", "max": "900"}}, **(invoice or {})}, rows),
    }
    version = GENERATOR_VERSION_2 if counter_based else GENERATOR_VERSION
    plans: Dict[str, GeneratorPlan] = {}
    for expose in document["exposes"]:
        expose_id = expose["exposeId"]
        expose_keys = keys.expose(expose_id)
        compiled = mark_key_columns(compile_expose(expose), expose_keys)
        if references and expose_id in references:
            plans[expose_id] = build_reference_plan(
                compiled,
                sim_id=SIM_ID,
                seam_digest=_h(expose_id),
                table=references[expose_id],
                generator_version=version,
                where=f"reference expose {expose_id!r}",
            )
            continue
        columns_, count = declared[expose_id]
        related = related_columns(expose_keys)
        plans[expose_id] = build_plan(
            compiled,
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": columns_},
            related=related,
            rules=plan_rules(
                contract_document=document,
                expose_id=expose_id,
                settings=None,
                related=related,
                mapping=None,
                has_fidelity=True,
                strict=False,
                flux_rules=None,
                keys=expose_keys,
                rows=count,
                null_fractions={
                    name: block["nullFraction"]
                    for name, block in columns_.items()
                    if "nullFraction" in block
                },
                counter_based=counter_based,
                links=(links or {}).get(expose_id, ()),
                copies=False,
            ),
            generator_version=version,
        )
    return relate_plans(
        plans,
        keys,
        bridges,
        weights,
        COUNTED if counts is None else counts,
        document,
        namespace,
    )


@pytest.fixture(autouse=True)
def fresh_layouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test works its layouts out afresh: the cache is per process."""
    monkeypatch.setattr(relations, "_LAYOUTS", {})


def table(plan: GeneratorPlan, rows: Any) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [dict(zip(names, row, strict=True)) for row in rows]


def refused(change: Callable[[], Any], code: str = "FLX-3403") -> str:
    with pytest.raises(FluxError) as caught:
        change()
    assert caught.value.code.code == code, str(caught.value)
    return str(caught.value)


def world(plans: Mapping[str, GeneratorPlan], seed: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    root = root_for(seed)
    return {name: table(plan, generate_rows(plan, root=root)) for name, plan in plans.items()}


def layout_of(
    rows: int = INVOICES, accounts: int = ACCOUNTS, seed: int = 5, shape: Any = SHAPE
) -> Any:
    return counted_layout(
        count_key(root_for(seed), SIM_ID, "invoice", "account"), accounts, rows, shape=shape
    )


# -- the layout is the rows -------------------------------------------------------------------


def test_the_slots_of_the_shape_total_the_child_rows() -> None:
    assert int(slot_values(ACCOUNTS, classes_of(SHAPE)).sum()) == INVOICES


def test_every_parent_holds_the_children_the_layout_allots_it_and_they_are_consecutive() -> None:
    data = world(plans_of())
    layout = layout_of()
    keys = [row["a_id"] for row in data["account"]]
    invoices = data["invoice"]
    assert [row["i_account"] for row in invoices] == [
        keys[layout.parent_of(i)] for i in range(INVOICES)
    ]
    held = Counter(row["i_account"] for row in invoices)
    assert [held[key] for key in keys] == layout.counts.tolist()
    for parent, key in enumerate(keys):
        run = layout.children_of(parent)
        assert [invoices[i]["i_account"] for i in run] == [key] * len(run)


def test_the_histogram_of_children_per_parent_is_the_declared_one_class_for_class() -> None:
    data = world(plans_of())
    held = Counter(Counter(row["i_account"] for row in data["invoice"]).values())
    expected = class_counts(ACCOUNTS, classes_of(SHAPE))
    assert [held[level] for level in (1, 3, 6)] == expected == [20, 12, 8]
    assert sum(held.values()) == ACCOUNTS  # no parent is childless


def test_no_shape_spreads_the_children_as_evenly_as_whole_counts_allow() -> None:
    plans = plans_of(counts={"invoice": {"account": entry(shape=None)}}, rows=100)
    held = Counter(row["i_account"] for row in world(plans)["invoice"])
    assert len(held) == ACCOUNTS and sum(held.values()) == 100
    assert set(held.values()) == {2, 3}


def test_a_different_seed_moves_which_parent_takes_which_count_and_not_the_histogram() -> None:
    first, second = world(plans_of(), seed=5), world(plans_of(), seed=6)
    count_of = lambda data: [  # noqa: E731
        Counter(row["i_account"] for row in data["invoice"])[row["a_id"]] for row in data["account"]
    ]
    assert count_of(first) != count_of(second)
    assert sorted(count_of(first)) == sorted(count_of(second))


def test_the_reader_names_the_parent_of_a_row_through_the_layout() -> None:
    plans = plans_of()
    reader = RelationReader(root_for())
    layout = layout_of()
    for row in (0, 1, 50, INVOICES - 1):
        assert reader.parent_row(plans["invoice"], "account", row) == layout.parent_of(row)


def test_a_reference_parent_is_laid_out_over_its_published_rows() -> None:
    references = {
        "account": ReferenceRows(
            ("a_id", "a_name", "a_weight"), tuple((f"acc{i}", "n", "1") for i in range(5))
        )
    }
    plans = plans_of(
        references=references, rows=11, counts={"invoice": {"account": entry(shape=None)}}
    )
    held = Counter(row["i_account"] for row in world(plans)["invoice"])
    assert sorted(held) == [f"acc{i}" for i in range(5)] and sorted(held.values()) == [
        2,
        2,
        2,
        2,
        3,
    ]


# -- nothing is drawn -------------------------------------------------------------------------


def test_the_foreign_key_columns_stream_is_never_read(monkeypatch: pytest.MonkeyPatch) -> None:
    plans = plans_of()
    root = root_for()
    asked: List[str] = []
    stream_at = CellStreams.stream_at

    def spy(self: CellStreams, row_index: int, column: str) -> Any:
        asked.append(column)
        return stream_at(self, row_index, column)

    forbidden = CellStreams(root, SIM_ID, "invoice").column_key("i_account")
    keys: List[Any] = []
    for name in ("column_words", "column_uniform53", "column_bounded"):
        original = getattr(kernel2, name)

        def keyed(key: Any, *args: Any, _original: Any = original) -> Any:
            keys.append(key)
            return _original(key, *args)

        monkeypatch.setattr(kernel2, name, keyed)
    monkeypatch.setattr(CellStreams, "stream_at", spy)
    for path in ("columns", "rows"):
        with monkeypatch.context() as patch:
            if path == "rows":
                patch.setattr(columns, "column_rows", lambda *args: None)
            generate_rows(plans["invoice"], root=root)
    assert "i_account" not in asked
    assert keys and forbidden not in keys


def test_the_layout_is_worked_out_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    plans = plans_of()
    root = root_for()
    made: List[int] = []
    original = relations.counted_layout

    def spy(*args: Any, **kwargs: Any) -> Any:
        made.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(relations, "counted_layout", spy)
    generate_rows(plans["invoice"], root=root)
    generate_row_slice(plans["invoice"], 10, 60, root=root)
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        generate_row_slice(plans["invoice"], 0, 30, root=root)
    assert made == [1]
    generate_rows(plans["invoice"], root=root_for(6))
    assert made == [1, 1]


# -- two paths, one answer --------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_paths_cells_under_a_counted_layout_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plans = plans_of()
    root = root_for()
    for expose_id, plan in plans.items():
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda plan=plan: generate_rows(plan, root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken and all(taken), expose_id


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (7, 61), (103, 104)])
def test_a_slice_of_a_counted_table_is_the_whole_tables_slice(start: int, stop: int) -> None:
    plans = plans_of()
    root = root_for()
    whole = generate_rows(plans["invoice"], root=root)
    assert generate_row_slice(plans["invoice"], start, stop, root=root) == whole[start:stop]
    names = [column_plan.column.name for column_plan in plans["invoice"].columns]
    assert generate_column(plans["invoice"], "i_account", root=root) == tuple(
        row[names.index("i_account")] for row in whole
    )


def test_a_counted_relationship_leaves_the_other_relationships_and_columns_alone() -> None:
    counted, plain = world(plans_of()), world(plans_of(counts={}))
    for column in ("i_id", "i_tag", "i_amount"):
        assert [row[column] for row in counted["invoice"]] == [
            row[column] for row in plain["invoice"]
        ]
    assert counted["account"] == plain["account"]
    assert [row["i_account"] for row in counted["invoice"]] != [
        row["i_account"] for row in plain["invoice"]
    ]


# -- the plan ---------------------------------------------------------------------------------


def test_the_foreign_key_names_its_pick_and_its_shape_in_the_plan_document() -> None:
    plans = plans_of()
    plan = plans["invoice"]
    assert plan.relations is not None
    driver = plan.relations.driver("i_account")
    assert isinstance(driver, ForeignKey) and driver.pick == "counted"
    assert driver.counted is not None and driver.counted.child_rows == INVOICES
    document = plan.as_document()["relations"]
    assert document["i_account"]["pick"] == "counted"
    assert document["i_account"]["counts"] == {"children": SHAPE}
    assert document["i_tag"]["pick"] == "uniform" and "counts" not in document["i_tag"]
    bare = plans_of(counts={"invoice": {"account": entry(shape=None)}})["invoice"].as_document()
    assert bare["relations"]["i_account"]["counts"] == {}


def test_a_plan_with_no_entry_keeps_its_document_and_the_entry_moves_it() -> None:
    plain = plans_of(counts={})
    assert json.dumps(plain["invoice"].as_document()).count("counts") == 0
    counted = plans_of()
    assert counted["invoice"].digest() != plain["invoice"].digest()
    other = plans_of(
        counts={
            "invoice": {
                "account": entry(shape={"levels": ["1", "4"], "levelWeights": ["0.5", "0.5"]})
            }
        }
    )
    assert other["invoice"].digest() != counted["invoice"].digest()
    assert plain["account"].digest() == counted["account"].digest()


def test_the_coverage_of_a_counted_key_is_derived_from_its_relationship() -> None:
    from flux_engine.generator.coverage import coverage_of_plan

    rows = {row.column: row for row in coverage_of_plan(plans_of()["invoice"]).columns}
    assert (rows["i_account"].origin.value, rows["i_account"].decided_by) == (
        "derived",
        "relationship",
    )
    assert rows["i_account"].source == "semantics.relationships.account"


# -- the refusals -----------------------------------------------------------------------------


def one_to_one() -> Dict[str, Any]:
    return contract(cardinality="one_to_one")


@pytest.mark.parametrize(
    ("label", "build", "needle"),
    [
        (
            "no-such-relationship",
            lambda: plans_of(counts={"invoice": {"customer": entry("customer")}}),
            "no relationship",
        ),
        (
            "an-emit-with-no-plan",
            lambda: plans_of(counts={"nowhere": {"account": entry(emit="nowhere")}}),
            "does not generate",
        ),
        (
            "a-reference-expose",
            lambda: plans_of(
                references={
                    "invoice": ReferenceRows(
                        ("i_id", "i_account", "i_tag", "i_amount"), (("x", None, None, None),)
                    )
                }
            ),
            "reference expose",
        ),
        (
            "a-null-fraction",
            lambda: plans_of(invoice={"i_account": {"nullFraction": "0.1"}}),
            "nullFraction",
        ),
        (
            "a-draw-of-its-own",
            lambda: plans_of(invoice={"i_account": {"levels": {"a": "1"}}}),
            "draw of its own",
        ),
        (
            "a-one-to-one-relationship-with-more-rows-than-its-parent",
            lambda: plans_of(document=one_to_one(), rows=ACCOUNTS + 1, counts=ONE_TO_ONE),
            "at most once",
        ),
        (
            "a-bridge-side",
            lambda: plans_of(bridges={"invoice": [("account", "tag")]}, rows=50),
            "bridge",
        ),
        (
            "a-links-condition",
            lambda: plans_of(
                links={"invoice": [LinkEntry("invoice", "account", when="i_amount > 100")]}
            ),
            "counts entry",
        ),
        (
            "a-links-weight",
            lambda: plans_of(weights={"invoice": {"account": "a_weight"}}),
            "counts entry",
        ),
        (
            "a-shape-that-is-not-ascending",
            lambda: plans_of(
                counts={
                    "invoice": {
                        "account": entry(
                            shape={"levels": ["3", "1"], "levelWeights": ["0.5", "0.5"]}
                        )
                    }
                }
            ),
            "strictly ascending",
        ),
        (
            "weights-that-do-not-sum-to-one",
            lambda: plans_of(
                counts={
                    "invoice": {
                        "account": entry(
                            shape={"levels": ["1", "2"], "levelWeights": ["0.5", "0.4"]}
                        )
                    }
                }
            ),
            "sum to",
        ),
        (
            "a-level-that-is-a-fraction",
            lambda: plans_of(
                counts={
                    "invoice": {"account": entry(shape={"levels": ["1.5"], "levelWeights": ["1"]})}
                }
            ),
            "whole number",
        ),
    ],
)
def test_what_cannot_be_counted_is_refused_before_a_row_exists(
    label: str, build: Callable[[], Any], needle: str
) -> None:
    assert needle in refused(build), label


def _shared(counts: Mapping[str, Mapping[str, CountsEntry]], contract: Any = None) -> Any:
    from .test_shared_copies import plans_of as shared_plans
    from .test_shared_copies import shared_contract

    document = shared_contract() if contract is None else contract
    keys = compile_keys(document)
    assert keys is not None
    return relate_plans(shared_plans(document, relate=False, copies=False), keys, counts=counts)


def test_a_member_of_a_several_column_key_is_refused() -> None:
    message = refused(lambda: _shared({"b": {"bx": entry("bx", None, "b")}}))
    assert "part of a key" in message and "bx" in message


def test_a_relationship_another_one_implies_is_refused() -> None:
    document = _shared_with_implied()
    message = refused(lambda: _shared({"c": {"c_x_only": entry("c_x_only", None, "c")}}, document))
    assert "link" in message and "implies it" in message


def _shared_with_implied() -> Dict[str, Any]:
    from .test_shared_copies import shared_contract

    document = shared_contract()
    document["exposes"][3]["semantics"]["relationships"].append(
        _rel("c_x_only", "x", ["c_x"], ["x_id"])
    )
    document["exposes"][2]["semantics"]["relationships"][0]["toColumns"] = ["x_id"]
    return document


def test_a_self_relationship_is_refused() -> None:
    document = contract()
    account = document["exposes"][0]
    account["contract"]["schema"].append(_column("a_parent", "STRING", False))
    account["semantics"]["relationships"] = [_rel("parent", "account", ["a_parent"], ["a_id"])]
    message = refused(
        lambda: plans_of(
            document=document, counts={"account": {"parent": entry("parent", None, "account")}}
        )
    )
    assert "its own expose" in message


def test_a_dependency_that_sets_the_foreign_key_is_refused() -> None:
    document = contract()
    document["exposes"][2]["contract"]["invariants"] = [
        {"id": "dep", "dependency": {"from": ["i_amount"], "to": ["i_account"]}}
    ]
    assert "dependency" in refused(lambda: plans_of(document=document))
    assert plans_of(document=document, counts={})  # the same bundle, with no counts, plans


def test_a_dependency_cannot_draw_a_counted_relationship() -> None:
    plans = plans_of()
    plan = plans["invoice"]
    assert plan.relations is not None
    driver = plan.relations.driver("i_account")
    assert isinstance(driver, ForeignKey)
    stream = CellStreams(root_for(), SIM_ID, "invoice").stream_at(0, "i_account")
    message = refused(
        lambda: RelationReader(root_for()).foreign_on_stream(
            plan.column_plan("i_account"), driver, stream
        )
    )
    assert "counted layout" in message


def test_kernel_0_1_0_refuses_the_entry_and_names_the_kernel() -> None:
    message = refused(lambda: plans_of(counter_based=False), "FLX-3105")
    assert "0.2.0" in message and "0.1.0" in message


# -- minChildren ------------------------------------------------------------------------------

#: The generatorDigest of ``plans_of()["invoice"]`` and of the same plan with no shape, as the
#: counted layout without ``minChildren`` made them: an entry that declares none keeps them.
PINNED_DIGEST = "sha256:3b204d9223bc26462f37faf95b13d8aa710b210da6bdaeda0839026e94e178eb"
PINNED_BARE_DIGEST = "sha256:75577b7004953b4035a4c006e918690988ecd9544749bb7e98e54d80df83e23d"


def test_an_entry_without_a_minimum_keeps_the_plan_digest_it_had() -> None:
    assert plans_of()["invoice"].digest().value == PINNED_DIGEST
    bare = plans_of(counts={"invoice": {"account": entry(shape=None)}})
    assert bare["invoice"].digest().value == PINNED_BARE_DIGEST
    document = plans_of()["invoice"].as_document()["relations"]["i_account"]["counts"]
    assert "minChildren" not in document


def test_a_declared_minimum_is_in_the_plan_document_and_moves_the_digest() -> None:
    with_minimum = plans_of(counts={"invoice": {"account": entry(minimum=1)}})["invoice"]
    document = with_minimum.as_document()["relations"]["i_account"]["counts"]
    assert document == {"minChildren": 1, "children": SHAPE}
    assert with_minimum.digest().value != PINNED_DIGEST
    zero = plans_of(counts={"invoice": {"account": entry(minimum=0)}})["invoice"]
    assert zero.digest().value not in (PINNED_DIGEST, with_minimum.digest().value)


@pytest.mark.parametrize("minimum", [1, 2])
def test_every_parent_has_at_least_the_minimum_and_the_layout_is_the_rows(minimum: int) -> None:
    shape = {"levels": ["0", "2", "5"], "levelWeights": ["0.25", "0.5", "0.25"]}
    plans = plans_of(counts={"invoice": {"account": entry(shape=shape, minimum=minimum)}})
    data = world(plans)
    held = Counter(row["i_account"] for row in data["invoice"])
    keys = [row["a_id"] for row in data["account"]]
    assert sum(held.values()) == INVOICES
    assert min(held[key] for key in keys) >= minimum
    layout = counted_layout(
        count_key(root_for(), SIM_ID, "invoice", "account"),
        ACCOUNTS,
        INVOICES,
        shape=shape,
        minimum=minimum,
    )
    assert [held[key] for key in keys] == layout.counts.tolist()


def test_the_column_path_equals_the_per_row_path_under_a_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = plans_of(counts={"invoice": {"account": entry(minimum=2)}})
    root = root_for()
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_rows(plans["invoice"], root=root)
    )
    assert by_columns == by_rows and taken and all(taken)


@pytest.mark.parametrize(
    ("build", "needle"),
    [
        (lambda: plans_of(counts={"invoice": {"account": entry(minimum=7)}}), "above the most"),
        (
            lambda: plans_of(
                counts={"invoice": {"account": entry(shape=None, minimum=3)}}, rows=119
            ),
            "N = 119",
        ),
        (
            lambda: plans_of(
                counts={"invoice": {"account": entry(shape=None, minimum=3)}}, rows=119
            ),
            "e = 40",
        ),
    ],
)
def test_a_minimum_the_shape_or_the_child_rows_cannot_meet_is_refused_at_plan(
    build: Callable[[], Any], needle: str
) -> None:
    assert needle in refused(build)


def test_a_minimum_the_child_rows_just_meet_is_laid_out() -> None:
    plans = plans_of(counts={"invoice": {"account": entry(shape=None, minimum=3)}}, rows=120)
    held = Counter(row["i_account"] for row in world(plans)["invoice"])
    assert set(held.values()) == {3} and len(held) == ACCOUNTS


# -- parentWhen -------------------------------------------------------------------------------

WHEN = "a_weight > 5"


def when_counts(
    when: str = WHEN, shape: Any = None, minimum: Optional[int] = None
) -> Dict[str, Dict[str, CountsEntry]]:
    return {"invoice": {"account": entry(shape=shape, minimum=minimum, when=when)}}


def eligible_keys(
    data: Mapping[str, List[Dict[str, Any]]], when: Any = lambda w: w > 5
) -> List[Any]:
    return [
        row["a_id"]
        for row in data["account"]
        if row["a_weight"] is not None and when(row["a_weight"])
    ]


def test_children_sit_only_under_the_parents_the_condition_holds_for() -> None:
    plans = plans_of(counts=when_counts(shape=SHAPE))
    data = world(plans)
    eligible = eligible_keys(data)
    assert 0 < len(eligible) < ACCOUNTS
    held = Counter(row["i_account"] for row in data["invoice"])
    assert set(held) <= set(eligible)
    assert sum(held.values()) == INVOICES and all(held[key] >= 1 for key in eligible)
    rows = [index for index, row in enumerate(data["account"]) if row["a_id"] in set(eligible)]
    layout = counted_layout(
        count_key(root_for(), SIM_ID, "invoice", "account"),
        ACCOUNTS,
        INVOICES,
        shape=SHAPE,
        eligible=rows,
    )
    assert [held[row["a_id"]] for row in data["account"]] == layout.counts.tolist()


def test_a_minimum_holds_for_every_eligible_parent_and_the_rest_have_none() -> None:
    data = world(plans_of(counts=when_counts(minimum=2)))
    eligible = set(eligible_keys(data))
    held = Counter(row["i_account"] for row in data["invoice"])
    assert all(held[key] >= 2 for key in eligible)
    assert not set(held) - eligible and sum(held.values()) == INVOICES


def test_the_condition_is_in_the_plan_document_and_the_childs_digest_holds_the_parents() -> None:
    plain = plans_of()["invoice"]
    conditioned = plans_of(counts=when_counts(shape=SHAPE))["invoice"]
    document = conditioned.as_document()
    assert document["relations"]["i_account"]["counts"]["parentWhen"] == "a_weight > 5"
    assert "parentWhen" not in plain.as_document()["relations"]["i_account"]["counts"]
    assert sorted(document["reads"]) == ["account"] and "reads" not in plain.as_document()
    assert conditioned.digest() != plain.digest()
    other = plans_of(counts=when_counts("a_weight > 6", shape=SHAPE))["invoice"]
    assert other.digest() != conditioned.digest()
    # A parent that moves in a way that could change eligibility moves the child's digest.
    moved = plans_of(
        counts=when_counts(shape=SHAPE), account={"a_weight": {"range": {"min": "1", "max": "8"}}}
    )["invoice"]
    assert moved.digest() != conditioned.digest()


def test_the_condition_is_checked_the_way_a_rules_when_is_and_canonicalised() -> None:
    plans = plans_of(counts=when_counts("a_weight>5  AND a_name IS NOT NULL", shape=SHAPE))
    document = plans["invoice"].as_document()["relations"]["i_account"]["counts"]
    assert "a_weight" in document["parentWhen"] and "IS NOT NULL" in document["parentWhen"].upper()
    data = world(plans)
    held = Counter(row["i_account"] for row in data["invoice"])
    assert set(held) <= set(eligible_keys(data))


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_path_equals_the_per_row_path_under_a_condition(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plans = plans_of(counts=when_counts(minimum=1))
    root = root_for()
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_rows(plans["invoice"], root=root)
    )
    assert by_columns == by_rows and taken and all(taken)


def test_a_slice_is_the_whole_tables_slice_and_the_layout_is_worked_out_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = plans_of(counts=when_counts(shape=SHAPE))
    root = root_for()
    made: List[int] = []
    original = relations.counted_layout

    def spy(*args: Any, **kwargs: Any) -> Any:
        made.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(relations, "counted_layout", spy)
    whole = generate_rows(plans["invoice"], root=root)
    assert generate_row_slice(plans["invoice"], 9, 70, root=root) == whole[9:70]
    assert made == [1]


def test_a_reference_parent_is_conditioned_on_its_published_values() -> None:
    references = {
        "account": ReferenceRows(
            ("a_id", "a_name", "a_weight"), tuple((f"acc{i}", "n", str(i)) for i in range(6))
        )
    }
    plans = plans_of(references=references, rows=9, counts=when_counts("a_weight > 2"))
    held = Counter(row["i_account"] for row in world(plans)["invoice"])
    assert sorted(held) == ["acc3", "acc4", "acc5"] and sorted(held.values()) == [3, 3, 3]


def test_child_rows_too_few_for_the_eligible_parents_are_refused_when_the_layout_is_worked_out() -> (
    None
):
    plans = plans_of(counts=when_counts(minimum=3), rows=7)  # planning cannot know e
    message = refused(lambda: generate_rows(plans["invoice"], root=root_for()))
    assert "N = 7" in message and "m = 3" in message and "e = " in message


def test_a_condition_no_parent_meets_is_refused_when_the_layout_is_worked_out() -> None:
    plans = plans_of(counts=when_counts("a_weight > 100"))
    assert "e = 0" in refused(lambda: generate_rows(plans["invoice"], root=root_for()))


@pytest.mark.parametrize(
    ("when", "needle"),
    [
        ("a_weight + 1", "FLX-6206"),
        ("a_name", "FLX-6206"),
        ("a_nothing > 1", ""),
        ("not a predicate (", ""),
    ],
)
def test_a_condition_that_is_not_a_predicate_over_the_parent_is_refused_at_plan(
    when: str, needle: str
) -> None:
    with pytest.raises(FluxError) as caught:
        plans_of(counts=when_counts(when))
    assert needle in str(caught.value)


def test_a_condition_needs_the_contract_to_be_checked() -> None:
    document = contract()
    keys = compile_keys(document)
    assert keys is not None
    plans = plans_of(counts={})
    refused(lambda: relate_plans(plans, keys, counts=when_counts()))


def test_a_refusal_quotes_a_long_weight_or_edge_by_a_short_prefix() -> None:
    long = "9" * 10_000
    for shape in (
        {"levels": ["1", "2"], "levelWeights": [long, "0.5"]},
        {"levels": ["1", long], "levelWeights": ["0.5", "0.5"]},
        {"bins": ["1", long], "binWeights": ["1"]},
        {"levels": [long, "1"], "levelWeights": ["0.5", "0.5"]},
    ):
        with pytest.raises(FluxError) as caught:
            classes_of(shape)
        assert caught.value.code.code == "FLX-3403"
        assert len(str(caught.value)) < 400
        assert "..." in str(caught.value)


def test_a_refusal_quotes_a_long_list_of_edges_by_a_short_prefix() -> None:
    edges = [str(n) for n in range(5000, 0, -1)]
    shape = {"bins": edges, "binWeights": ["1"] * (len(edges) - 1)}
    with pytest.raises(FluxError) as caught:
        classes_of(shape)
    assert caught.value.code.code == "FLX-3403"
    assert len(str(caught.value)) < 400


# -- a links when or weight on a counted relationship (ADR 0016 section 1) ---------------------


def joined(join_type: str) -> Dict[str, Any]:
    """The contract with the invoice to account relationship given *join_type*."""
    document = contract()
    invoice = next(e for e in document["exposes"] if e["exposeId"] == "invoice")
    invoice["semantics"]["relationships"][0]["joinType"] = join_type
    return document


WHEN_LINK = {"invoice": [LinkEntry("invoice", "account", when="i_amount > 100")]}


@pytest.mark.parametrize("join_type", ["inner", "left"])
def test_a_links_when_on_a_counted_relationship_is_refused_once_and_names_counts(
    join_type: str,
) -> None:
    message = refused(lambda: plans_of(document=joined(join_type), links=WHEN_LINK))
    assert "relationship 'account' is on the counted layout (its counts entry)" in message
    assert "cannot give it a when" in message and "allotted count" in message
    assert "joinType" not in message and message.count("FLX-3403") == 1


def test_a_links_weight_on_a_counted_relationship_is_refused_once_and_names_counts() -> None:
    message = refused(lambda: plans_of(weights={"invoice": {"account": "a_weight"}}))
    assert "relationship 'account' is on the counted layout (its counts entry)" in message
    assert "cannot give it a weight" in message and message.count("FLX-3403") == 1


def test_a_relationship_that_is_not_counted_keeps_its_joinType_advice() -> None:
    message = refused(lambda: plans_of(document=joined("inner"), links=WHEN_LINK, counts={}))
    assert "its joinType is inner" in message and "make the joinType left" in message
    assert "counted layout" not in message
    # A left join with no counts entry takes the link as before.
    assert plans_of(document=joined("left"), links=WHEN_LINK, counts={})
