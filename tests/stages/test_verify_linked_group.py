# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A property grouped by a link is checked group by group (ADR 0023 section 6).

The invoices of ``test_given_across_link_run`` declare ``levelHistogram`` of ``i_channel`` with
``groupBy: account.a_region``, and the pinned profile holds the statistic as a ``groupByLink``.
Pinned here:

* a run whose channel distribution per account region matches the profile passes, end to end;
* rows edited so one region's distribution moves fail that region only, naming the group by its
  ordinal, and a profile that says otherwise fails the run;
* rows whose link reaches no account form the group that is never compared;
* the parent's rows missing refuse the emit (``FLX-8001``), and a profile that holds the
  statistic grouped by another column, or by no link, is refused (``FLX-5037``);
* the declaration is refused (``FLX-5031``) when its relationship is none the contract declares.

"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.concordance.boundary.statistic import level_cells
from flux_engine.concordance.compiler import CompiledFidelity, compile_fidelity
from flux_engine.concordance.decoded import DecodedRows
from flux_engine.concordance.linked import join_linked_columns, link_groupings
from flux_engine.concordance.predicate import expose_from_contract
from flux_engine.concordance.verifier import ClearProfile
from flux_engine.core.errors import FluxError
from flux_engine.stages.run import run
from flux_engine.stages.verify import verify
from flux_stats.profile import SCHEMA_URL_1_3_0, LinkGrouping, parse_profile
from tests.bundles import repin_contract
from tests.stats.profiles import profile_from_rows

from .test_given_across_link_run import _CONTRACT, _CONTRACT_TEXT, _SIMULATION, FIXTURE
from .test_scorecard_stages import _pinned_bundle

pytestmark = pytest.mark.integration

INVOICES = 400
_TOLERANCE = "{ type: distance, measure: totalVariation, max: '0.2' }"

_BLOCK = """\
        rows: {rows}
        fidelity:
          encoding: {{ fractionalBits: 16, rounding: floor }}
          records: {{ min: 1, max: 1000000 }}
          columns:
{columns}          properties:
{properties}          budget: {{ maxChecksPerState: 20, maxStateAge: P30D }}
          failureAction: withholdBundle
"""

_ACCOUNT_COLUMNS = (
    "            a_region:\n"
    "              levels: { north: '0.5', south: '0.3', west: '0.2' }\n"
    "              support: { values: [north, south, west] }\n"
    "              maxCardinality: 3\n"
    "            a_score: { range: { min: '0', max: '100' } }\n"
)
_ACCOUNT_PROPERTIES = (
    "            - property: levelHistogram\n"
    "              column: a_region\n"
    f"              tolerance: {_TOLERANCE}\n"
)
_INVOICE_COLUMNS = (
    "            i_channel:\n"
    "              given: account.a_region\n"
    "              levelWeightsGiven:\n"
    "                north: { web: '1', shop: '0' }\n"
    "                south: { web: '0', shop: '1' }\n"
    "                west: { web: '1', shop: '0' }\n"
    "              support: { values: [shop, web] }\n"
    "              maxCardinality: 2\n"
    "            i_amount: { range: { min: '1', max: '30' } }\n"
    "            account.a_region:\n"
    "              support: { values: [north, south, west] }\n"
    "              maxCardinality: 3\n"
)
_INVOICE_PROPERTIES = (
    "            - property: levelHistogram\n"
    "              column: i_channel\n"
    "              groupBy: account.a_region\n"
    f"              tolerance: {_TOLERANCE}\n"
)


