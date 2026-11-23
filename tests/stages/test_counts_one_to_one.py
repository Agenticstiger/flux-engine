# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A ``one_to_one`` relationship on the counted layout end to end (ADR 0016 section 2, cardinality).

The bundle is ``tests/fixtures/exact-counts`` with ``invoice.account`` made ``one_to_one``, 181
invoices over its 300 accounts, and a ``counts`` entry whose ``children`` is ``levels: ["0", "1"]``,
the shape ``fluxsim init`` writes for such a relationship. Of 300 accounts 180 are of class 1, so
the 181st invoice is the child the layout's cap moves to an account of class 0. What is pinned, on
kernel 0.2.0:

* **The cap.** No account has more than one invoice, so the account column holds each value at most
  once, and the invoices total the rows.
* **Scale.** ``--scale 0.5`` and ``--scale 0.37`` plan and apply: the rounding of the rows and of
  the classes leaves one more child than parents of class 1 under the first.
* **Two paths, one answer**, the shares of a ``--shard`` joined, and ``--jobs``.
* **The refusals** a ``one_to_one`` entry earns at plan time: a shape or a minimum above one, and
  more child rows than parents, which names ``--scale`` under a scale.
"""

from __future__ import annotations

import json
import re
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest
import yaml

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify
from tests.generator.test_rules_over_links import _by_both_paths

from .conftest import REPO_ROOT
from .test_counts_run import _digests, _shares, _table

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "exact-counts"
ACCOUNTS = 300
LEVELS = {"levels": ["0", "1"], "levelWeights": ["0.4", "0.6"]}


def bundle_with(
    tmp_path: Path,
    name: str = "one-to-one",
    *,
    invoices: int = 181,
    children: Optional[Mapping[str, Sequence[str]]] = LEVELS,
    minimum: Optional[int] = None,
    cardinality: str = "one_to_one",
    keyed: bool = False,
    parent_when: Optional[str] = None,
) -> Path:
    """A copy of ``tests/fixtures/exact-counts`` whose invoice to account relationship is *cardinality*."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    contract = target / "sales-chain.fluid.yml"
    text = contract.read_text("utf-8")
    old = "          toColumns: [account_id]\n          cardinality: many_to_one\n"
    assert text.count(old) == 1
    text = text.replace(old, old.replace("many_to_one", cardinality))
    if keyed:  # the account column is also the invoice's primary key
        key = "{ name: invoice, type: primary, expr: invoice_id }"
        assert text.count(key) == 1
        text = text.replace(key, key.replace("invoice_id", "account_id"))
    contract.write_text(text, "utf-8")
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    # The contract changed, so the digest it declared no longer names it.
    pinned = digest_of_document(
        yaml.safe_load(contract.read_text("utf-8")), subject="contractDigest:retail.sales_chain"
    ).value
    text = re.sub(r"sha256:[0-9a-f]{64}", pinned, text)
    for old, new in (
        ("        rows: 1200\n", f"        rows: {invoices}\n"),
        (
            "invoice_id: { range: { min: '1', max: '1200' } }",
            f"invoice_id: {{ range: {{ min: '1', max: '{invoices}' }} }}",
        ),
    ):
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    entry = "{ emit: invoice, relationship: account,"
    if minimum is not None:
        entry += f" minChildren: {minimum},"
    if parent_when is not None:
        entry += f" parentWhen: {json.dumps(parent_when)},"
    if children is not None:
        shape = ", ".join(f"{key}: {json.dumps(list(children[key]))}" for key in children)
        entry += f" children: {{ {shape} }},"
    anchor = "  com.agentics.flux/1:\n"
    assert text.count(anchor) == 1
    block = f"    counts:\n      - {entry.rstrip(',')} }}\n"
    path.write_text(text.replace(anchor, anchor + block), "utf-8")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return bundle_with(tmp_path_factory.mktemp("one-to-one"))


def _accounts_of(outcome: Any) -> List[Any]:
    return [row["account_id"] for row in _table(outcome, "invoice")]


def test_no_account_has_two_invoices_and_the_invoices_total_the_rows(bundle: Path) -> None:
    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    held = _accounts_of(outcome)
    assert len(held) == len(set(held)) == 181
    assert set(held) <= {row["account_id"] for row in _table(outcome, "account")}


@pytest.mark.parametrize("scale", ["0.5", "0.37", "2"])
def test_a_scaled_run_plans_applies_and_holds_each_account_at_most_once(
    bundle: Path, tmp_path: Path, scale: str
) -> None:
    planned = plan(bundle, state_dir=tmp_path / "state", kernel="0.2.0", scale=scale, stage=False)
    assert planned.success, planned.stderr
    applied = apply(bundle, run_id=str(planned.artifacts["run_id"]), state_dir=tmp_path / "state")
    assert applied.success, applied.stderr
    scaled = pipeline.execute(bundle, kernel=KERNEL_V2, scale=Decimal(scale))
    held = _accounts_of(scaled)
    assert len(held) == len(set(held)) == len(_table(scaled, "invoice"))
    assert len(held) == int((Decimal(181) * Decimal(scale)).to_integral_value("ROUND_HALF_UP"))


def test_verify_passes_on_the_run(bundle: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state, kernel="0.2.0", stage=False)
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    assert apply(bundle, state_dir=state, run_id=run_id).success
    verified = verify(bundle, run_id=run_id, state_dir=state, allow_declared_baseline_drift=True)
    assert verified.success, verified.stderr


