# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A FLUX-side rule that reads ``previous`` generates on a history's emit (ADR 0017 section 7).

The previous version of row ``i`` is row ``i - 1`` unless ``i`` opens its entity's chain, and it is
read as the expose's own pass writes it, its rules included. What is pinned, on kernel 0.2.0:

* **The column.** ``derive: prior_plan, expr: previous.plan`` takes the previous version's plan,
  and NULL on an entity's first version.
* **The chain.** A rule that reads a column another rule derives from ``previous`` holds version
  by version.
* **The relationship.** ``previous.<relationship>.<column>`` reads the previous version's link.
* **Two paths, one answer.** The column path's rows equal the per-row path's, and a slice that
  cuts an entity in two is the whole table's slice.
* **The refusals.** An emit with no history keeps ``FLX-6210``, and an unversioned expose keeps
  ``FLX-6106``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.rules import plan_rules
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.history import HistoryEntry
from flux_engine.seam.keys import compile_keys, mark_key_columns
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.plan import build_plan_document
from flux_engine.stages.verify import verify
from tests.stages.test_history_run import EMIT as RUN_EMIT
from tests.stages.test_history_run import NAMESPACE, _bundle, _rows, _run, _subscriptions

from .test_history_end import (
    EMIT,
    by_entity,
    contract,
    declared,
    entry,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
)
from .test_history_end import HistoryBinding as _Binding
from .test_history_layout import BOUNDS, SIM_ID, _column, _h, root_for, table
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

PRIOR_PLAN = {
    "id": "prior_plan_rule",
    "language": "flux-expr/2",
    "derive": "prior_plan",
    "expr": "previous.plan",
}
PRIOR_PRIOR = {
    "id": "prior_prior_rule",
    "language": "flux-expr/2",
    "derive": "prior_prior",
    "expr": "previous.prior_plan",
}
PRIOR_CUSTOMER = {
    "id": "prior_customer_rule",
    "language": "flux-expr/2",
    "derive": "prior_customer",
    "expr": "previous.customer.c_name",
}
FIXTURE_DIGEST = "sha256:221eb9a322807255ae15c0c76f70b0ca2bb3c71c4b424266886db49635778ab7"
WITH_COLUMN_DIGEST = "sha256:0c485ea079d99c41cef4fd1d0934cea14bcaae8c3f272bc04b563e41cff13c6b"
RULES = [PRIOR_PLAN, PRIOR_PRIOR, PRIOR_CUSTOMER]
LONG = entry(entities=16)


def document() -> Dict[str, Any]:
    """The history contract with a column for each rule the tests declare."""
    found = copy.deepcopy(contract())
    schema = next(expose for expose in found["exposes"] if expose["exposeId"] == EMIT)["contract"][
        "schema"
    ]
    for name in ("prior_plan", "prior_prior", "prior_customer"):
        schema.append(_column(name, "STRING", False))
    return found


def plans_with(
    held: HistoryEntry,
    rules: Sequence[Mapping[str, Any]],
    *,
    rows: int = 60,
    entities: int = 16,
    with_history: bool = True,
    bind: bool = True,
) -> Dict[str, GeneratorPlan]:
    """The plans of the document, the versioned emit carrying *rules* as FLUX-side rules."""
    found = document()
    keys = compile_keys(found)
    assert keys is not None
    declarations = declared(rows, 6)
    for name in ("prior_plan", "prior_prior", "prior_customer"):
        declarations[EMIT][0][name] = {"levels": {"a": "1"}}
    plans: Dict[str, GeneratorPlan] = {}
    for expose in found["exposes"]:
        expose_id = expose["exposeId"]
        expose_keys = keys.expose(expose_id)
        compiled = mark_key_columns(compile_expose(expose), expose_keys)
        fidelity, count = declarations[expose_id]
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
                contract_document=found,
                expose_id=expose_id,
                settings=None,
                related=related,
                mapping=None,
                has_fidelity=True,
                strict=False,
                flux_rules=list(rules) if expose_id == EMIT else None,
                keys=expose_keys,
                rows=count,
                null_fractions={},
                counter_based=True,
                links=(),
                copies=False,
                history=(held,) if with_history and expose_id == EMIT else (),
            ),
            generator_version=GENERATOR_VERSION_2,
        )
    binding = {EMIT: _Binding(held, entities, "valid_from", "valid_to")} if bind else {}
    return relate_plans(plans, keys, None, None, None, found, None, binding)


