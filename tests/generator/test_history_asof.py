# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A dated row reads the version of a linked entity valid on its date (ADR 0017 section 8).

An invoice names a subscription, a history's emit, and carries a date. With an ``asOf`` entry on the
relationship, a rule that reads through it reads the version of the subscription valid on the
invoice's date, not the row the foreign key picked. What is pinned, on kernel 0.2.0:

* **The read.** The version is the one whose start is not after the date and whose end is open or,
  under ``nextStart``, after it, or, under ``dayBefore``, not before it, in both chain modes and for a
  DATE against a TIMESTAMP both ways, checked against a join of the rows the run wrote.
* **No version.** A date before the entity's first start reads NULL through every path.
* **Without an entry.** A path may read the carried columns, the natural key and the activation; a
  tracked column, a validity column or a column a rule sets is ``FLX-6106`` naming ``asOf``.
* **The refusals.** A ``one_to_one`` relationship and a ``counts`` entry on one, a required column set
  from the link, and a date a rule sets from the same link.
* **Two paths, one answer.** The column path's rows equal the per-row path's, and a slice that cuts
  the table is the whole table's slice.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator import columns
from flux_engine.generator.history import (
    Chain,
    HistoryBinding,
    instant_ms,
    version_valid_on,
)
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.rules import plan_rules
from flux_engine.seam.asof import AsOfEntry
from flux_engine.seam.contract import LogicalKind, compile_expose
from flux_engine.seam.counts import ChildShape, CountsEntry
from flux_engine.seam.history import Histogram, HistoryEnd, HistoryEntry, HistoryTracked
from flux_engine.seam.keys import compile_keys, mark_key_columns

from .test_history_layout import (
    BOUNDS,
    SIM_ID,
    _column,
    _expose,
    _h,
    _primary,
    _rel,
    root_for,
    table,
)
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

EMIT = "sub_version"
DAY = 86_400_000
EPOCH = date(1970, 1, 1).toordinal()
ASOF = AsOfEntry("invoice", "sub", "i_date")
PLAN_MOVES = {"a": (("b", "1"),), "b": (("a", "0.5"), ("c", "0.5")), "c": (("a", "1"),)}
TRACKED = (HistoryTracked("plan", tuple(PLAN_MOVES.items())),)
LENGTH = Histogram(("30", "90", "200"), ("0.5", "0.5"))
VERSIONS = ChildShape("levels", ("1", "2", "4"), ("0.3", "0.4", "0.3"))
ENTITIES = 12
ROWS = 36
INVOICES = 120

#: A rule of the invoice for each column it carries: what it reads through ``sub``.
RULES: Dict[str, Dict[str, str]] = {
    "plan_then": {"derive": "plan_then", "expr": "sub.plan"},
    "region_then": {"derive": "region_then", "expr": "sub.region"},
    "sub_then": {"derive": "sub_then", "expr": "sub.sub_id"},
    "activated_then": {"derive": "activated_then", "expr": "sub.activated"},
    "from_then": {"derive": "from_then", "expr": "sub.valid_from"},
    "price_then": {"derive": "price_then", "expr": "sub.price"},
    "amount": {
        "derive": "amount",
        "expr": "CASE WHEN sub.plan = 'a' THEN 10 WHEN sub.plan = 'b' THEN 20 ELSE 30 END",
    },
    "date_from_sub": {"derive": "i_date", "expr": "sub.valid_from"},
}


def entry_of(
    *,
    chain: str = "nextStart",
    end: Optional[HistoryEnd] = None,
    until: str = "2025-12-31",
    activation: Optional[str] = "activated",
) -> HistoryEntry:
    """The history of the versioned emit: ``plan`` tracked, everything else carried or set."""
    return HistoryEntry(
        emit=EMIT,
        entities=ENTITIES,
        length=LENGTH,
        until=until,
        versions=VERSIONS,
        chain=chain,
        tracked=TRACKED,
        activation=activation,
        end=end,
    )


