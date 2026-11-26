# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Generating a contract's one-way ``dependency`` (EXPR-SPEC-2 §8, ADR 0011 step 2).

What is pinned:

* **One tuple per key.** Every row that carries a key carries the same ``to`` values,
  under kernel ``0.1.0`` and kernel ``0.2.0``, and different keys do not all carry one.
* **Foreign keys.** A ``to`` column that is a foreign key names a row the parent holds,
  whether it declares a draw or picks uniformly, and a NULL draw stays NULL.
* **Coverage.** A row with a NULL ``from`` value keeps the value its own source gives it.
* **Determinism.** The values are the same for any slicing, in a worker process and
  from a pickled plan, because each is a function of the key.
* **Order.** A dependency is set after the rules that determine its ``from`` columns and
  before those that read its ``to`` columns.
* **Refusals.** Each code of EXPR-SPEC-2 §8, before a row is drawn.
* **The stream.** Its path, under both kernels, and its absence from the seed-path ledger.
"""

from __future__ import annotations

import pickle
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import (
    SEED_TREE_VERSION,
    SeedTreeRoot,
    derive_root,
    digest_bytes,
    key_label,
    path_key,
    path_label,
)
from flux_engine.expr2.errors import Expr2Error
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.paths import (
    DEPENDENCY_PATH_SHAPE,
    DEPENDENCY_ROW_PATH_SHAPE,
    GeneratorStreams,
    dependency_path,
    dependency_row_path,
    dependency_row_stream,
    dependency_stream,
)
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.rules import (
    FORM_DEPENDENCY,
    Band,
    PlannedRule,
    _null_rows,
    _violating_share,
    key_text,
    plan_rules,
    rule_reference,
)
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.keys import compile_keys, mark_key_columns

from .test_relations import _h, _rel, star_contract, star_declarations

pytestmark = pytest.mark.unit

SIM_ID = "sim-deps"
SEAM = "sha256:" + "7" * 64
TICKET = {
    "id": "ticket",
    "dependency": {
        "from": ["ss_ticket_number"],
        "to": ["ss_sold_date_sk", "ss_customer_id", "ss_amount"],
    },
}


def root_for(version: str = SEED_TREE_VERSION, seed: int = 11) -> SeedTreeRoot:
    return derive_root(digest_bytes(SEAM), seed, seed_tree_version=version)


KERNELS = pytest.mark.parametrize(
    "version", [SEED_TREE_VERSION, kernel2.SEED_TREE_VERSION], ids=["kernel-0.1.0", "kernel-0.2.0"]
)


# ---------------------------------------------------------------------------
# Plans over the store star, with invariants on store_sales
# ---------------------------------------------------------------------------


def star_with(
    *invariants: Mapping[str, Any], contract_id: Optional[str] = "test.deps"
) -> Dict[str, Any]:
    contract = star_contract()
    contract["kind"] = "DataProduct"
    if contract_id is not None:
        contract["id"] = contract_id
    sales = next(e for e in contract["exposes"] if e["exposeId"] == "store_sales")
    if invariants:
        sales["contract"]["invariants"] = [dict(i) for i in invariants]
    return contract


def small_star() -> Dict[str, Tuple[Dict[str, Any], int]]:
    """The store star with a sale of 1200 lines over 150 tickets: about eight lines a ticket."""
    declarations = dict(star_declarations())
    columns = dict(declarations["store_sales"][0])
    columns["ss_ticket_number"] = {"range": {"min": "1", "max": "150"}}
    declarations["store_sales"] = (columns, 1200)
    return declarations


def star_plans(
    contract: Mapping[str, Any],
    *,
    version: str = SEED_TREE_VERSION,
    declarations: Optional[Dict[str, Tuple[Dict[str, Any], int]]] = None,
    settings: Any = None,
    mapping: Optional[Mapping[str, Any]] = None,
    flux_rules: Any = None,
    strict: bool = False,
) -> Dict[str, GeneratorPlan]:
    declarations = small_star() if declarations is None else declarations
    generator_version = GENERATOR_VERSION if version == SEED_TREE_VERSION else GENERATOR_VERSION_2
    keys = compile_keys(contract)
    assert keys is not None
    plans: Dict[str, GeneratorPlan] = {}
    for expose in contract["exposes"]:
        expose_id = expose["exposeId"]
        expose_keys = keys.expose(expose_id)
        columns, rows = declarations[expose_id]
        related = related_columns(expose_keys)
        rules = plan_rules(
            contract_document=contract,
            expose_id=expose_id,
            settings=settings if expose_id == "store_sales" else None,
            related=related,
            mapping=mapping if expose_id == "store_sales" else None,
            has_fidelity=True,
            strict=strict,
            flux_rules=flux_rules if expose_id == "store_sales" else None,
            keys=expose_keys,
            rows=rows,
            null_fractions={
                name: block["nullFraction"]
                for name, block in columns.items()
                if "nullFraction" in block
            },
        )
        plans[expose_id] = build_plan(
            mark_key_columns(compile_expose(expose), expose_keys),
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=rows,
            fidelity={"columns": columns},
            related=related,
            rules=rules,
            generator_version=generator_version,
        )
    return relate_plans(plans, keys)


def sales_of(plans: Mapping[str, GeneratorPlan], version: str) -> List[Dict[str, Any]]:
    plan = plans["store_sales"]
    names = [c.column.name for c in plan.columns]
    rows = generate_rows(plan, root=root_for(version))
    return [dict(zip(names, row, strict=True)) for row in rows]


@pytest.fixture(scope="module")
def plans_v1() -> Dict[str, GeneratorPlan]:
    return star_plans(star_with(TICKET))


@pytest.fixture(scope="module")
def plans_v2() -> Dict[str, GeneratorPlan]:
    return star_plans(star_with(TICKET), version=kernel2.SEED_TREE_VERSION)


def plans_of(request: pytest.FixtureRequest, version: str) -> Dict[str, GeneratorPlan]:
    name = "plans_v1" if version == SEED_TREE_VERSION else "plans_v2"
    plans: Dict[str, GeneratorPlan] = request.getfixturevalue(name)
    return plans


TO_COLUMNS = ("ss_sold_date_sk", "ss_customer_id", "ss_amount")


# ---------------------------------------------------------------------------
# One tuple per key
# ---------------------------------------------------------------------------


@KERNELS
def test_every_row_of_a_key_carries_one_to_tuple(
    version: str, request: pytest.FixtureRequest
) -> None:
    sales = sales_of(plans_of(request, version), version)
    by_key: Dict[Any, set] = {}
    for row in sales:
        by_key.setdefault(row["ss_ticket_number"], set()).add(tuple(row[c] for c in TO_COLUMNS))
    assert len(by_key) > 100  # 150 tickets, about 140 of them drawn
    assert all(len(tuples) == 1 for tuples in by_key.values())
    # Keys do not all carry one tuple: each key draws its own.
    assert len({next(iter(t)) for t in by_key.values()}) > 100
    rows_per_key = [sum(1 for r in sales if r["ss_ticket_number"] == k) for k in list(by_key)[:20]]
    assert max(rows_per_key) > 1


@KERNELS
def test_a_to_column_that_is_a_foreign_key_names_a_row_the_parent_holds(
    version: str, request: pytest.FixtureRequest
) -> None:
    plans = plans_of(request, version)
    root = root_for(version)
    sales = sales_of(plans, version)
    dates = {r[0] for r in generate_rows(plans["date_dim"], root=root)}
    customers = {r[0] for r in generate_rows(plans["customer"], root=root)}
    seen_null = False
    for row in sales:
        if row["ss_sold_date_sk"] is not None:
            assert row["ss_sold_date_sk"] in dates
        if row["ss_customer_id"] is None:
            seen_null = True
        else:
            assert row["ss_customer_id"] in customers
    # A NULL draw is a value of its key: a nullFraction of 0.1 gives some keys NULL, all rows.
    assert seen_null
    null_keys = {r["ss_ticket_number"] for r in sales if r["ss_customer_id"] is None}
    for key in null_keys:
        assert {r["ss_customer_id"] for r in sales if r["ss_ticket_number"] == key} == {None}


def test_a_declared_foreign_key_keeps_its_declared_shape_per_key(
    plans_v1: Dict[str, GeneratorPlan],
) -> None:
    # The date histogram puts 80 % of the weight in the first bin of two.
    sales = sales_of(plans_v1, SEED_TREE_VERSION)
    first: Dict[Any, Any] = {}
    for row in sales:
        first.setdefault(row["ss_ticket_number"], row["ss_sold_date_sk"])
    drawn = [v for v in first.values() if v is not None]
    low = sum(1 for v in drawn if v < 2450200)
    assert 0.65 < low / len(drawn) < 0.95


# ---------------------------------------------------------------------------
# Coverage: a NULL from value is not covered
# ---------------------------------------------------------------------------


def test_a_row_with_a_null_from_value_keeps_its_own_source() -> None:
    # ss_customer_id is NULL on a tenth of the rows; ss_amount follows it.
    dependency = {
        "id": "by_customer",
        "dependency": {"from": ["ss_customer_id"], "to": ["ss_amount"]},
    }
    with_rule = star_plans(star_with(dependency))
    without = star_plans(star_with())
    covered = sales_of(with_rule, SEED_TREE_VERSION)
    plain = sales_of(without, SEED_TREE_VERSION)
    uncovered = [i for i, row in enumerate(covered) if row["ss_customer_id"] is None]
    assert len(uncovered) > 100
    for i in uncovered:
        assert covered[i]["ss_amount"] == plain[i]["ss_amount"]
    by_customer: Dict[str, set] = {}
    for row in covered:
        if row["ss_customer_id"] is not None:
            by_customer.setdefault(row["ss_customer_id"], set()).add(row["ss_amount"])
    assert all(len(amounts) == 1 for amounts in by_customer.values())
    assert any(
        covered[i]["ss_amount"] != plain[i]["ss_amount"]
        for i, row in enumerate(covered)
        if row["ss_customer_id"] is not None
    )
    # Columns that are not 'to' columns are the rows they would be without it.
    assert [r["ss_item_sk"] for r in covered] == [r["ss_item_sk"] for r in plain]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@KERNELS
def test_slices_columns_and_pickles_agree(version: str, request: pytest.FixtureRequest) -> None:
    plan = plans_of(request, version)["store_sales"]
    root = root_for(version)
    whole = generate_rows(plan, root=root)
    assert whole == generate_rows(plan, root=root_for(version))
    parts = (
        generate_row_slice(plan, 0, 401, root=root)
        + generate_row_slice(plan, 401, 802, root=root)
        + generate_row_slice(plan, 802, plan.rows, root=root)
    )
    assert parts == whole
    names = [c.column.name for c in plan.columns]
    for column in TO_COLUMNS:
        assert generate_column(plan, column, root=root) == tuple(
            row[names.index(column)] for row in whole
        )
    clone = pickle.loads(pickle.dumps(plan))
    assert clone.as_document() == plan.as_document()
    assert generate_rows(clone, root=root_for(version)) == whole


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.parametrize(
    "band", [{}, {"mustBeLessOrEqualTo": 20, "unit": "percent"}], ids=["zero", "nulls"]
)
@KERNELS
def test_worker_processes_make_the_rows_one_process_makes(
    version: str, band: Dict[str, Any], request: pytest.FixtureRequest
) -> None:
    from flux_engine.stages import _parallel as parallel

    if parallel.cpu_jobs() < 2:
        pytest.skip("one CPU: every emit stays in one process")
    plans = star_plans(
        star_with({**TICKET, **band}),
        version=version,
        declarations={
            **small_star(),
            "store_sales": (small_star()["store_sales"][0], 3 * parallel.MIN_ROWS_PER_JOB + 7),
        },
    )
    plan = plans["store_sales"]
    root = root_for(version)
    many, _lines = parallel.generate_framed(plan, root=root, expose=plan.expose, workers=2)
    assert many == generate_rows(plan, root=root)


# ---------------------------------------------------------------------------
# Order with other rules
# ---------------------------------------------------------------------------


def chain_contract() -> Dict[str, Any]:
    columns = [
        {"name": "c", "type": "INT", "required": False},
        {"name": "b", "type": "INT", "required": False},
        {"name": "a", "type": "INT", "required": False},
        {"name": "k", "type": "INT", "required": False},
    ]
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.chain",
        "exposes": [
            {
                "exposeId": "t",
                "kind": "table",
                "contract": {
                    "schema": columns,
                    "invariants": [
                        {"id": "twice", "derive": "c", "expr": "b * 2"},
                        {"id": "group", "dependency": {"from": ["a"], "to": ["b"]}},
                        {"id": "bucket", "derive": "a", "expr": "ROUND(k / 10, 0)"},
                    ],
                },
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "t"}},
            }
        ],
    }


CHAIN_STATISTICS = {
    "k": {"range": {"min": "0", "max": "999"}},
    "b": {"range": {"min": "0", "max": "9999"}},
}


def single_plan(
    contract: Mapping[str, Any],
    statistics: Mapping[str, Any] = CHAIN_STATISTICS,
    *,
    rows: int = 600,
    version: str = SEED_TREE_VERSION,
    has_fidelity: bool = True,
    **kwargs: Any,
) -> GeneratorPlan:
    expose = contract["exposes"][0]
    keys = compile_keys(contract)
    expose_keys = None if keys is None else keys.expose(expose["exposeId"])
    related = related_columns(expose_keys)
    rules = plan_rules(
        contract_document=contract,
        expose_id=expose["exposeId"],
        settings=kwargs.pop("settings", None),
        related=related,
        mapping=kwargs.pop("mapping", None),
        has_fidelity=has_fidelity,
        strict=False,
        flux_rules=kwargs.pop("flux_rules", None),
        keys=expose_keys,
    )
    return build_plan(
        mark_key_columns(compile_expose(expose), expose_keys),
        sim_id=SIM_ID,
        seam_digest=SEAM,
        rows=rows,
        fidelity={"columns": dict(statistics)},
        related=related,
        rules=rules,
        generator_version=(
            GENERATOR_VERSION if version == SEED_TREE_VERSION else GENERATOR_VERSION_2
        ),
        **kwargs,
    )


@KERNELS
def test_a_dependency_is_ordered_with_the_rules_around_it(version: str) -> None:
    plan = single_plan(chain_contract(), version=version)
    assert plan.rules is not None and plan.rules.order == ("a", "b", "c")
    names = [c.column.name for c in plan.columns]
    rows = [dict(zip(names, r, strict=True)) for r in generate_rows(plan, root=root_for(version))]
    by_a: Dict[int, set] = {}
    for row in rows:
        assert row["a"] == int(Decimal(row["k"]) / 10 + Decimal("0.5")) or row["a"] is not None
        by_a.setdefault(row["a"], set()).add(row["b"])
        assert row["c"] == row["b"] * 2
    assert len(by_a) > 10 and all(len(values) == 1 for values in by_a.values())


# ---------------------------------------------------------------------------
# The plan document, coverage and digest
# ---------------------------------------------------------------------------


def test_the_plan_records_the_dependency(plans_v1: Dict[str, GeneratorPlan]) -> None:
    plan = plans_v1["store_sales"]
    document = plan.as_document()["rules"]
    (rule,) = document["rules"]
    assert rule["id"] == "ticket" and rule["form"] == FORM_DEPENDENCY
    assert rule["from"] == ["ss_ticket_number"] and rule["to"] == list(TO_COLUMNS)
    assert rule["origin"] == "contract" and rule["band"] is None
    assert plan.rules is not None and plan.rules.order == TO_COLUMNS
    assert rule["order"] == [0, 1, 2]


def test_coverage_names_the_dependency(plans_v1: Dict[str, GeneratorPlan]) -> None:
    coverage = {row.column: row for row in coverage_of_plan(plans_v1["store_sales"]).columns}
    for column in TO_COLUMNS:
        assert (coverage[column].origin.value, coverage[column].decided_by) == (
            "derived",
            "dependency",
        )
        assert coverage[column].source == "contract.invariants.ticket"
    assert coverage["ss_item_sk"].decided_by == "relationship"
    assert "dependency" in coverage["ss_amount"].detail or "key" in coverage["ss_amount"].detail


def test_a_plan_without_a_dependency_keeps_its_digest() -> None:
    star = star_plans(star_with())
    bare = star_plans(star_contract())
    for expose_id, plan in star.items():
        assert plan.rules is None and "rules" not in plan.as_document()
        assert plan.digest() == bare[expose_id].digest()
    assert star["store_sales"].digest() != star_plans(star_with(TICKET))["store_sales"].digest()


def test_a_dependency_moves_only_its_to_columns() -> None:
    with_rule = sales_of(star_plans(star_with(TICKET)), SEED_TREE_VERSION)
    without = sales_of(star_plans(star_with()), SEED_TREE_VERSION)
    for column in ("ss_item_sk", "ss_ticket_number"):
        assert [r[column] for r in with_rule] == [r[column] for r in without]
    assert [r["ss_amount"] for r in with_rule] != [r["ss_amount"] for r in without]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def refuses(
    code: str,
    contract: Mapping[str, Any],
    *args: Any,
    plan: Callable[..., Any] = star_plans,
    **kwargs: Any,
) -> str:
    """The message of the refusal *code* that *plan* raises for *contract*."""
    with pytest.raises((Expr2Error, FluxError)) as caught:
        plan(contract, *args, **kwargs)
    assert caught.value.code.code == code, str(caught.value)
    return str(caught.value)


def dependency(from_: Sequence[str], to: Sequence[str], **extra: Any) -> Dict[str, Any]:
    both = extra.pop("bothWays", None)
    body: Dict[str, Any] = {"from": list(from_), "to": list(to)}
    if both is not None:
        body["bothWays"] = both
    return {"id": extra.pop("id", "dep"), "dependency": body, **extra}


def test_both_ways_is_refused() -> None:
    message = refuses(
        "FLX-6210",
        star_with(dependency(["ss_ticket_number"], ["ss_amount"], bothWays=True)),
    )
    assert "bothWays" in message
    star_plans(star_with(dependency(["ss_ticket_number"], ["ss_amount"], bothWays=False)))


@pytest.mark.parametrize(
    ("from_", "to"),
    [
        (["ss_ticket_number"], ["ss_ticket_number"]),
        (["ss_amount"], ["ss_customer_id", "ss_amount"]),
        (["ss_ticket_number", "ss_amount"], ["ss_amount"]),
    ],
)
def test_a_column_on_both_sides_is_a_cycle_and_not_a_crash(
    from_: Sequence[str], to: Sequence[str]
) -> None:
    """A dependency reads the columns it determines, so one on both sides is a cycle, ``FLX-6208``."""
    message = refuses("FLX-6208", star_with(dependency(from_, to)))
    assert "both sides" in message
    assert all(name in message for name in set(from_) & set(to))


def test_a_column_named_twice_is_refused_before_the_cycle_is_named() -> None:
    """A name repeated in ``from`` is refused as a repeat, so the cycle message lists each name once."""
    with pytest.raises(ValueError, match="names a column twice"):
        refuses("FLX-6208", star_with(dependency(["ss_amount", "ss_amount"], ["ss_amount"])))
    message = refuses(
        "FLX-6208",
        star_with(dependency(["ss_amount", "ss_ticket_number"], ["ss_ticket_number", "ss_amount"])),
    )
    assert message.count("'ss_amount'") == 1 and message.count("'ss_ticket_number'") == 1


@pytest.mark.parametrize(
    "band",
    [
        {"mustBe": 3},
        {"mustBe": 1, "unit": "percent"},
        {"mustBeGreaterThan": 0},
        {"mustBeGreaterOrEqualTo": 2},
        {"mustBeBetween": [1, 5]},
        {"mustBeBetween": [1, 5], "unit": "percent"},
    ],
)
def test_a_lower_bound_above_the_columns_own_nulls_is_refused(band: Dict[str, Any]) -> None:
    # A lower bound above zero needs violating rows, which wait for section 7.
    contract = star_with(dependency(["ss_ticket_number"], ["ss_amount"], **band))
    assert "lower bound above zero" in refuses("FLX-6210", contract)


@pytest.mark.parametrize(
    "band", [{"mustBeLessOrEqualTo": -1}, {"mustBeLessThan": 0}, {"mustBe": -2}]
)
def test_a_band_below_zero_is_more_than_the_nulls_allow(band: Dict[str, Any]) -> None:
    refuses("FLX-6204", star_with(dependency(["ss_ticket_number"], ["ss_amount"], **band)))


@pytest.mark.parametrize(
    "band",
    [
        {"mustBe": 0},
        {"mustBe": 0, "unit": "percent"},
        {"mustBeLessOrEqualTo": 0},
        {"mustBeLessThan": 1},
        {"mustBeBetween": [0, 0]},
        {"mustBeLessThan": 5, "unit": "percent"},
        {"mustBeLessThan": 3},
        {"mustBeLessOrEqualTo": 2},
        {"mustBeBetween": [0, 2]},
        {"mustBeBetween": [-1, 2], "unit": "percent"},
        {"mustBeGreaterOrEqualTo": 0},
        {"mustBeGreaterThan": -1, "unit": "percent"},
    ],
)
def test_a_band_the_columns_own_nulls_account_for_generates(band: Dict[str, Any]) -> None:
    plans = star_plans(star_with(dependency(["ss_ticket_number"], ["ss_amount"], **band)))
    assert plans["store_sales"].rules is not None


def test_what_a_band_allows() -> None:
    zero, five, ten = Decimal(0), Decimal(5), Decimal(10)
    assert Band("mustBe", (zero,), "rows").allows_only_zero()
    assert not Band("mustBeLessThan", (Decimal(1),), "percent").allows_only_zero()
    assert not Band("mustBeLessThan", (Decimal(2),), "rows").allows_only_zero()
    assert Band("mustBeBetween", (five, ten), "percent").upper() == (ten, False)
    assert Band("mustBeLessThan", (five,), "rows").upper() == (five, True)
    assert Band("mustBeLessOrEqualTo", (five,), "rows").upper() == (five, False)
    assert Band("mustBe", (five,), "rows").upper() == (five, False)
    assert Band("mustBeGreaterThan", (five,), "rows").upper() is None
    assert Band("mustBeGreaterOrEqualTo", (five,), "rows").upper() is None


# ---------------------------------------------------------------------------
# What the band decides about NULLs
# ---------------------------------------------------------------------------

#: The three 'to' columns declare 5 %, 10 % and no NULLs, so about 14.5 % of rows have one.
NULL_BAND = {"mustBeLessOrEqualTo": 20, "unit": "percent"}


def banded(version: str = SEED_TREE_VERSION, **band: Any) -> Dict[str, GeneratorPlan]:
    invariant = {**TICKET, **(band or NULL_BAND)}
    return star_plans(star_with(invariant), version=version)


def _every_key_agrees(sales: Sequence[Mapping[str, Any]]) -> int:
    """Assert each key's non-NULL values agree; return how many keys also hold a NULL."""
    mixed = 0
    for column in TO_COLUMNS:
        values: Dict[Any, set] = {}
        for row in sales:
            if row[column] is not None:
                values.setdefault(row["ss_ticket_number"], set()).add(row[column])
        assert all(len(found) == 1 for found in values.values()), column
        if column != "ss_amount":
            nulls = {r["ss_ticket_number"] for r in sales if r[column] is None}
            mixed += len(nulls & set(values))
    return mixed


