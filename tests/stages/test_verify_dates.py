# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify`` scores every temporal fact and every level of a chain (ADR 0024 section 8).

``tests/fixtures/temporal`` is applied with the namespace's ``lifetimes`` and ``within`` keys, and
the rows the run wrote are then replaced by rows written here, so the counts do not depend on what
the generator drew. The module checks read the rows back as ``verify`` does (``artifacts_ok`` set,
since the edit moves the run's digest), and the stage itself runs on the rows the generator drew.
"""

from __future__ import annotations

import json
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.stages._module_checks import ModuleChecks, module_checks
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify
from tests.fixtures.temporal.bundle import CHAIN as BASE_CHAIN
from tests.fixtures.temporal.bundle import copy_uncut

#: The chain with its outer levels stated: a date tie on a middle expose's start makes no implied line.
CHAIN = (
    BASE_CHAIN
    + "  - { emit: invoice, relationship: account.customer, date: i_issued }\n"
    + "  - { emit: payment, relationship: invoice.account, date: p_paid }\n"
)

pytestmark = pytest.mark.integration

Row = Dict[str, Any]

#: Rows that hold every fact of the chain whichever account an invoice names: every customer runs
#: from 2024-01-10 and every account from 2024-02-01, ends and all, so a test changes one cell.
CUSTOMER_END = "2024-12-31"
ACCOUNT_END = "2024-06-30"


class Run:
    """An applied bundle, whose rows a test may replace, and what its module checks find."""

    def __init__(self, bundle: Path, state: Path, *, run_id: Optional[str] = None) -> None:
        """Plan and apply *bundle*, or, given the id of a run already applied there, take it as it is."""
        if run_id is None:
            planned = plan(bundle, state_dir=state, kernel="0.2.0")
            assert planned.success, planned.stderr
            run_id = str(planned.artifacts["run_id"])
            applied = apply(bundle, state_dir=state, run_id=run_id)
            assert applied.success, applied.stderr
        self.bundle = bundle
        self.state = state
        self.layout = RunLayout.for_state_dir(state, run_id)

    def rewrite(self, expose_id: str, edit: Callable[[int, Row], None]) -> None:
        """Replace the cells of an emit's rows: *edit* gets each row as a mapping by column name."""
        names = json.loads(self.layout.seam_attestation_path(expose_id).read_text("utf-8"))[
            "columns"
        ]
        path = self.layout.emit_path(expose_id)
        out: List[str] = []
        for index, line in enumerate(path.read_text("utf-8").splitlines()):
            row = dict(zip(names, json.loads(line), strict=True))
            edit(index, row)
            out.append(json.dumps([row[name] for name in names], separators=(",", ":")))
        path.write_text("\n".join(out) + "\n", "utf-8")

    def checks(self) -> ModuleChecks:
        receipt = json.loads(self.layout.receipt_path.read_text("utf-8"))
        document = json.loads(self.layout.plan_path.read_text("utf-8"))
        return module_checks(self.bundle, self.layout, receipt, document, artifacts_ok=True)

    def verified(self) -> Any:
        return run_verify(
            self.bundle,
            run_id=self.layout.run_id,
            state_dir=self.state,
            allow_declared_baseline_drift=True,
        )


# Most tests here start from the same applied bundle and then rewrite its rows, so each would plan and
# apply the same bundle again. The bundle is applied once per module, under a directory of its own, and
# every test takes a copy of that bundle and of its state: the rows a test rewrites are its own.
_APPLIED: Dict[str, Run] = {}
_APPLIED_ROOT: List[Path] = []


@pytest.fixture(scope="module", autouse=True)
def _applied_bundles(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    _APPLIED.clear()
    _APPLIED_ROOT[:] = [tmp_path_factory.mktemp("applied")]
    yield
    _APPLIED.clear()


def _copy_of_applied(label: str, build: Callable[[Path], Path], tmp_path: Path, name: str) -> Run:
    """A run of the bundle *build* writes, applied once under *label*, copied to *tmp_path* as *name*."""
    if label not in _APPLIED:
        root = _APPLIED_ROOT[0] / str(len(_APPLIED))
        root.mkdir()
        _APPLIED[label] = Run(build(root), root / "state")
    template = _APPLIED[label]
    bundle, state = tmp_path / name, tmp_path / f"state-{name}"
    shutil.copytree(template.bundle, bundle)
    shutil.copytree(template.state, state)
    return Run(bundle, state, run_id=template.layout.run_id)


def _make(tmp_path: Path, keys: str = CHAIN, name: str = "bundle") -> Run:
    return _copy_of_applied(
        f"keys:{keys}", lambda root: copy_uncut(root, "bundle", keys), tmp_path, name
    )


def _clean(run: Run) -> None:
    """Rows that hold every fact: every end is not before its start, every date within its parents'."""

    def customer(index: int, row: Row) -> None:
        row["cu_start"] = "2024-01-10"
        row["cu_end"] = None if index % 3 == 0 else CUSTOMER_END

    def account(index: int, row: Row) -> None:
        row["a_open"] = "2024-02-01"
        row["a_close"] = None if index % 2 == 0 else ACCOUNT_END

    def invoice(_: int, row: Row) -> None:
        row["i_issued"] = "2024-03-01T10:00:00.000Z"

    def payment(_: int, row: Row) -> None:
        row["p_paid"] = "2024-03-05T00:00:00.000Z"

    for expose_id, edit in (
        ("customer", customer),
        ("account", account),
        ("invoice", invoice),
        ("payment", payment),
    ):
        run.rewrite(expose_id, edit)


def _by_name(checks: ModuleChecks) -> Dict[str, Any]:
    return {share.name: share for share in checks.rules}


def _counts(checks: ModuleChecks, name: str) -> Tuple[int, int]:
    holds = _by_name(checks)[name].holds
    return holds.satisfied, holds.total


CHAIN_FACTS = (
    "customer.lifetime:cu_start.cu_end",
    "account.lifetime:a_open.a_close",
    "account.within:customer.a_open",
    "invoice.within:account.i_issued",
    "invoice.within:account.customer.i_issued",
    "payment.within:invoice.p_paid",
    "payment.within:invoice.account.p_paid",
)


def test_every_fact_and_every_level_of_a_chain_is_a_line_and_holds_on_clean_rows(
    tmp_path: Path,
) -> None:
    run = _make(tmp_path)
    _clean(run)
    checks = run.checks()
    assert tuple(sorted(_by_name(checks))) == tuple(sorted(CHAIN_FACTS))
    assert not checks.failures() and not checks.not_scored
    assert _counts(checks, "customer.lifetime:cu_start.cu_end") == (40, 40)  # 20 customers are open
    for name in CHAIN_FACTS:
        share = _by_name(checks)[name]
        assert share.holds.whole and share.holds.total > 0 and share.met is True
        assert share.enforced is True and share.target is None
    # every row the link reaches is covered: a DATE, a TIMESTAMP and a chain of two links
    assert _counts(checks, "invoice.within:account.customer.i_issued") == (900, 900)
    assert _counts(checks, "payment.within:invoice.account.p_paid") == (700, 700)


def test_a_lifetime_whose_end_is_before_its_start_violates_its_line_once(tmp_path: Path) -> None:
    run = _make(tmp_path)
    _clean(run)
    run.rewrite("customer", lambda i, row: row.update({"cu_end": "2023-12-31"} if i == 1 else {}))
    checks = run.checks()
    # row 1 now ends before it starts (it was one of the 40 customers with an end)
    assert _counts(checks, "customer.lifetime:cu_start.cu_end") == (39, 40)
    assert "rule customer.lifetime:cu_start.cu_end: 39 of 40 (97.50%)" in checks.failures()[0]
    # for every row that reads it as a parent, that lifetime is open: no other fact moves
    assert _counts(checks, "account.within:customer.a_open") == (150, 150)


def test_a_date_before_its_parents_start_violates_the_level_and_the_outer_level(
    tmp_path: Path,
) -> None:
    run = _make(tmp_path)
    _clean(run)
    run.rewrite(
        "invoice",
        lambda i, row: row.update({"i_issued": "2024-01-20T00:00:00.000Z"} if i == 0 else {}),
    )
    checks = run.checks()
    assert _counts(checks, "invoice.within:account.i_issued") == (
        899,
        900,
    )  # account from 2024-02-01
    assert _counts(checks, "invoice.within:account.customer.i_issued") == (
        900,
        900,
    )  # inside customer
    run.rewrite(
        "invoice",
        lambda i, row: row.update({"i_issued": "2023-12-31T23:59:59.999Z"} if i == 1 else {}),
    )
    checks = run.checks()
    assert _counts(checks, "invoice.within:account.i_issued") == (898, 900)
    assert _counts(checks, "invoice.within:account.customer.i_issued") == (899, 900)


def test_a_date_after_a_parent_that_has_ended_violates_and_an_open_parent_does_not(
    tmp_path: Path,
) -> None:
    run = _make(tmp_path)
    _clean(run)
    run.rewrite("invoice", lambda i, row: row.update({"i_issued": "2024-09-01T00:00:00.000Z"}))
    run.rewrite(
        "account", lambda i, row: row.update({"a_close": None if i % 2 == 0 else "2024-06-30"})
    )
    checks = run.checks()
    accounts = {
        index: (row[0], row[1])
        for index, row in enumerate(
            json.loads(line)
            for line in run.layout.emit_path("account").read_text("utf-8").splitlines()
        )
    }
    ended = {key for index, (key, _) in accounts.items() if index % 2 == 1}
    invoices = [
        json.loads(line) for line in run.layout.emit_path("invoice").read_text("utf-8").splitlines()
    ]
    expected = sum(1 for row in invoices if row[1] not in ended)
    assert _counts(checks, "invoice.within:account.i_issued") == (expected, 900)
    assert 0 < expected < 900


def test_a_row_without_a_parent_start_or_a_value_of_its_own_is_left_out_of_both_counts(
    tmp_path: Path,
) -> None:
    run = _make(tmp_path)
    _clean(run)
    run.rewrite("account", lambda i, row: row.update({"a_open": None} if i < 10 else {}))
    run.rewrite("customer", lambda i, row: row.update({"cu_start": None} if i < 6 else {}))
    run.rewrite("payment", lambda i, row: row.update({"p_paid": None} if i < 5 else {}))
    run.rewrite("invoice", lambda i, row: row.update({"i_account": None} if i < 4 else {}))
    checks = run.checks()
    accounts = [
        json.loads(line)[0]
        for line in run.layout.emit_path("account").read_text("utf-8").splitlines()
    ]
    started = set(accounts[10:])
    invoices = [
        json.loads(line) for line in run.layout.emit_path("invoice").read_text("utf-8").splitlines()
    ]
    covered = sum(1 for row in invoices if row[1] in started)  # the 4 with no account reach none
    satisfied, total = _counts(checks, "invoice.within:account.i_issued")
    assert total == covered and satisfied == total
    payments = [
        json.loads(line) for line in run.layout.emit_path("payment").read_text("utf-8").splitlines()
    ]
    assert _counts(checks, "payment.within:invoice.p_paid")[1] == sum(
        1 for row in payments[5:] if row[1] is not None
    )
    assert not checks.failures()


def test_an_enforced_fact_fails_the_run_on_one_violating_row(tmp_path: Path) -> None:
    run = _make(tmp_path)
    _clean(run)
    assert not run.checks().failures()
    run.rewrite(
        "payment",
        lambda i, row: row.update({"p_paid": "2024-01-05T00:00:00.000Z"} if i == 7 else {}),
    )
    checks = run.checks()
    # the payment is before its invoice, so before the account and the customer behind it
    assert _counts(checks, "payment.within:invoice.p_paid") == (699, 700)
    assert _counts(checks, "payment.within:invoice.account.p_paid") == (699, 700)
    assert _counts(checks, "payment.within:invoice.account.p_paid") == (699, 700)
    assert any("payment.within:invoice.account.p_paid" in line for line in checks.failures())


FACTS_TARGETS = """\
within:
  - { emit: account, relationship: customer, date: a_open }
  - { emit: invoice, relationship: account, date: i_issued, target: "50" }
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end }
  - { emit: account, start: a_open, end: a_close }
  - { emit: invoice, start: i_issued }
"""


def _half_early(run: Run, *, early: int) -> None:
    run.rewrite(
        "invoice",
        lambda i, row: row.update(
            {"i_issued": "2024-01-15T00:00:00.000Z" if i < early else "2024-03-01T10:00:00.000Z"}
        ),
    )


def test_a_fact_with_a_target_is_met_near_it_and_missed_away_from_it(tmp_path: Path) -> None:
    run = _make(tmp_path, FACTS_TARGETS)
    _clean(run)
    _half_early(run, early=450)
    checks = run.checks()
    share = _by_name(checks)["invoice.within:account.i_issued"]
    assert (share.holds.satisfied, share.holds.total) == (450, 900)
    assert share.target == Fraction(50) and share.met is True and share.enforced is True
    assert not checks.failures()
    _half_early(run, early=600)  # 67 % violate against a target of 50 %
    missed = _by_name(run.checks())["invoice.within:account.i_issued"]
    assert missed.met is False
    assert "outside its target" in "\n".join(run.checks().lines())
    # a date tie on the account's start makes no implied outer level (ADR 0024 section 8)
    assert "invoice.within:account.customer.i_issued" not in _by_name(run.checks())


def test_a_facts_entry_gives_a_temporal_fact_its_tolerance(tmp_path: Path) -> None:
    keys = FACTS_TARGETS + (
        "facts:\n" '  - { emit: invoice, rule: "within:account.i_issued", tolerance: "0.2" }\n'
    )
    run = _make(tmp_path, keys)
    _clean(run)
    _half_early(run, early=600)  # 67 % violate against a target of 50 %: 17 points over
    share = _by_name(run.checks())["invoice.within:account.i_issued"]
    assert share.allowed == Fraction(1, 5) and share.met is True
    assert [fact.name for fact in run.checks().facts] == ["invoice.within:account.i_issued"]


def test_a_facts_entry_for_an_implied_level_is_accepted(tmp_path: Path) -> None:
    keys = CHAIN + (
        "facts:\n"
        '  - { emit: invoice, rule: "within:account.customer.i_issued", tolerance: "0.01" }\n'
    )
    run = _make(tmp_path, keys)
    assert [fact.name for fact in run.checks().facts] == [
        "invoice.within:account.customer.i_issued"
    ]


def test_an_entry_that_states_the_outer_level_holds_its_own_target(tmp_path: Path) -> None:
    keys = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end }
  - { emit: account, start: a_open, end: a_close }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, date: a_open }
  - { emit: invoice, relationship: account, date: i_issued }
  - { emit: invoice, relationship: "account.customer", date: i_issued, target: "30" }