def contract(
    *,
    kind: str = "DATE",
    date_kind: str = "DATE",
    cardinality: str = "many_to_one",
    amount_required: bool = False,
    rules: Sequence[str] = (),
    priced: bool = True,
) -> Dict[str, Any]:
    """A versioned emit (a tracked plan, a carried region, a rule-set price) and the invoice that reads it."""
    version = _expose(
        EMIT,
        [
            _column("version_id", "INT"),
            _column("sub_id", "STRING"),
            _column("plan", "STRING", False),
            _column("region", "STRING", False),
            _column("activated", kind, False),
            _column("valid_from", kind),
            _column("valid_to", kind, False),
            _column("price", "INT", False),
        ],
        {
            "entities": [
                {"name": "version", "type": "primary", "expr": "version_id"},
                {"name": "sub", "type": "natural", "expr": "sub_id"},
            ],
            "dimensions": [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"validityParams": {"isStart": True}},
                },
                {
                    "name": "valid_to",
                    "type": "time",
                    "typeParams": {"validityParams": {"isEnd": True}},
                },
            ],
        },
    )
    if priced:
        version["contract"]["invariants"] = [
            {
                "id": "price_of_plan",
                "derive": "price",
                "expr": "CASE WHEN plan = 'a' THEN 5 WHEN plan = 'b' THEN 7 ELSE 9 END",
            }
        ]
    derived = {
        "plan_then": _column("plan_then", "STRING", False),
        "region_then": _column("region_then", "STRING", False),
        "sub_then": _column("sub_then", "STRING", False),
        "activated_then": _column("activated_then", kind, False),
        "from_then": _column("from_then", kind, False),
        "price_then": _column("price_then", "INT", False),
        "amount": _column("amount", "INT", amount_required),
    }
    invoice = _expose(
        "invoice",
        [
            _column("i_id", "INT"),
            _column("i_sub", "STRING", False),
            _column("i_date", date_kind),
            *(derived[name] for name in rules if name in derived),
        ],
        {
            **_primary("invoice", "i_id"),
            "defaultAggTimeDimension": "i_date",
            "relationships": [_rel("sub", EMIT, ["i_sub"], ["sub_id"], cardinality)],
        },
    )
    invoice["contract"]["invariants"] = [{"id": f"{name}_rule", **RULES[name]} for name in rules]
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.history.asof",
        "exposes": [version, invoice],
    }


def date_range(kind: str) -> Dict[str, Any]:
    """The declared range of the invoice's date: the window, as the column's type spells it."""
    if kind == "DATE":
        return {"range": {"min": "2023-01-01", "max": "2025-12-31"}}
    return {"range": {"min": "2023-01-01T00:00:00Z", "max": "2025-12-31T00:00:00Z"}}


def plans_with(
    *,
    held: Optional[HistoryEntry] = None,
    kind: str = "DATE",
    date_kind: str = "DATE",
    asof: Optional[Sequence[AsOfEntry]] = (ASOF,),
    counts: Optional[Mapping[str, Mapping[str, CountsEntry]]] = None,
    invoice_range: Optional[Mapping[str, Any]] = None,
    counter_based: bool = True,
    **shape: Any,
) -> Dict[str, GeneratorPlan]:
    """The plans of the contract above, the invoice carrying the rules *shape* names."""
    document = contract(kind=kind, date_kind=date_kind, **shape)
    held = held if held is not None else entry_of()
    if kind == "TIMESTAMP" and "T" not in held.until:
        held = HistoryEntry(**{**held.__dict__, "until": held.until + "T00:00:00Z"})
    keys = compile_keys(document)
    assert keys is not None
    statistics: Dict[str, Tuple[Dict[str, Any], int]] = {
        EMIT: (
            {
                "plan": {"levels": {"a": "1", "b": "0", "c": "0"}},
                "region": {"levels": {"n": "0.5", "s": "0.5"}},
                **(
                    {}
                    if shape.get("priced", True)
                    else {"price": {"range": {"min": "1", "max": "9"}}}
                ),
            },
            ROWS,
        ),
        "invoice": ({"i_date": dict(invoice_range or date_range(date_kind))}, INVOICES),
    }
    plans: Dict[str, GeneratorPlan] = {}
    for expose in document["exposes"]:
        expose_id = expose["exposeId"]
        expose_keys = keys.expose(expose_id)
        compiled = mark_key_columns(compile_expose(expose), expose_keys)
        fidelity, count = statistics[expose_id]
        related = related_columns(expose_keys) | ({"sub_id"} if expose_id == EMIT else set())
        plans[expose_id] = build_plan(
            compiled,
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": fidelity},
            time_bounds=BOUNDS,
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
                null_fractions={},
                counter_based=counter_based,
                history=(held,) if expose_id == EMIT else (),
                asof=tuple(asof or ()) if expose_id == "invoice" else (),
                history_emits=(EMIT,),
            ),
            generator_version=GENERATOR_VERSION_2,
        )
    binding = {EMIT: HistoryBinding(held, ENTITIES, "valid_from", "valid_to")}
    return relate_plans(plans, keys, None, None, counts, document, None, binding)