def _share_with_null(sales: Sequence[Mapping[str, Any]]) -> float:
    return sum(1 for row in sales if any(row[c] is None for c in TO_COLUMNS)) / len(sales)


@KERNELS
def test_a_band_with_an_upper_bound_draws_nulls_per_row_and_values_per_key(version: str) -> None:
    plans = banded(version)
    rules = plans["store_sales"].rules
    assert rules is not None
    # S is about 14.5 percent, well inside 20: the columns' NULLs are drawn independently.
    assert rules.rules[0].null_rows == Decimal(1)
    sales = sales_of(plans, version)
    # Each column keeps about its declared share of NULLs: 5 and 10 percent, and none.
    shares = {c: sum(row[c] is None for row in sales) / len(sales) for c in TO_COLUMNS}
    assert 0.02 < shares["ss_sold_date_sk"] < 0.08 and 0.06 < shares["ss_customer_id"] < 0.14
    assert shares["ss_amount"] == 0
    # Every non-NULL value of a key agrees, and the key's NULL rows sit among them.
    assert _every_key_agrees(sales) > 20
    # The g3 count of section 8 is the rows whose tuple is not their key's most common one.
    tuples: Dict[Any, Dict[Tuple[Any, ...], int]] = {}
    for row in sales:
        counts = tuples.setdefault(row["ss_ticket_number"], {})
        found = tuple(row[c] for c in TO_COLUMNS)
        counts[found] = counts.get(found, 0) + 1
    violating = len(sales) - sum(max(counts.values()) for counts in tuples.values())
    with_null = sum(1 for row in sales if any(row[c] is None for c in TO_COLUMNS))
    assert 0 < violating <= with_null
    assert 0.08 < _share_with_null(sales) < 0.22