def _bundle(root: Path, *, columns: str = _INVOICE_COLUMNS, name: str = "bundle") -> Path:
    target = root / name
    shutil.copytree(FIXTURE, target)
    (target / _CONTRACT).write_text(_CONTRACT_TEXT, "utf-8")
    text = (target / _SIMULATION).read_text("utf-8")
    head, _, _ = text.partition("    - productRef:")
    emits = "".join(
        f"    - productRef: retail.store_star\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("account", "invoice")
    )
    extension = (
        "extensions:\n  com.agentics.flux/1:\n    emits:\n"
        "      account:\n"
        + _BLOCK.format(rows=60, columns=_ACCOUNT_COLUMNS, properties=_ACCOUNT_PROPERTIES)
        + "      invoice:\n"
        + _BLOCK.format(rows=INVOICES, columns=columns, properties=_INVOICE_PROPERTIES)
    )
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "retail.store_star")
    return target


def _compiled(run_dir: Path, expose_id: str) -> Tuple[CompiledFidelity, Any, Any]:
    fidelity_input = json.loads((run_dir / f"fidelity-input-{expose_id}.json").read_text())
    contract = fidelity_input["contract"]
    expose = expose_from_contract(contract, expose_id)
    return (
        compile_fidelity(contract=contract, emit=fidelity_input["emit"], expose=expose),
        contract,
        expose,
    )


def _rows(run_dir: Path, expose_id: str) -> List[Dict[str, Any]]:
    columns = json.loads((run_dir / f"seam-attestation-{expose_id}.json").read_text())["columns"]
    lines = (run_dir / "emits" / f"{expose_id}.ndjson").read_text().splitlines()
    return [dict(zip(columns, json.loads(line), strict=True)) for line in lines if line]