def rows_of(
    plans: Mapping[str, GeneratorPlan], expose_id: str, seed: int = 5
) -> List[Dict[str, Any]]:
    plan = plans[expose_id]
    return table(plan, generate_row_slice(plan, 0, plan.rows, root=root_for(seed)))


def refusal(change: Any, code: str) -> str:
    with pytest.raises(FluxError) as caught:
        change()
    assert caught.value.code.code == code, str(caught.value)
    return str(caught.value)


# -- the read: a join of the rows the run wrote -----------------------------------------------------


def moment(value: Any) -> int:
    """A DATE (its midnight UTC) or a TIMESTAMP (milliseconds) as milliseconds."""
    if isinstance(value, date):
        return (value.toordinal() - EPOCH) * DAY
    return int(value)


def valid_row(
    versions: Sequence[Mapping[str, Any]], when: Any, chain: str
) -> Optional[Mapping[str, Any]]:
    """The version valid on *when*, found by looking at every version, not by bisection."""
    at = moment(when)
    reach = DAY if chain == "dayBefore" else 0
    for row in versions:
        stop = row["valid_to"]
        if moment(row["valid_from"]) <= at and (stop is None or at < moment(stop) + reach):
            return row
    return None


def joined(
    plans: Mapping[str, GeneratorPlan], chain: str, seed: int = 5
) -> List[Tuple[Dict[str, Any], Optional[Mapping[str, Any]]]]:
    """Each invoice with the version of its subscription valid on its date, by a join of the rows."""
    versions: Dict[Any, List[Mapping[str, Any]]] = {}
    for row in rows_of(plans, EMIT, seed):
        versions.setdefault(row["sub_id"], []).append(row)
    return [
        (invoice, valid_row(versions[invoice["i_sub"]], invoice["i_date"], chain))
        for invoice in rows_of(plans, "invoice", seed)
    ]


KINDS = [
    pytest.param("DATE", "DATE", "nextStart", id="date-nextStart"),
    pytest.param("DATE", "DATE", "dayBefore", id="date-dayBefore"),
    pytest.param("TIMESTAMP", "TIMESTAMP", "nextStart", id="timestamp-nextStart"),
    pytest.param("DATE", "TIMESTAMP", "nextStart", id="timestamp-date-against-a-date-history"),
    pytest.param("TIMESTAMP", "DATE", "nextStart", id="date-against-a-timestamp-history"),
    pytest.param("DATE", "TIMESTAMP", "dayBefore", id="timestamp-against-dayBefore"),
]