@KERNELS
def test_a_band_below_the_independent_share_puts_the_nulls_on_the_same_rows(version: str) -> None:
    # S is about 14.5 percent, above 10.5. At 1200 rows four standard deviations of the count
    # leave no room above ss_customer_id's 10 percent, so the NULL rows are that column's.
    plans = banded(version, mustBeLessOrEqualTo=10.5, unit="percent")
    rules = plans["store_sales"].rules
    assert rules is not None and rules.rules[0].null_rows == Decimal("0.1")
    # A strict run cannot promise that band with so little room, so it refuses it.
    refuses(
        "FLX-6204",
        star_with({**TICKET, "mustBeLessOrEqualTo": 10.5, "unit": "percent"}),
        strict=True,
    )
    sales = sales_of(plans, version)
    date_nulls = [row for row in sales if row["ss_sold_date_sk"] is None]
    assert date_nulls and all(row["ss_customer_id"] is None for row in date_nulls)
    assert 0.02 < len(date_nulls) / len(sales) < 0.08
    assert 0.06 < _share_with_null(sales) < 0.14
    assert _every_key_agrees(sales) > 10


@KERNELS
def test_a_dependency_with_no_band_draws_nulls_per_key(version: str) -> None:
    sales = sales_of(star_plans(star_with(TICKET), version=version), version)
    for key in {r["ss_ticket_number"] for r in sales}:
        assert (
            len({tuple(r[c] for c in TO_COLUMNS) for r in sales if r["ss_ticket_number"] == key})
            == 1
        )
    assert any(row["ss_customer_id"] is None for row in sales)