"""
    run = _make(tmp_path, keys)
    _clean(run)
    checks = run.checks()
    names = [share.name for share in checks.rules if share.expose_id == "invoice"]
    assert names == ["invoice.within:account.i_issued", "invoice.within:account.customer.i_issued"]
    assert _by_name(checks)["invoice.within:account.customer.i_issued"].target == Fraction(30)


# ---------------------------------------------------------------------------
# Hidden data
# ---------------------------------------------------------------------------

HIDDEN_COLUMN_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: churn }
  - { emit: account, start: a_open, end: a_close }
within:
  - { emit: account, relationship: customer, date: a_open }
"""
REASON_COLUMN = (
    "it reads the hidden column customer.churn; the run's streams hold no hidden column "
    "and no hidden table"
)
REASON_TABLE = (
    "it reads the hidden table customer; the run's streams hold no hidden column and no "
    "hidden table"
)


def _with_hidden_column(text: str) -> str:
    text = text.replace(
        "      customer:\n        rows: 60\n",
        "      customer:\n        rows: 60\n        hiddenColumns:\n          - { name: churn, type: DATE }\n",
    )
    return text.replace(
        "            cu_score: { range",
        "            churn: { range: { min: '2024-01-01', max: '2025-12-31' } }\n"
        "            cu_score: { range",
    )