def _expected(accounts: List[Dict[str, Any]], invoices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The channel counts per region of the account the invoice names, counted by hand."""
    region = {row["a_id"]: row["a_region"] for row in accounts}
    counts: Dict[str, Dict[str, int]] = {}
    for row in invoices:
        found = counts.setdefault(region[row["i_account"]], {"shop": 0, "web": 0})
        found[row["i_channel"]] += 1
    return counts


def _profile(root: Path, bundle: Path) -> Dict[str, Any]:
    """A profile of the bundle's own run: the account table, and the invoices grouped by link."""
    first = run(bundle, state_dir=root / "first-state")
    assert first.success, first.stderr
    run_dir = root / "first-state" / "runs" / first.artifacts["plan"]["run_id"]
    account, _, _ = _compiled(run_dir, "account")
    invoice, _, _ = _compiled(run_dir, "invoice")
    accounts, invoices = _rows(run_dir, "account"), _rows(run_dir, "invoice")
    document = profile_from_rows(accounts, account)
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    document["tables"]["account"]["columns"]["a_id"] = {"domain": "category"}
    levels = list(level_cells(invoice.block.columns["i_channel"]))
    counts = _expected(accounts, invoices)
    document["tables"]["invoice"] = {
        "rowCount": len(invoices),
        "columns": {
            "i_account": {"domain": "category"},
            "i_channel": {
                "domain": "category",
                "statistics": [
                    {
                        "definition": "levelHistogram/1",
                        "levels": levels,
                        "sampleSize": {"min": len(invoices), "max": len(invoices)},
                        "disclosure": "none",
                        "groupByLink": {
                            "parent": {"table": "account", "columns": ["a_id"]},
                            "columns": ["i_account"],
                            "column": "a_region",
                        },
                        "groups": [
                            {
                                "group": region,
                                "k": sum(found.values()),
                                "levelCounts": [found[level] for level in levels],
                                "status": "F",
                            }
                            for region, found in sorted(counts.items())
                        ],
                    }
                ],
            },
        },
    }
    return document


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    root = tmp_path_factory.mktemp("linked-group")
    source = _bundle(root, name="source")
    profile = _profile(root, source)
    bundle, path, _ = _pinned_bundle(root / "pinned", profile, source=source)
    state = root / "state"
    applied = run(bundle, state_dir=state, profile=path)
    assert applied.success, applied.stderr
    run_id = applied.artifacts["plan"]["run_id"]
    run_dir = state / "runs" / run_id
    compiled, contract, expose = _compiled(run_dir, "invoice")
    return {
        "bundle": bundle,
        "source": source,
        "state": state,
        "run_id": run_id,
        "run_dir": run_dir,
        "profile": profile,
        "path": path,
        "compiled": compiled,
        "contract": contract,
        "expose": expose,
        "accounts": _rows(run_dir, "account"),
        "invoices": _rows(run_dir, "invoice"),
    }


def _evaluate(
    world: Dict[str, Any],
    invoices: List[Dict[str, Any]],
    accounts: List[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Any:
    compiled = world["compiled"]
    links = link_groupings(
        world["contract"], "invoice", compiled.block, world["expose"].column_names
    )
    rows = join_linked_columns(invoices, links, lambda parent: (accounts, None))
    return ClearProfile(
        compiled=compiled,
        profile=parse_profile(profile),
        synthetic_rows=rows,
        link_groupings=links,
    ).evaluate()


def test_a_run_whose_children_follow_their_parents_levels_passes_verify(
    world: Dict[str, Any],
) -> None:
    verified = verify(
        world["bundle"], run_id=world["run_id"], state_dir=world["state"], profile=world["path"]
    )
    assert verified.success, verified.stderr
    leg = verified.artifacts["fidelity"]["invoice"]
    assert leg["status"] == "passed", leg
    assert "2 of 2" in leg["detail"] or "1 of 1" in leg["detail"]


def test_the_declaration_names_the_grouping_the_profile_publishes(world: Dict[str, Any]) -> None:
    links = link_groupings(
        world["contract"], "invoice", world["compiled"].block, world["expose"].column_names
    )
    assert links == {
        "account.a_region": LinkGrouping(
            parent_table="account",
            parent_columns=("a_id",),
            child_columns=("i_account",),
            column="a_region",
        )
    }


def test_the_join_reads_each_invoices_account_region(world: Dict[str, Any]) -> None:
    links = link_groupings(
        world["contract"], "invoice", world["compiled"].block, world["expose"].column_names
    )
    joined = join_linked_columns(world["invoices"], links, lambda parent: (world["accounts"], None))
    region = {row["a_id"]: row["a_region"] for row in world["accounts"]}
    assert [row["account.a_region"] for row in joined] == [
        region[row["i_account"]] for row in world["invoices"]
    ]
    names = list(world["invoices"][0]) + ["account.a_region"]
    decoded = DecodedRows(list(world["invoices"][0]), [list(r.values()) for r in world["invoices"]])
    again = join_linked_columns(
        decoded,
        links,
        lambda parent: (
            DecodedRows(list(world["accounts"][0]), [list(r.values()) for r in world["accounts"]]),
            None,
        ),
    )
    assert list(again.names) == names
    assert [row["account.a_region"] for row in again] == [row["account.a_region"] for row in joined]


def test_rows_that_match_the_profile_are_within_in_every_group(world: Dict[str, Any]) -> None:
    evaluation = _evaluate(world, world["invoices"], world["accounts"], world["profile"])
    assert evaluation.within and not evaluation.refusals
    (outcome,) = evaluation.outcomes
    assert outcome.group_by == "account.a_region"
    assert [group.outcome for group in outcome.groups] == ["within"] * len(outcome.groups)


def test_edited_rows_fail_the_group_that_moved_and_only_that_one(world: Dict[str, Any]) -> None:
    accounts = world["accounts"]
    south = {row["a_id"] for row in accounts if row["a_region"] == "south"}
    invoices = copy.deepcopy(world["invoices"])
    for row in invoices:
        if row["i_account"] in south:
            row["i_channel"] = "web"
    evaluation = _evaluate(world, invoices, accounts, world["profile"])
    assert not evaluation.within
    (outcome,) = evaluation.outcomes
    moved = [group for group in outcome.groups if not group.within]
    assert [group.group for group in moved] == ["south"]
    assert all(group.within for group in outcome.groups if group.group != "south")


def test_a_row_whose_link_reaches_no_account_belongs_to_no_group(world: Dict[str, Any]) -> None:
    invoices = copy.deepcopy(world["invoices"])
    invoices[0]["i_account"] = "no-such-account"
    invoices[1]["i_account"] = None
    evaluation = _evaluate(world, invoices, world["accounts"], world["profile"])
    (outcome,) = evaluation.outcomes
    assert sum(group.synthetic_count for group in outcome.groups) == len(invoices) - 2
    assert evaluation.within or not evaluation.refusals


def test_the_parents_rows_missing_refuse_the_emit(world: Dict[str, Any]) -> None:
    links = link_groupings(
        world["contract"], "invoice", world["compiled"].block, world["expose"].column_names
    )
    with pytest.raises(FluxError) as refused:
        join_linked_columns(
            world["invoices"], links, lambda parent: ([], ("FLX-8001", "no emit file"))
        )
    assert refused.value.code.code == "FLX-8001"
    assert "account" in str(refused.value)


def test_a_profile_that_holds_no_grouping_by_that_link_is_refused(world: Dict[str, Any]) -> None:
    other = copy.deepcopy(world["profile"])
    statistic = other["tables"]["invoice"]["columns"]["i_channel"]["statistics"][0]
    del statistic["groupByLink"]
    statistic["groups"] = [
        {
            "group": "",
            "k": 10,
            "levelCounts": [5, 5],
            "status": "F",
        }
    ]
    evaluation = _evaluate(world, world["invoices"], world["accounts"], other)
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5037"]
    assert "groupBy" in evaluation.refusals[0].reason


def test_a_failing_profile_fails_the_run(world: Dict[str, Any], tmp_path: Path) -> None:
    bad = copy.deepcopy(world["profile"])
    statistic = bad["tables"]["invoice"]["columns"]["i_channel"]["statistics"][0]
    for group in statistic["groups"]:
        group["levelCounts"] = list(reversed(group["levelCounts"]))
    bundle, path, _ = _pinned_bundle(tmp_path, bad, source=world["source"])
    state = tmp_path / "state"
    applied = run(bundle, state_dir=state, profile=path)
    assert applied.success, applied.stderr
    verified = verify(
        bundle, run_id=applied.artifacts["plan"]["run_id"], state_dir=state, profile=path
    )
    assert not verified.success
    leg = verified.artifacts["fidelity"]["invoice"]
    assert leg["status"] == "failed"
    assert leg["outside"][0]["groupBy"] == "account.a_region"
    assert leg["outside"][0]["groupOrdinals"]


def test_a_relationship_the_contract_does_not_declare_is_refused(tmp_path: Path) -> None:
    refused = _bundle(
        tmp_path,
        columns=_INVOICE_COLUMNS.replace("account.a_region:", "customer.a_region:").replace(
            "given: customer.a_region", "given: account.a_region"
        ),
    )
    simulation = (refused / _SIMULATION).read_text("utf-8")
    simulation = simulation.replace("groupBy: account.a_region", "groupBy: customer.a_region")
    (refused / _SIMULATION).write_text(simulation, "utf-8")
    state = tmp_path / "state"
    result = run(refused, state_dir=state)
    assert not result.success
    assert "FLX-5031" in result.stderr or "FLX-5034" in result.stderr


def test_a_column_named_like_a_link_is_the_column_and_the_link_form_beside_it_is_the_link() -> None:
    """``groupBy: account.a_region`` is the invoice's own column when it holds one of that name."""
    import types

    import yaml

    contract = yaml.safe_load(_CONTRACT_TEXT)
    block = types.SimpleNamespace(columns={"account.a_region": None, "account.a_score": None})
    own = ["i_id", "i_account", "i_channel", "i_amount", "account.a_region"]
    assert link_groupings(contract, "invoice", block, own) == {
        "account.a_score": LinkGrouping(
            parent_table="account",
            parent_columns=("a_id",),
            child_columns=("i_account",),
            column="a_score",
        )
    }
    # With no such column of its own the same name is the link, as it was.
    assert sorted(link_groupings(contract, "invoice", block, own[:-1])) == [
        "account.a_region",
        "account.a_score",
    ]