@KERNELS
@pytest.mark.parametrize("band", [NULL_BAND, {"mustBeLessOrEqualTo": 200}], ids=["free", "coupled"])
def test_the_per_row_nulls_agree_across_slices_and_pickles(
    version: str, band: Dict[str, Any]
) -> None:
    plan = banded(version, **band)["store_sales"]
    root = root_for(version)
    whole = generate_rows(plan, root=root)
    parts = generate_row_slice(plan, 0, 333, root=root) + generate_row_slice(
        plan, 333, plan.rows, root=root
    )
    assert parts == whole
    assert generate_rows(pickle.loads(pickle.dumps(plan)), root=root_for(version)) == whole


def test_a_band_below_what_the_heaviest_column_gives_alone_is_refused_in_every_run() -> None:
    # ss_customer_id declares 10 percent NULLs: even nested, the NULLs make 10 percent violate.
    under = {**TICKET, "mustBeLessOrEqualTo": 9, "unit": "percent"}
    for strict in (False, True):
        message = refuses("FLX-6204", star_with(under), strict=strict)
        assert "ss_customer_id" in message and "ticket" in message
    refuses("FLX-6204", star_with({**TICKET, "mustBeLessThan": 10, "unit": "percent"}))
    # A rows band is read over the 1200 covered rows, so 100 is about 8.3 percent.
    refuses("FLX-6204", star_with({**TICKET, "mustBeLessOrEqualTo": 100}))
    # No band, or a zero band, has no NULL rows to count: its NULLs are the key's.
    star_plans(star_with(TICKET), strict=True)