def _hidden_run(tmp_path: Path, keys: str, edit: Callable[[str], str], name: str) -> Run:
    def build(root: Path, directory: str) -> Path:
        bundle = copy_uncut(root, directory, keys)
        simulation = bundle / "simulation.flux.yml"
        simulation.write_text(edit(simulation.read_text("utf-8")), "utf-8")
        return bundle

    # An edit written as a lambda has no name to tell it from another, so it is applied for its own test.
    if edit.__name__ == "<lambda>":
        return Run(build(tmp_path, name), tmp_path / f"state-{name}")
    return _copy_of_applied(
        f"hidden:{edit.__name__}:{keys}", lambda root: build(root, "bundle"), tmp_path, name
    )


def test_a_fact_over_a_hidden_column_is_listed_as_not_scored_with_the_reason(
    tmp_path: Path,
) -> None:
    run = _hidden_run(tmp_path, HIDDEN_COLUMN_KEYS, _with_hidden_column, "hidden-column")
    checks = run.checks()
    assert dict(checks.not_scored) == {
        "customer.lifetime:cu_start.churn": REASON_COLUMN,
        "account.within:customer.a_open": REASON_COLUMN,
    }
    scored = set(_by_name(checks))
    assert scored == {"account.lifetime:a_open.a_close"}  # never counted as passing
    assert not any(name in scored for name, _ in checks.not_scored)
    assert (
        "  facts: not scored -- customer.lifetime:cu_start.churn: " + REASON_COLUMN
        in checks.lines()
    )