@pytest.mark.parametrize(("kind", "date_kind", "chain"), KINDS)
@pytest.mark.parametrize("seed", [1, 2])
def test_a_rule_reads_the_version_valid_on_the_rows_date(
    kind: str, date_kind: str, chain: str, seed: int
) -> None:
    plans = plans_with(
        held=entry_of(chain=chain),
        kind=kind,
        date_kind=date_kind,
        rules=["plan_then", "region_then", "price_then"],
    )
    pairs = joined(plans, chain, seed)
    found = [(invoice, version) for invoice, version in pairs if version is not None]
    assert found and len(found) < len(pairs)  # some dates find a version, some do not
    for invoice, version in pairs:
        if version is None:
            assert invoice["plan_then"] is None and invoice["price_then"] is None
            assert invoice["region_then"] is None
        else:
            assert invoice["plan_then"] == version["plan"]
            assert invoice["price_then"] == version["price"]
            assert invoice["region_then"] == version["region"]
    # The tracked column changes, so the version a row reads is not always the first.
    assert len({invoice["plan_then"] for invoice, _ in found}) > 1


def test_the_foreign_key_still_copies_the_entitys_key_and_the_pick_is_unchanged() -> None:
    with_entry = plans_with(rules=["region_then"])
    bare = plans_with(rules=["region_then"], asof=())
    held = {row["sub_id"] for row in rows_of(with_entry, EMIT)}
    assert {row["i_sub"] for row in rows_of(with_entry, "invoice")} <= held
    assert [row["i_sub"] for row in rows_of(with_entry, "invoice")] == [
        row["i_sub"] for row in rows_of(bare, "invoice")
    ]


def test_a_path_through_the_link_is_null_for_a_date_before_the_first_start() -> None:
    plans = plans_with(
        invoice_range={"range": {"min": "2020-01-01", "max": "2022-12-31"}},
        rules=["plan_then", "price_then"],
    )
    data = rows_of(plans, "invoice")
    assert data and all(row["plan_then"] is None and row["price_then"] is None for row in data)


def test_a_date_after_an_ended_entitys_closed_end_reads_no_version() -> None:
    held = entry_of(end=HistoryEnd("closeLast", "1"))
    plans = plans_with(
        held=held,
        invoice_range={"range": {"min": "2026-01-01", "max": "2026-12-31"}},
        rules=["plan_then"],
    )
    data = rows_of(plans, "invoice")
    assert data and all(row["plan_then"] is None for row in data)


def test_a_date_after_until_reads_the_open_version_of_an_entity_that_has_not_ended() -> None:
    plans = plans_with(
        invoice_range={"range": {"min": "2026-01-01", "max": "2026-12-31"}},
        rules=["plan_then", "amount"],
        amount_required=True,
    )
    versions: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows_of(plans, EMIT):
        versions.setdefault(row["sub_id"], []).append(row)
    for invoice in rows_of(plans, "invoice"):
        last = versions[invoice["i_sub"]][-1]
        assert last["valid_to"] is None
        assert invoice["plan_then"] == last["plan"]
        assert invoice["amount"] == {"a": 10, "b": 20, "c": 30}[last["plan"]]


def test_the_entry_is_part_of_the_rule_plan_document() -> None:
    plans = plans_with(rules=["plan_then"])
    rules = plans["invoice"].rules
    assert rules is not None and rules.asof == (ASOF,)
    assert rules.as_document()["asOf"] == [{"relationship": "sub", "date": "i_date"}]


# -- without an entry -------------------------------------------------------------------------------


def test_a_path_without_an_entry_reads_the_carried_columns_the_key_and_the_activation() -> None:
    plans = plans_with(asof=(), rules=["region_then", "sub_then", "activated_then"], priced=False)
    versions: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows_of(plans, EMIT):
        versions.setdefault(row["sub_id"], []).append(row)
    for invoice in rows_of(plans, "invoice"):
        first = versions[invoice["i_sub"]][0]
        assert invoice["region_then"] == first["region"]
        assert invoice["sub_then"] == invoice["i_sub"]
        assert invoice["activated_then"] == first["valid_from"] == first["activated"]


@pytest.mark.parametrize("rule", ["plan_then", "from_then", "price_then"])
def test_a_tracked_a_validity_or_a_rule_set_column_without_an_entry_is_6106(rule: str) -> None:
    message = refusal(lambda: plans_with(asof=(), rules=[rule]), "FLX-6106")
    assert "asOf" in message and "invoice.sub" in message