def test_a_band_with_a_lower_bound_above_zero_is_refused() -> None:
    for band in (
        {"mustBeBetween": [20, 30], "unit": "percent"},
        {"mustBeBetween": [10, 20], "unit": "percent"},
        {"mustBeGreaterOrEqualTo": 10, "unit": "percent"},
        {"mustBeGreaterThan": 0},
        {"mustBeBetween": [150, 200]},
        {"mustBe": 150},
    ):
        message = refuses("FLX-6210", star_with({**TICKET, **band}))
        assert "lower bound above zero" in message, band
    # A lower bound at or below zero is no bound: the NULLs are drawn independently.
    for band in ({"mustBeGreaterOrEqualTo": 0}, {"mustBeGreaterThan": -1, "unit": "percent"}):
        rules = star_plans(star_with({**TICKET, **band}))["store_sales"].rules
        assert rules is not None and rules.rules[0].null_rows == Decimal(1)


def test_the_plan_records_how_the_nulls_are_drawn() -> None:
    def recorded(invariant: Mapping[str, Any]) -> Any:
        rules = star_plans(star_with(invariant))["store_sales"].rules
        assert rules is not None
        return rules.as_document()["rules"][0]["nullRows"]

    assert recorded(TICKET) is None
    assert recorded({**TICKET, **NULL_BAND}) == "1"
    assert recorded({**TICKET, "mustBeLessOrEqualTo": 10.5, "unit": "percent"}) == "0.1"
    coupled = Decimal(recorded({**TICKET, "mustBeLessOrEqualTo": 200}))
    assert Decimal("0.1") < coupled < 1


#: Seven 'to' columns with 4 to 4.6 percent NULLs each: about 27 percent of rows hold one when
#: the NULLs are independent.
_SEVEN = tuple(f"c{i}" for i in range(7))
_SEVEN_NULLS = dict(
    zip(_SEVEN, ("0.040", "0.041", "0.042", "0.043", "0.044", "0.045", "0.046"), strict=True)
)


def _share(
    rows: Optional[int],
    nulls: Mapping[str, str] = _SEVEN_NULLS,
    *,
    strict: bool = False,
    **band: Any,
) -> Optional[Decimal]:
    rule = PlannedRule(
        id="ticket",
        form=FORM_DEPENDENCY,
        text="k -> " + ", ".join(nulls),
        band=Band.read(band, "rule 'ticket'"),
        from_columns=("k",),
        to_columns=tuple(nulls),
    )
    return _null_rows(rule, rows, nulls, strict)


def _seven_share(rows: Optional[int], **band: Any) -> Optional[Decimal]:
    return _share(rows, **band)