def test_a_fact_over_a_hidden_table_is_listed_as_not_scored_at_any_level_of_its_chain(
    tmp_path: Path,
) -> None:
    run = _hidden_run(
        tmp_path,
        CHAIN,
        lambda t: t.replace(
            "      customer:\n        rows: 60\n",
            "      customer:\n        hidden: true\n        rows: 60\n",
        ),
        "hidden-table",
    )
    checks = run.checks()
    assert dict(checks.not_scored) == {
        "customer.lifetime:cu_start.cu_end": REASON_TABLE,
        "account.within:customer.a_open": REASON_TABLE,
        "invoice.within:account.customer.i_issued": REASON_TABLE,
    }
    # the levels that never read the hidden table are scored
    assert {
        "invoice.within:account.i_issued",
        "payment.within:invoice.p_paid",
        "payment.within:invoice.account.p_paid",
    } <= set(_by_name(checks))


def test_verify_lists_the_not_scored_facts_in_its_lines_and_its_artifact(tmp_path: Path) -> None:
    run = _hidden_run(tmp_path, HIDDEN_COLUMN_KEYS, _with_hidden_column, "hidden-verify")
    result = run.verified()
    lines = result.stdout.splitlines()
    assert f"  facts: not scored -- account.within:customer.a_open: {REASON_COLUMN}" in lines
    document = result.artifacts["rules"]
    assert document["notScored"] == [
        {"fact": "account.within:customer.a_open", "reason": REASON_COLUMN},
        {"fact": "customer.lifetime:cu_start.churn", "reason": REASON_COLUMN},
    ]
    listed = {entry["rule"] for rules in document["emits"].values() for entry in rules}
    assert not any(
        name.split(".", 1)[1] in listed for name in ("customer.lifetime:cu_start.churn",)
    )