def rows_of(plans: Mapping[str, GeneratorPlan], expose_id: str = EMIT) -> List[Dict[str, Any]]:
    plan = plans[expose_id]
    return table(plan, generate_row_slice(plan, 0, plan.rows, root=root_for()))


def in_chain_order(data: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    """Each entity's versions by start, which is also the order the rows come in."""
    found = by_entity(data)
    for versions in found.values():
        assert [row["valid_from"] for row in versions] == sorted(
            row["valid_from"] for row in versions
        )
    return found


# -- the column -----------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_rule_reading_previous_takes_the_previous_versions_column(seed: int) -> None:
    plans = plans_with(LONG, [PRIOR_PLAN])
    plan = plans[EMIT]
    data = table(plan, generate_row_slice(plan, 0, plan.rows, root=root_for(seed)))
    chains = in_chain_order(data)
    assert any(len(versions) > 1 for versions in chains.values())
    for versions in chains.values():
        assert versions[0]["prior_plan"] is None
        for before, after in zip(versions, versions[1:], strict=False):
            assert after["prior_plan"] == before["plan"]
            assert after["prior_plan"] is not None


def test_a_rule_reading_previous_leaves_every_other_cell_as_it_was() -> None:
    with_rule = rows_of(plans_with(LONG, [PRIOR_PLAN]))
    bare = rows_of(plans_with(LONG, []))
    for held, plain in zip(with_rule, bare, strict=True):
        assert {k: v for k, v in held.items() if k != "prior_plan"} == {
            k: v for k, v in plain.items() if k != "prior_plan"
        }


# -- the chain ------------------------------------------------------------------------------------


def test_a_chain_of_two_previous_reads_holds_version_by_version() -> None:
    data = rows_of(plans_with(LONG, [PRIOR_PLAN, PRIOR_PRIOR]))
    long_chains = [versions for versions in in_chain_order(data).values() if len(versions) >= 3]
    assert long_chains
    for versions in in_chain_order(data).values():
        assert [row["prior_prior"] for row in versions[:2]] == [None] * min(2, len(versions))
        for index in range(2, len(versions)):
            assert versions[index]["prior_prior"] == versions[index - 2]["plan"]


def test_a_rule_reads_the_previous_rows_own_rules() -> None:
    # prior_prior reads the previous row's prior_plan, which that row's own rule set.
    data = rows_of(plans_with(LONG, [PRIOR_PRIOR, PRIOR_PLAN]))
    for versions in in_chain_order(data).values():
        for before, after in zip(versions, versions[1:], strict=False):
            assert after["prior_prior"] == before["prior_plan"]


# -- the relationship -----------------------------------------------------------------------------


def test_a_rule_through_a_relationship_reads_the_previous_versions_link() -> None:
    plans = plans_with(LONG, [PRIOR_CUSTOMER])
    data = rows_of(plans)
    names = {row["customer_id"]: row["c_name"] for row in rows_of(plans, "customer")}
    assert names
    for versions in in_chain_order(data).values():
        assert versions[0]["prior_customer"] is None
        for before, after in zip(versions, versions[1:], strict=False):
            assert after["prior_customer"] == names[before["customer_id"]]


# -- two paths, one answer ------------------------------------------------------------------------


def test_the_column_path_is_the_per_row_path(monkeypatch: pytest.MonkeyPatch) -> None:
    plans = plans_with(LONG, RULES)
    plan = plans[EMIT]
    root = root_for()
    by_columns, by_rows, taken = _by_both_paths(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken and all(taken)


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (1, 2), (7, 40), (23, 24), (50, 60)])
def test_a_slice_that_cuts_an_entity_is_the_whole_tables_slice(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    plans = plans_with(LONG, RULES)
    plan = plans[EMIT]
    root = root_for()
    whole = generate_rows(plan, root=root)
    assert generate_row_slice(plan, start, stop, root=root) == whole[start:stop]
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    assert generate_rows(plan, root=root) == whole


# -- the refusals ---------------------------------------------------------------------------------


def test_a_versioned_emit_with_no_history_keeps_the_refusal() -> None:
    with pytest.raises(FluxError) as caught:
        plans_with(LONG, [PRIOR_PLAN], with_history=False, bind=False)
    assert caught.value.code.code == "FLX-6210"
    assert "previous" in str(caught.value)


def test_an_unversioned_expose_keeps_the_refusal() -> None:
    found = document()
    keys = compile_keys(found)
    assert keys is not None
    expose = next(item for item in found["exposes"] if item["exposeId"] == "customer")
    rule = {
        "id": "prior_name",
        "language": "flux-expr/2",
        "derive": "c_name",
        "expr": "previous.c_name",
    }
    with pytest.raises(FluxError) as caught:
        plan_rules(
            contract_document=found,
            expose_id=expose["exposeId"],
            settings=None,
            related=related_columns(keys.expose("customer")),
            mapping=None,
            has_fidelity=True,
            strict=False,
            flux_rules=[rule],
            keys=keys.expose("customer"),
            counter_based=True,
        )
    assert caught.value.code.code == "FLX-6106"


# -- a run: jobs and verify -----------------------------------------------------------------------


def _bundle_with_rules(tmp_path: Path, **sizes: int) -> Path:
    """The history-chain bundle with a ``prior_plan`` column that a FLUX-side rule derives."""
    bundle = _bundle(tmp_path, **sizes)
    fluid = bundle / "subscriptions.fluid.yml"
    text = fluid.read_text("utf-8")
    anchor = "        - { name: started_on, type: DATE, required: true }\n    semantics:\n      entities:\n        - { name: subscription_version"
    assert anchor in text
    fluid.write_text(
        text.replace(
            anchor,
            "        - { name: started_on, type: DATE, required: true }\n"
            "        - { name: prior_plan, type: STRING, required: false }\n"
            "    semantics:\n      entities:\n        - { name: subscription_version",
            1,
        ),
        "utf-8",
    )
    path = bundle / "simulation.flux.yml"
    # The contract changed, so the digest each emit declares is the new contract's.
    text = path.read_text("utf-8").replace(FIXTURE_DIGEST, WITH_COLUMN_DIGEST)
    document = yaml.safe_load(text)
    emit = document["extensions"][NAMESPACE]["emits"][RUN_EMIT]
    emit["fidelity"]["columns"]["prior_plan"] = {"levels": {"basic": "1"}}
    emit["rules"] = [PRIOR_PLAN]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


@pytest.mark.integration
def test_a_run_generates_the_rule_and_verify_checks_it(tmp_path: Path) -> None:
    bundle = _bundle_with_rules(tmp_path)
    layout = _run(bundle, tmp_path / "state")
    data = _rows(layout, RUN_EMIT)
    chains = _subscriptions(data)
    assert len(chains) == 60
    assert any(len(versions) > 1 for versions in chains.values())
    for versions in chains.values():
        assert versions[0]["prior_plan"] is None
        for before, after in zip(versions, versions[1:], strict=False):
            assert after["prior_plan"] == before["plan"]
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    # A written row that breaks the rule fails verify: the check reads the previous version.
    broken = [dict(row) for row in data]
    victim = next(row for row in broken if row["prior_plan"] is not None)
    victim["prior_plan"] = "elsewhere" if victim["prior_plan"] != "elsewhere" else "basic"
    names = list(broken[0])
    layout.emit_path(RUN_EMIT).write_text(
        "".join(json.dumps([row[name] for name in names]) + "\n" for row in broken), "utf-8"
    )
    again = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert not again.success


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes(tmp_path: Path) -> None:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle_with_rules(tmp_path, versions=versions, entities=2500, customers=1200)
    one = pipeline.execute(bundle, jobs=1, kernel=KERNEL_V2)
    four = pipeline.execute(bundle, jobs=4, kernel=KERNEL_V2)
    assert build_plan_document(four) == build_plan_document(one)
    for mine, theirs in zip(four.emits, one.emits, strict=True):
        assert mine.result == theirs.result
        assert mine.writer.lines() == theirs.writer.lines()
    generated = [e for e in four.emits if e.plan is not None and e.plan.rows == versions]
    assert generated and all(e.processes > 1 for e in generated)