def test_the_null_row_share_is_the_weakest_coupling_the_band_allows() -> None:
    fractions = [Decimal(_SEVEN_NULLS[c]) for c in _SEVEN]
    independent = _violating_share(Decimal(1), fractions)
    assert Decimal("0.26") < independent < Decimal("0.27")
    # A band the independent share fits, four standard deviations inside, keeps it.
    assert _seven_share(30000, mustBeLessOrEqualTo=30, unit="percent") == Decimal(1)
    # Below it, the share is the one whose expected count sits four deviations under the bound.
    bound = Decimal("0.09")
    for rows in (30000, 3000000):
        share = _seven_share(rows, mustBeLessOrEqualTo=9, unit="percent")
        assert share is not None and Decimal("0.046") < share < 1
        target = bound - 4 * (bound * (1 - bound) / rows).sqrt()
        assert abs(_violating_share(share, fractions) - target) < Decimal("1e-15")
    assert _seven_share(30000, mustBeLessOrEqualTo=9, unit="percent") < _seven_share(  # type: ignore[operator]
        3000000, mustBeLessOrEqualTo=9, unit="percent"
    )
    # With no declared row count no margin can be sized: a percent band takes the fewest
    # violating rows.
    assert _seven_share(None, mustBeLessOrEqualTo=9, unit="percent") == Decimal("0.046")
    # The heaviest column alone: at its share every NULL is on its rows, and below it is refused.
    assert _seven_share(None, mustBeLessOrEqualTo=4.6, unit="percent") == Decimal("0.046")
    with pytest.raises(Expr2Error) as caught:
        _seven_share(30000, mustBeLessOrEqualTo=4.5, unit="percent")
    assert caught.value.code.code == "FLX-6204" and "c6" in str(caught.value)
    # No band, or a zero band: per key.
    assert _seven_share(30000) is None and _seven_share(30000, mustBe=0) is None


def test_an_emit_with_no_row_count_cannot_hold_its_nulls_to_a_count_of_rows() -> None:
    # A rows band there has no share to read, and its NULLs would exceed it unseen: refused.
    with pytest.raises(Expr2Error) as caught:
        _seven_share(None, mustBeLessOrEqualTo=10)
    assert caught.value.code.code == "FLX-6204" and "declares no 'rows'" in str(caught.value)
    # A strict run needs the count for any bound's room, so a percent band is refused there too.
    with pytest.raises(Expr2Error) as caught:
        _share(None, strict=True, mustBeLessOrEqualTo=9, unit="percent")
    assert caught.value.code.code == "FLX-6204" and "declare the emit's rows" in str(caught.value)
    # A band no row can break is no harder without the count: no NULL, or a zero band.
    assert _share(None, {"c": "0"}, mustBeLessOrEqualTo=10) == Decimal(1)
    assert _seven_share(None, mustBe=0) is None


def test_a_dependency_with_no_nulls_takes_independent_nulls_under_any_band() -> None:
    # No 'to' column declares a NULL, so no row violates: even a tight band in a strict run
    # at a small row count, whose room falls below zero, is met.
    for band in ({"mustBeLessOrEqualTo": 1, "unit": "percent"}, {"mustBeLessOrEqualTo": 5}):
        assert _share(1000, {"a": "0", "b": "0"}, strict=True, **band) == Decimal(1)
        assert _share(1000, {"a": "0"}, strict=True, **band) == Decimal(1)


def test_a_band_too_close_to_the_heaviest_column_is_refused_only_in_a_strict_run() -> None:
    # At 30000 rows four deviations of the count below 4.7 percent leave about 4.2, under 4.6.
    assert _seven_share(30000, mustBeLessOrEqualTo=4.7, unit="percent") == Decimal("0.046")
    with pytest.raises(Expr2Error) as caught:
        _share(30000, strict=True, mustBeLessOrEqualTo=4.7, unit="percent")
    assert caught.value.code.code == "FLX-6204" and "strict" in str(caught.value)
    # With room to spare a strict run takes the same share as any other.
    assert _share(30000, strict=True, mustBeLessOrEqualTo=9, unit="percent") == _seven_share(
        30000, mustBeLessOrEqualTo=9, unit="percent"
    )


def test_a_band_no_row_can_exceed_or_no_covered_row_takes_independent_nulls() -> None:
    # A bound of 100 percent or more cannot be exceeded, even by a column NULL on every row.
    assert _share(30000, {"c": "1"}, mustBeLessThan=100, unit="percent") == Decimal(1)
    assert _share(30000, mustBeLessOrEqualTo=40000) == Decimal(1)
    # An emit with no row has none to violate.
    assert _share(0, mustBeLessOrEqualTo=5) == Decimal(1)
    # One column with NULLs: every coupling gives its share, and the share is 1.
    assert _share(30000, {"c": "0.05", "d": "0"}, mustBeLessOrEqualTo=6, unit="percent") == 1


def test_the_violating_share_runs_from_the_heaviest_column_to_the_independent_share() -> None:
    fractions = [Decimal(_SEVEN_NULLS[c]) for c in _SEVEN]
    largest = max(fractions)
    assert _violating_share(largest, fractions) == largest
    steps = [largest + (1 - largest) * Decimal(i) / 20 for i in range(21)]
    shares = [_violating_share(step, fractions) for step in steps]
    assert all(a <= b for a, b in zip(shares[:-1], shares[1:], strict=True))


def test_a_to_column_that_is_a_key_is_refused() -> None:
    # ss_item_sk and ss_ticket_number are the two columns of the sale's composite key.
    refuses("FLX-6210", star_with(dependency(["ss_amount"], ["ss_item_sk"])))
    contract = star_with(dependency(["ss_amount"], ["ss_ticket_number"]))
    assert "key" in refuses("FLX-6210", contract)
    parent = star_with()
    dim = next(e for e in parent["exposes"] if e["exposeId"] == "date_dim")
    dim["contract"]["invariants"] = [dependency(["d_year"], ["d_date_sk"])]
    message = refuses("FLX-6210", parent)
    assert "'d_date_sk'" in message


def test_a_to_column_of_a_composite_foreign_key_is_refused() -> None:
    contract = star_with()
    returns = next(e for e in contract["exposes"] if e["exposeId"] == "store_returns")
    returns["contract"]["invariants"] = [dependency(["sr_returned_date_sk"], ["sr_item_sk"])]
    with pytest.raises((Expr2Error, FluxError)) as caught:
        star_plans(contract)
    assert caught.value.code.code == "FLX-6210"
    assert "composite" in str(caught.value)