# ---------------------------------------------------------------------------
# The stage, the command line, and a bundle without the keys
# ---------------------------------------------------------------------------


def test_verify_prints_and_records_a_temporal_line_for_each_fact(tmp_path: Path) -> None:
    run = _make(tmp_path)
    _clean(run)
    checks = run.checks()
    assert "  rules invoice: within:account.i_issued 900 of 900 (100.00%); " in "\n".join(
        checks.lines()
    ) or any(line.startswith("  rules invoice: within:account.i_issued") for line in checks.lines())
    document = checks.artifact()["rules"]["emits"]["invoice"]
    assert document[0] == {
        "rule": "within:account.i_issued",
        "form": "check",
        "origin": "bundle",
        "enforced": True,
        "satisfied": 900,
        "total": 900,
        "share": "1",
        "met": True,
    }


def test_the_command_line_verifies_a_run_of_the_fixture_and_names_every_fact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = copy_uncut(tmp_path, "cli", CHAIN)
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.2.0"]) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    assert main(["-j", "apply", str(bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    code = main(
        [
            "verify",
            str(bundle),
            "--state-dir",
            state,
            "--run-id",
            run_id,
            "--allow-declared-baseline-drift",
        ]
    )
    out = capsys.readouterr().out
    assert code in (0, 1)
    for name in CHAIN_FACTS:
        expose, rule = name.split(".", 1)
        assert f"  rules {expose}: " in out and rule in out
    assert "not scored" not in out