def test_a_versioned_expose_with_no_history_keeps_the_refusal() -> None:
    document = contract(rules=["plan_then"])
    keys = compile_keys(document)
    assert keys is not None
    refusal(
        lambda: plan_rules(
            contract_document=document,
            expose_id="invoice",
            settings=None,
            related=related_columns(keys.expose("invoice")),
            mapping=None,
            has_fidelity=True,
            strict=False,
            keys=keys.expose("invoice"),
            counter_based=True,
        ),
        "FLX-6106",
    )


# -- the refusals -----------------------------------------------------------------------------------


def test_a_one_to_one_relationship_into_a_history_is_refused() -> None:
    message = refusal(lambda: plans_with(cardinality="one_to_one", rules=["plan_then"]), "FLX-3403")
    assert "one_to_one" in message and EMIT in message


def test_a_counts_entry_on_a_relationship_into_a_history_is_refused() -> None:
    counted = {"invoice": {"sub": CountsEntry(emit="invoice", relationship="sub", min_children=1)}}
    message = refusal(lambda: plans_with(counts=counted, rules=["plan_then"]), "FLX-3403")
    assert "counts" in message


def test_a_required_column_set_from_the_link_is_refused_unless_every_row_finds_a_version() -> None:
    message = refusal(lambda: plans_with(rules=["amount"], amount_required=True), "FLX-3403")
    assert "amount" in message and "asOf" in message
    after_until = {"range": {"min": "2026-01-01", "max": "2026-12-31"}}
    plans_with(rules=["amount"], amount_required=True, invoice_range=after_until)
    # An end can leave an entity without a version on a later date.
    refusal(
        lambda: plans_with(
            held=entry_of(end=HistoryEnd("closeLast", "0.5")),
            rules=["amount"],
            amount_required=True,
            invoice_range=after_until,
        ),
        "FLX-3403",
    )
    # A range that starts before until does not.
    refusal(
        lambda: plans_with(
            rules=["amount"],
            amount_required=True,
            invoice_range={"range": {"min": "2025-12-30", "max": "2026-12-31"}},
        ),
        "FLX-3403",
    )


def test_a_required_column_that_no_rule_sets_from_the_link_is_not_refused() -> None:
    plans_with(rules=["plan_then"], amount_required=True)


def test_a_date_a_rule_sets_from_the_same_link_is_a_cycle() -> None:
    message = refusal(lambda: plans_with(rules=["date_from_sub", "plan_then"]), "FLX-6208")
    assert "cycle" in message


def test_kernel_010_refuses_an_as_of_entry() -> None:
    document = contract(rules=["plan_then"])
    keys = compile_keys(document)
    assert keys is not None
    message = refusal(
        lambda: plan_rules(
            contract_document=document,
            expose_id="invoice",
            settings=None,
            related=related_columns(keys.expose("invoice")),
            mapping=None,
            has_fidelity=True,
            strict=False,
            keys=keys.expose("invoice"),
            counter_based=False,
            asof=(ASOF,),
        ),
        "FLX-3105",
    )
    assert "kernel 0.1.0" in message and "asOf" in message


# -- the bisection ----------------------------------------------------------------------------------


def chain_of(starts: Sequence[int], ends: Sequence[Optional[int]]) -> Chain:
    return Chain(tuple(starts), tuple(ends))


def test_under_nextStart_a_start_belongs_to_its_version_and_an_end_to_the_next() -> None:
    held = entry_of()
    chain = chain_of([10, 20, 30], [20, 30, None])
    kind = LogicalKind.DATE
    found = [version_valid_on(chain, held, kind, day * DAY) for day in (9, 10, 19, 20, 29, 30, 99)]
    assert found == [None, 0, 0, 1, 1, 2, 2]