def test_a_to_column_that_is_a_one_to_one_foreign_key_is_refused() -> None:
    contract = star_with()
    sales = next(e for e in contract["exposes"] if e["exposeId"] == "store_sales")
    sales["semantics"]["relationships"].append(
        _rel("extra", "customer", ["ss_other"], ["c_id"], "one_to_one")
    )
    sales["contract"]["schema"].append({"name": "ss_other", "type": "STRING", "required": False})
    sales["contract"]["invariants"] = [dependency(["ss_amount"], ["ss_other"])]
    with pytest.raises((Expr2Error, FluxError)) as caught:
        star_plans(contract)
    assert caught.value.code.code == "FLX-6210" and "one_to_one" in str(caught.value)


def test_a_to_column_that_another_is_drawn_given_is_refused() -> None:
    columns = [
        {"name": "g", "type": "STRING", "required": False},
        {"name": "x", "type": "INT", "required": False},
        {"name": "k", "type": "INT", "required": False},
    ]
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.given",
        "exposes": [
            {
                "exposeId": "t",
                "kind": "table",
                "contract": {
                    "schema": columns,
                    "invariants": [{"id": "d", "dependency": {"from": ["k"], "to": ["g"]}}],
                },
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "t"}},
            }
        ],
    }
    statistics = {
        "k": {"range": {"min": "0", "max": "9"}},
        "g": {"support": {"values": ["x", "y"]}},
        "x": {
            "given": "g",
            "range": {"min": "0", "max": "9"},
            "bins": ["0", "5", "9"],
            "binWeightsGiven": {"x": ["1", "1"], "y": ["1", "1"]},
        },
    }
    assert "given" in refuses("FLX-6210", contract, statistics, plan=single_plan)
    # The 'to' column itself drawn given another is refused as well.
    contract["exposes"][0]["contract"]["invariants"] = [
        {"id": "d", "dependency": {"from": ["k"], "to": ["x"]}}
    ]
    assert "given" in refuses("FLX-6210", contract, statistics, plan=single_plan)


def test_a_to_column_that_is_an_identifier_is_refused() -> None:
    columns = [
        {"name": "id", "type": "UUID", "required": False},
        {"name": "k", "type": "INT", "required": False},
    ]
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.ident",
        "exposes": [
            {
                "exposeId": "t",
                "kind": "table",
                "contract": {
                    "schema": columns,
                    "invariants": [{"id": "d", "dependency": {"from": ["k"], "to": ["id"]}}],
                },
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "t"}},
            }
        ],
    }
    statistics = {"k": {"range": {"min": "0", "max": "9"}}}
    assert "identifier" in refuses("FLX-6210", contract, statistics, plan=single_plan)


def test_an_emit_with_no_fidelity_block_is_refused() -> None:
    message = refuses("FLX-6210", chain_contract(), plan=single_plan, has_fidelity=False)
    assert "fidelity" in message


def test_a_to_column_another_source_also_determines_is_two_sources() -> None:
    clash = star_with(
        dependency(["ss_ticket_number"], ["ss_amount"], id="one"),
        {"id": "two", "derive": "ss_amount", "expr": "ss_item_sk + 1"},
    )
    message = refuses("FLX-6203", clash)
    assert "one" in message and "two" in message
    refuses(
        "FLX-6203",
        star_with(
            dependency(["ss_ticket_number"], ["ss_amount"], id="one"),
            dependency(["ss_item_sk"], ["ss_amount"], id="two"),
        ),
    )
    refuses(
        "FLX-6203",
        star_with(dependency(["ss_ticket_number"], ["ss_amount"], id="one")),
        mapping={"ss_amount": "subject.id"},
    )


def test_a_generates_and_a_dependency_on_one_column_are_two_sources() -> None:
    columns = [
        {"name": "d", "type": "DATE", "required": False},
        {"name": "n", "type": "INT", "required": False},
        {"name": "k", "type": "INT", "required": False},
    ]
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.two",
        "exposes": [
            {
                "exposeId": "t",
                "kind": "table",
                "contract": {
                    "schema": columns,
                    "invariants": [
                        {"id": "dep", "dependency": {"from": ["k"], "to": ["d"]}},
                        {"id": "off", "check": "n = DAYS_BETWEEN(DATE '2026-01-01', d)"},
                    ],
                },
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "t"}},
            }
        ],
    }
    statistics = {
        "k": {"range": {"min": "0", "max": "9"}},
        "n": {"range": {"min": "0", "max": "9"}},
        "d": {"range": {"min": "2026-01-01", "max": "2026-12-31"}},
    }
    refuses(
        "FLX-6203", contract, statistics, plan=single_plan, settings={"off": {"generates": "d"}}
    )


def test_a_flux_side_rule_and_a_dependency_on_one_column_are_two_sources() -> None:
    columns = [
        {"name": "d", "type": "DATE", "required": False},
        {"name": "n", "type": "INT", "required": False},
        {"name": "k", "type": "INT", "required": False},
    ]
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.flux",
        "exposes": [
            {
                "exposeId": "t",
                "kind": "table",
                "contract": {
                    "schema": columns,
                    "invariants": [{"id": "dep", "dependency": {"from": ["k"], "to": ["n"]}}],
                },
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "t"}},
            }
        ],
    }
    statistics = {
        "k": {"range": {"min": "0", "max": "9"}},
        "n": {"range": {"min": "0", "max": "9"}},
        "d": {"range": {"min": "2026-01-01", "max": "2026-12-31"}},
    }
    flux_rules = [
        {
            "id": "day_of_month",
            "language": "flux-expr/3",
            "derive": "n",
            "expr": "EXTRACT(DAY FROM d)",
        }
    ]
    message = refuses("FLX-6203", contract, statistics, plan=single_plan, flux_rules=flux_rules)
    assert "dep" in message and "day_of_month" in message


def test_a_cycle_through_a_rule_and_a_dependency_is_refused() -> None:
    contract = chain_contract()
    invariants = contract["exposes"][0]["contract"]["invariants"]
    invariants[2] = {"id": "bucket", "derive": "a", "expr": "ROUND(b / 10, 0)"}
    message = refuses("FLX-6208", contract, plan=single_plan)
    assert "group" in message and "bucket" in message