def test_a_bundle_without_the_keys_verifies_as_it_did(tmp_path: Path) -> None:
    run = _make(tmp_path, keys="", name="plain")
    result = run.verified()
    assert "rules" not in result.artifacts
    assert all(
        "  rules" not in line and "not scored" not in line for line in result.stdout.splitlines()
    )
    checks = run.checks()
    assert not checks.rules and not checks.not_scored and checks.artifact().get("rules") is None


def test_a_run_whose_rows_moved_is_not_scored_against_them(tmp_path: Path) -> None:
    run = _make(tmp_path)
    _clean(run)
    result = run.verified()
    assert result.exit_code == 1
    assert "rules and integrity: not checked" in result.stdout
    assert "within:" not in result.stdout


# ---------------------------------------------------------------------------
# The evidence pack
# ---------------------------------------------------------------------------


def test_the_evidence_report_lists_the_facts_that_were_not_scored(tmp_path: Path) -> None:
    from flux_engine.concordance.scorecard import TableInput, build_scorecard
    from flux_engine.core.digest import digest_of_document
    from flux_engine.stages.ship import _scored_run, _write_evidence_report

    run = _hidden_run(tmp_path, HIDDEN_COLUMN_KEYS, _with_hidden_column, "hidden-evidence")
    checks = run.checks()
    scorecard = build_scorecard(
        run_id=run.layout.run_id,
        plan_digest="sha256:" + "0" * 64,
        run_digest="sha256:" + "1" * 64,
        profile_digest="sha256:" + "2" * 64,
        tables=[TableInput(expose_id="customer")],
    )
    scored = _scored_run(run.layout, scorecard, {}, (), checks)
    assert scored is not None and dict(scored.not_scored) == dict(checks.not_scored)
    run.layout.run_dir.mkdir(parents=True, exist_ok=True)
    _write_evidence_report(
        run.layout,
        bundle_name="billing",
        kernel_version="0.2.0",
        run_digest=digest_of_document({"run": 1}, subject="runDigest"),
        generated_at="2026-11-22T00:00:00+00:00",
        decisions={},
        ephemeral_signing=True,
        self_attested=True,
        scored=scored,
    )
    text = (run.layout.run_dir / "EVIDENCE.md").read_text("utf-8")
    assert f"- fact `account.within:customer.a_open` not scored: {REASON_COLUMN}" in text
    assert f"- fact `customer.lifetime:cu_start.churn` not scored: {REASON_COLUMN}" in text


def test_the_evidence_report_of_a_run_with_nothing_unscored_adds_no_line(tmp_path: Path) -> None:
    from flux_engine.concordance.scorecard import TableInput, build_scorecard
    from flux_engine.stages.ship import _scorecard_summary_text, _scored_run

    run = _make(tmp_path)
    _clean(run)
    scorecard = build_scorecard(
        run_id=run.layout.run_id,
        plan_digest="sha256:" + "0" * 64,
        run_digest="sha256:" + "1" * 64,
        profile_digest="sha256:" + "2" * 64,
        tables=[TableInput(expose_id="customer")],
    )
    scored = _scored_run(run.layout, scorecard, {}, (), run.checks())
    assert scored is not None and scored.not_scored == ()
    assert _scorecard_summary_text(scorecard, scored.not_scored) == _scorecard_summary_text(
        scorecard
    )
    assert "not scored" not in _scorecard_summary_text(scorecard)