def test_under_dayBefore_a_date_end_covers_its_whole_day() -> None:
    held = entry_of(chain="dayBefore")
    chain = chain_of([10, 20, 30], [19, 29, None])
    kind = LogicalKind.DATE
    assert [version_valid_on(chain, held, kind, day * DAY) for day in (10, 19, 20, 29, 30)] == [
        0,
        0,
        1,
        1,
        2,
    ]
    # A TIMESTAMP on the last day is still inside the version that ends that day.
    assert version_valid_on(chain, held, kind, 19 * DAY + DAY - 1) == 0
    assert version_valid_on(chain, held, kind, 20 * DAY) == 1


def test_an_ended_chain_has_no_version_after_its_last_closed_end() -> None:
    chain = chain_of([10, 20], [20, 40])
    held = entry_of()
    assert version_valid_on(chain, held, LogicalKind.DATE, 40 * DAY) is None
    assert version_valid_on(chain, held, LogicalKind.DATE, 39 * DAY) == 1


def test_a_date_is_its_midnight_and_a_timestamp_its_milliseconds() -> None:
    assert instant_ms(date(1970, 1, 3)) == 2 * DAY
    assert instant_ms(2 * DAY + 5) == 2 * DAY + 5
    chain = chain_of([5 * DAY, 9 * DAY], [9 * DAY, None])
    held = entry_of()
    stamp = LogicalKind.TIMESTAMP
    assert version_valid_on(chain, held, stamp, 9 * DAY - 1) == 0
    assert version_valid_on(chain, held, stamp, 9 * DAY) == 1
    # A DATE read against a TIMESTAMP history is its midnight UTC.
    assert version_valid_on(chain, held, stamp, instant_ms(date(1970, 1, 10))) == 1


# -- two paths, one answer --------------------------------------------------------------------------


def test_the_column_path_is_the_per_row_path(monkeypatch: pytest.MonkeyPatch) -> None:
    plans = plans_with(rules=["plan_then", "region_then", "price_then", "amount"])
    root = root_for()
    for expose_id in (EMIT, "invoice"):
        plan = plans[expose_id]
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda plan=plan: generate_rows(plan, root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken and all(taken), expose_id


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (7, 40), (119, 120), (60, 90)])
def test_a_slice_is_the_whole_tables_slice(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    plans = plans_with(rules=["plan_then", "price_then"])
    plan = plans["invoice"]
    root = root_for()
    whole = generate_rows(plan, root=root)
    assert generate_row_slice(plan, start, stop, root=root) == whole[start:stop]
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    assert generate_rows(plan, root=root) == whole


def test_the_read_of_a_chain_is_the_chain_the_column_path_writes() -> None:
    # The versions a read finds are bisected over the per-row chain, and the rows it reads are the
    # column path's: they join to the same version for every invoice.
    plans = plans_with(rules=["plan_then"])
    root = root_for()
    plan = plans[EMIT]
    written = table(plan, generate_rows(plan, root=root))
    by_entity: Dict[Any, List[Dict[str, Any]]] = {}
    for row in written:
        by_entity.setdefault(row["sub_id"], []).append(row)
    for invoice in table(plans["invoice"], generate_rows(plans["invoice"], root=root)):
        expected = valid_row(by_entity[invoice["i_sub"]], invoice["i_date"], "nextStart")
        assert invoice["plan_then"] == (None if expected is None else expected["plan"])


def test_the_plan_of_the_history_is_in_the_readers_digest_and_a_plan_without_an_entry_is_unchanged() -> (
    None
):
    with_entry = plans_with(rules=["plan_then"])
    other = plans_with(rules=["plan_then"], held=entry_of(until="2025-06-30"))
    assert "asOf" in with_entry["invoice"].rules.as_document()  # type: ignore[union-attr]
    reads = dict(with_entry["invoice"].reads)
    assert EMIT in reads
    assert with_entry["invoice"].digest() != other["invoice"].digest()
    bare = plans_with(rules=["region_then"], asof=())
    assert "asOf" not in bare["invoice"].rules.as_document()  # type: ignore[union-attr]