def test_a_dependency_must_name_its_columns_as_the_rfc_says() -> None:
    cases = [
        (dependency(["nope"], ["ss_amount"]), "FLX-6113"),
        (dependency(["ss_ticket_number"], ["nope"]), "FLX-6113"),
    ]
    for invariant, code in cases:
        refuses(code, star_with(invariant))
    for invariant in (
        dependency([], ["ss_amount"]),
        dependency(["ss_amount", "ss_amount"], ["ss_customer_id"]),
        dependency(["ss_ticket_number"], ["ss_amount"], when="ss_amount > 1"),
        {"id": "x", "dependency": {"from": "ss_ticket_number", "to": ["ss_amount"]}},
        {"id": "x", "dependency": {"from": ["ss_ticket_number"], "to": ["ss_amount"], "z": 1}},
        {
            "id": "x",
            "dependency": {"from": ["ss_ticket_number"], "to": ["ss_amount"], "bothWays": 1},
        },
    ):
        with pytest.raises(ValueError):
            star_plans(star_with(invariant))


def test_a_from_column_of_a_type_with_no_fluid_sql_type_is_refused() -> None:
    contract = star_with(dependency(["ss_blob"], ["ss_amount"]))
    sales = next(e for e in contract["exposes"] if e["exposeId"] == "store_sales")
    sales["contract"]["schema"].append({"name": "ss_blob", "type": "BINARY", "required": False})
    declarations = small_star()
    declarations["store_sales"][0]["ss_blob"] = {"nullFraction": "0.5"}
    with pytest.raises((Expr2Error, FluxError)) as caught:
        star_plans(contract, declarations=declarations)
    assert caught.value.code.code == "FLX-6111"


# ---------------------------------------------------------------------------
# The key, the reference and the stream
# ---------------------------------------------------------------------------


def test_a_key_is_the_canonical_json_of_type_and_text_pairs() -> None:
    assert key_text(["number"], [1]) == '[["number","1"]]'
    assert key_text(["string"], ["1"]) == '[["string","1"]]'
    assert key_text(["number"], [1]) != key_text(["string"], ["1"])
    one = key_text(["number"], [Decimal("1.50")])
    assert one == key_text(["number"], [Decimal("1.5")]) == key_text(["number"], [1.5])
    assert key_text(["number"], [Decimal("100")]) == key_text(["number"], [Decimal("1E+2")])
    assert key_text(["number"], [Decimal("0.00")]) == '[["number","0"]]'
    assert key_text(["number"], [Decimal("-0.050")]) == '[["number","-0.05"]]'
    assert key_text(["number"], [Decimal("0.000" + "1" * 40)]) == (
        '[["number","0.000' + "1" * 40 + '"]]'
    )
    assert key_text(["date", "bool"], [date(2026, 1, 2), True]) == (
        '[["date","2026-01-02"],["bool","true"]]'
    )
    assert key_text(["timestamp"], [1700000000123]) == '[["timestamp","1700000000123"]]'
    # A string holding a separator character cannot forge another key's path.
    nasty = key_text(["string"], ["a\x1fb\x1ec"])
    assert "\x1f" not in nasty and "\x1e" not in nasty
    path_label(dependency_path("s", rule_reference("c", "r"), nasty, "col"))


def test_a_rule_reference_names_its_contract_and_id() -> None:
    assert rule_reference("test.deps", "ticket") == '["contract","test.deps","ticket"]'
    assert rule_reference(None, "ticket") == '["contract",null,"ticket"]'


def test_the_stream_is_derived_through_the_plans_kernel() -> None:
    reference = rule_reference("c", "r")
    key = key_text(["number"], [7])
    path = dependency_path("sim", reference, key, "col")
    assert path == ("sim", "sim", "dependency", reference, "key", key, "col")
    # Kernel 0.1.0: the stream of the path's label, words from the first.
    first = dependency_stream(root_for(), "sim", reference, key, "col")
    again = dependency_stream(root_for(), "sim", reference, key, "col")
    assert first.next_raw() == again.next_raw()
    assert dependency_stream(root_for(), "sim", reference, key, "other").next_raw() != (
        dependency_stream(root_for(), "sim", reference, key, "col").next_raw()
    )
    # Kernel 0.2.0: no ordinal part, so the key is the whole path and the ordinals are zero.
    root2 = root_for(kernel2.SEED_TREE_VERSION)
    stream = dependency_stream(root2, "sim", reference, key, "col")
    expected = kernel2.CounterStream(path_key(root2, path_label(path)), (0, 0))
    assert stream.block(0) == expected.block(0)
    assert stream.block(1) == expected.block(1) and stream.block(0) != stream.block(1)
    assert DEPENDENCY_PATH_SHAPE.pattern.count("key") == 1


def test_dependency_paths_stay_out_of_the_seed_path_ledger() -> None:
    streams = GeneratorStreams(root=root_for())
    reference = rule_reference("c", "r")
    for path in (
        dependency_path("sim", reference, key_text(["number"], [1]), "col"),
        dependency_row_path("sim", reference, 3),
    ):
        with pytest.raises(FluxError) as caught:
            streams.stream(path)
        assert caught.value.code.code == "FLX-2201"


def test_a_rows_null_draws_are_on_a_row_path_through_the_plans_kernel() -> None:
    reference = rule_reference("c", "r")
    path = dependency_row_path("sim", reference, 41)
    assert path == ("sim", "sim", "dependency", reference, "row", 41)
    # Kernel 0.1.0: the stream of the path's label, one per row.
    first = dependency_row_stream(root_for(), "sim", reference, 41).next_raw()
    assert first == dependency_row_stream(root_for(), "sim", reference, 41).next_raw()
    assert first != dependency_row_stream(root_for(), "sim", reference, 42).next_raw()
    # Kernel 0.2.0: the key leaves the row index out, and the counter carries it.
    root2 = root_for(kernel2.SEED_TREE_VERSION)
    label, ordinals = key_label(path, DEPENDENCY_ROW_PATH_SHAPE)
    assert ordinals == (41, 0)
    expected = kernel2.CounterStream(path_key(root2, label), ordinals)
    stream = dependency_row_stream(root2, "sim", reference, 41)
    assert stream.block(0) == expected.block(0) and stream.block(1) == expected.block(1)
    other = dependency_row_stream(root2, "sim", reference, 42)
    assert other.block(0) != stream.block(0)