def test_the_column_path_equals_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, bundle: Path
) -> None:
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(bundle, kernel=KERNEL_V2)
    )
    for expose_id in ("invoice", "account"):
        assert by_columns.emit(expose_id).rows == by_rows.emit(expose_id).rows, expose_id
    assert taken and all(taken)


@pytest.mark.parametrize("count", [3, 7])
def test_the_shares_joined_are_the_whole_runs_bytes(bundle: Path, count: int) -> None:
    whole, parts = _shares(bundle, count)
    joined = b"".join(part.emit("invoice").writer.stream_bytes() for part in parts)
    assert joined == whole.emit("invoice").writer.stream_bytes()


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_digests_or_the_bytes(bundle: Path, tmp_path: Path) -> None:
    runs = [pipeline.execute(bundle, kernel=KERNEL_V2, jobs=jobs) for jobs in (1, 4)]
    for expose_id in ("invoice", "invoice_line"):
        assert (
            runs[0].emit(expose_id).writer.stream_bytes()
            == runs[1].emit(expose_id).writer.stream_bytes()
        )
    found = []
    for jobs in (1, 4):
        state = tmp_path / f"state-{jobs}"
        planned = plan(bundle, state_dir=state, jobs=jobs, kernel="0.2.0")
        assert planned.success, planned.stderr
        applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
        assert applied.success, applied.stderr
        found.append((planned.artifacts.get("plan_digest"), _digests(applied)))
    assert found[0] == found[1]


def test_the_cap_matches_the_layout_function(bundle: Path) -> None:
    from flux_engine.generator.counts import count_key, counted_layout

    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    layout = counted_layout(
        count_key(outcome.root, outcome.emit("invoice").plan.sim_id, "invoice", "account"),
        ACCOUNTS,
        181,
        shape=LEVELS,
        one_to_one=True,
    )
    keys = [row["account_id"] for row in _table(outcome, "account")]
    held = set(_accounts_of(outcome))
    assert [key in held for key in keys] == [bool(count) for count in layout.counts]


# -- the refusals -----------------------------------------------------------------------------


def _refused(bundle: Dict[str, Any] | Path, **run: Any) -> str:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2, **run)  # type: ignore[arg-type]
    assert caught.value.code.code == "FLX-3403", str(caught.value)
    return str(caught.value)


@pytest.mark.parametrize(
    "children",
    [
        {"levels": ["0", "1", "2"], "levelWeights": ["0.2", "0.6", "0.2"]},
        {"bins": ["0", "1", "3"], "binWeights": ["0.5", "0.5"]},
    ],
    ids=["a-level-above-one", "a-bin-reaching-above-one"],
)
def test_a_shape_that_allows_a_count_above_one_is_refused_at_plan(
    tmp_path: Path, children: Mapping[str, Sequence[str]]
) -> None:
    message = _refused(bundle_with(tmp_path, children=children, invoices=100))
    assert "one_to_one" in message and "children shape allows" in message


def test_a_bin_that_stays_at_one_is_accepted(tmp_path: Path) -> None:
    children = {"bins": ["0", "1"], "binWeights": ["1"]}
    outcome = pipeline.execute(bundle_with(tmp_path, children=children, invoices=100))
    held = _accounts_of(outcome)
    assert len(held) == len(set(held)) == 100


def test_a_minimum_above_one_is_refused_at_plan(tmp_path: Path) -> None:
    message = _refused(bundle_with(tmp_path, children=None, minimum=2, invoices=100))
    assert "minChildren is 2" in message


def test_child_rows_above_the_parents_are_refused_at_plan_and_name_the_scale_under_one(
    tmp_path: Path,
) -> None:
    many = bundle_with(tmp_path, invoices=ACCOUNTS + 1)
    message = _refused(many)
    assert "at most once" in message and "--scale" not in message
    scaled = _refused(many, scale=Decimal("0.9"))
    assert "--scale 0.9" in scaled and "more child rows than parents" in scaled


def test_a_minimum_of_one_needs_as_many_rows_as_parents(tmp_path: Path) -> None:
    exact = bundle_with(tmp_path, "exact", children=None, minimum=1, invoices=ACCOUNTS)
    held = _accounts_of(pipeline.execute(exact, kernel=KERNEL_V2))
    assert len(set(held)) == ACCOUNTS
    assert "N = 299" in _refused(
        bundle_with(tmp_path, "short", children=None, minimum=1, invoices=299)
    )


def test_columns_that_are_a_key_of_the_child_stay_refused(tmp_path: Path) -> None:
    # A many_to_one relationship whose column is the invoice's own key is not a layout of counts.
    keyed = bundle_with(tmp_path, cardinality="many_to_one", keyed=True, invoices=100)
    message = _refused(keyed)
    assert "a key of the expose" in message


def test_child_rows_above_the_eligible_parents_name_the_scale_when_the_layout_is_worked_out(
    tmp_path: Path,
) -> None:
    # A parentWhen makes the eligible parents known only at first layout, not at plan time.
    many = bundle_with(tmp_path, invoices=ACCOUNTS - 20, parent_when="status <> 'closed'")
    plain = _refused(many)
    assert "more than its" in plain and "--scale" not in plain
    scaled = _refused(many, scale=Decimal("0.9"))
    assert "--scale 0.9" in scaled and "more child rows than parents" in scaled
