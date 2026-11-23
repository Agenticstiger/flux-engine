# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's conditions (ADR 0028 section 10): declared in the FLUX bundle, bound into ``H``.

The bundle is ``tests/fixtures/conditions``: one table of subscriptions whose categorical columns
are drawn through a chain of conditionals. A bundle that declares no conditions hashes as it
always did; a group moves ``H`` of its emit and never ``G``. What a column can hold is checked at
``plan`` and ``validate``, where the columns are known, and kernel 0.1.0 refuses the key.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1
from flux_engine.seam.conditions import (
    FLX_3640,
    FLX_3641,
    FLX_3642,
    FLX_3643,
    FLX_3644,
    FLX_3645,
    ConditionGroup,
    bound_into_h,
    conditions_of,
    read_conditions_block,
)
from flux_engine.seam.emit import seam_digests
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from flux_engine.stages import pipeline
from flux_engine.stages.validate import validate
from tests.stages.test_shared_columns_run import _bundle as _star

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "conditions"
NAMESPACE = "  com.agentics.flux/1:\n"
SIMULATION = "sim-plans"
EMIT = "subscriptions"

TWO_GROUPS = (
    "conditions:\n"
    "  subscriptions:\n"
    "    - { values: { plan: premium, region: EU }, count: 400 }\n"
    "    - { values: { plan: basic }, count: 600 }\n"
)


def bundle_with(
    tmp_path: Path, conditions: str, name: str = "bundle", *, rows: Optional[int] = None
) -> Path:
    """A copy of the fixture whose namespace declares *conditions* (the text of the key)."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count(NAMESPACE) == 1
    block = "".join(f"    {line}\n" for line in conditions.splitlines())
    text = text.replace(NAMESPACE, NAMESPACE + block, 1)
    if rows is not None:
        assert text.count("        rows: 1000\n") == 1
        text = text.replace("        rows: 1000\n", f"        rows: {rows}\n", 1)
    simulation.write_text(text, "utf-8")
    return target


def _digests(bundle: Path) -> Dict[str, Tuple[str, str]]:
    loaded = load_bundle(bundle)
    found = {}
    for resolved in resolve_all_emits(loaded, simulation_id=SIMULATION):
        digests = seam_digests(
            resolved.contract_document,
            resolved.emit,
            resolved.compile().column_names,
            resolved.extension,
        )
        found[resolved.expose_id] = (digests.h.value, digests.g.value)
    return found


@pytest.fixture(scope="module")
def plain() -> Dict[str, Tuple[str, str]]:
    return _digests(FIXTURE)


def test_a_bundle_that_declares_no_conditions_keeps_its_digests(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(FIXTURE, copy)
    assert _digests(copy) == plain


def test_the_groups_move_h_of_their_emit_and_never_g(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    moved = _digests(bundle_with(tmp_path, TWO_GROUPS))
    (h, g), (moved_h, moved_g) = plain[EMIT], moved[EMIT]
    assert moved_h != h
    assert moved_g == g


def test_an_edit_of_a_value_a_count_or_the_order_moves_h_again(tmp_path: Path) -> None:
    base = _digests(bundle_with(tmp_path, TWO_GROUPS, "base"))[EMIT]
    edits = {
        "value": TWO_GROUPS.replace("region: EU", "region: US"),
        "count": TWO_GROUPS.replace("count: 400", "count: 300").replace("count: 600", "count: 700"),
        "order": (
            "conditions:\n  subscriptions:\n"
            "    - { values: { plan: basic }, count: 600 }\n"
            "    - { values: { plan: premium, region: EU }, count: 400 }\n"
        ),
    }
    for name, text in edits.items():
        moved = _digests(bundle_with(tmp_path, text, name))[EMIT]
        assert moved[0] != base[0], name
        assert moved[1] == base[1], name


def test_the_member_holds_the_groups_in_their_order() -> None:
    groups = read_conditions_block(
        {
            "subscriptions": [
                {"values": {"region": "EU", "plan": "premium"}, "count": 2},
                {"values": {"plan": "basic"}, "count": 3},
            ]
        },
        emits=frozenset({"subscriptions"}),
        where="conditions",
    )
    member = bound_into_h(groups, "subscriptions")
    assert member == [
        {"values": {"plan": "premium", "region": "EU"}, "count": 2},
        {"values": {"plan": "basic"}, "count": 3},
    ]
    assert bound_into_h(groups, "elsewhere") is None
    assert conditions_of({"conditions": member}) == groups["subscriptions"]
    assert conditions_of(None) == ()
    one = read_conditions_block(
        {"subscriptions": [{"values": {"plan": "basic"}}]},
        emits=frozenset({"subscriptions"}),
        where="conditions",
    )
    assert bound_into_h(one, "subscriptions") == [{"values": {"plan": "basic"}}]
    assert one["subscriptions"][0] == ConditionGroup(values=(("plan", "basic"),))


def test_names_are_read_in_nfc_and_a_list_past_its_bound_is_refused() -> None:
    read = read_conditions_block(
        {"café": [{"values": {"café": "x"}, "count": 1}]},
        emits=frozenset({"café"}),
        where="conditions",
    )
    assert read["café"][0].values == (("café", "x"),)
    big = {"a": [{"values": {"c": "x"}, "count": 1}] * 100_001}
    with pytest.raises(FluxError) as refused:
        read_conditions_block(big, emits=frozenset({"a"}), where="conditions")
    assert refused.value.code == FLX_3640
    edge = {"a": [{"values": {"c": "x"}, "count": 1}] * 100_000}
    assert len(read_conditions_block(edge, emits=frozenset({"a"}), where="c")["a"]) == 100_000


def _refused(tmp_path: Path, conditions: str, **extra: object) -> FluxError:
    bundle = bundle_with(tmp_path, conditions, "bad", **extra)  # type: ignore[arg-type]
    with pytest.raises(FluxError) as refused:
        resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    return refused.value


@pytest.mark.parametrize(
    ("conditions", "problem"),
    [
        ("conditions: []", "is a mapping"),
        ("conditions:\n  nowhere:\n    - { values: { plan: basic }, count: 1 }", "not an emit"),
        ("conditions:\n  subscriptions: {}", "non-empty list of groups"),
        ("conditions:\n  subscriptions: []", "non-empty list of groups"),
        ("conditions:\n  subscriptions:\n    - nope", "is a mapping"),
        (
            "conditions:\n  subscriptions:\n    - { values: { plan: basic }, count: 1, x: 1 }",
            "['x']",
        ),
        ("conditions:\n  subscriptions:\n    - { count: 1 }", "declares no values"),
        (
            "conditions:\n  subscriptions:\n    - { values: {}, count: 1 }",
            "non-empty mapping",
        ),
        (
            "conditions:\n  subscriptions:\n    - { values: { plan: 1 }, count: 1 }",
            "write it as a string",
        ),
        (
            "conditions:\n  subscriptions:\n    - { values: [plan], count: 1 }",
            "non-empty mapping",
        ),
    ],
)
def test_a_malformed_declaration_is_refused_before_any_row(
    tmp_path: Path, conditions: str, problem: str
) -> None:
    refused = _refused(tmp_path, conditions)
    assert refused.code == FLX_3640
    assert problem in str(refused)


def test_a_malformed_declaration_names_the_emit_and_the_group(tmp_path: Path) -> None:
    text = (
        "conditions:\n  subscriptions:\n"
        "    - { values: { plan: basic }, count: 1 }\n"
        "    - { values: { plan: 7 }, count: 1 }\n"
    )
    message = str(_refused(tmp_path, text))
    assert "subscriptions[1]" in message
    assert "values.plan" in message


@pytest.mark.parametrize(
    "count",
    ["0", "-3", "1.5", "'4'", "true", "null"],
)
def test_a_count_that_is_not_a_whole_number_of_at_least_one_is_refused(
    tmp_path: Path, count: str
) -> None:
    text = f"conditions:\n  subscriptions:\n    - {{ values: {{ plan: basic }}, count: {count} }}\n"
    refused = _refused(tmp_path, text)
    assert refused.code == FLX_3641
    assert "whole number of at least 1" in str(refused)


def test_only_an_emits_one_group_may_leave_its_count_out(tmp_path: Path) -> None:
    text = (
        "conditions:\n  subscriptions:\n"
        "    - { values: { plan: basic }, count: 600 }\n"
        "    - { values: { plan: premium } }\n"
    )
    refused = _refused(tmp_path, text)
    assert refused.code == FLX_3641
    assert "subscriptions[1]" in str(refused)
    alone = bundle_with(
        tmp_path, "conditions:\n  subscriptions:\n    - { values: { plan: basic } }"
    )
    resolve_all_emits(load_bundle(alone), simulation_id=SIMULATION)


def test_a_declared_row_count_that_is_not_the_sum_of_the_counts_is_refused(
    tmp_path: Path,
) -> None:
    refused = _refused(tmp_path, TWO_GROUPS, rows=999)
    assert refused.code == FLX_3641
    assert "count 1000 rows" in str(refused)
    assert "declares 999" in str(refused)
    ok = bundle_with(tmp_path, TWO_GROUPS, "ok", rows=1000)
    resolve_all_emits(load_bundle(ok), simulation_id=SIMULATION)


def test_a_group_whose_count_is_left_out_covers_whatever_rows_the_emit_declares(
    tmp_path: Path,
) -> None:
    text = "conditions:\n  subscriptions:\n    - { values: { plan: basic } }"
    for rows in (1, 1000):
        resolve_all_emits(
            load_bundle(bundle_with(tmp_path, text, f"rows{rows}", rows=rows)),
            simulation_id=SIMULATION,
        )


def test_a_column_the_expose_does_not_have_is_refused_naming_the_group(tmp_path: Path) -> None:
    text = (
        "conditions:\n  subscriptions:\n"
        "    - { values: { plan: basic }, count: 600 }\n"
        "    - { values: { colour: red }, count: 400 }\n"
    )
    refused = _refused(tmp_path, text)
    assert refused.code == FLX_3642
    message = str(refused)
    assert "subscriptions" in message and "group 1" in message and "'colour'" in message


def _plan_refusal(tmp_path: Path, conditions: str, name: str = "plan") -> FluxError:
    bundle = bundle_with(tmp_path, conditions, name)
    with pytest.raises(FluxError) as refused:
        pipeline.execute(bundle)
    return refused.value


@pytest.mark.parametrize(
    ("values", "code", "problem"),
    [
        ("{ plan: gold }", FLX_3642, "'gold' is not a level of 'plan'"),
        ("{ region: Europe }", FLX_3642, "not a level of 'region'"),
        ("{ plan: Basic }", FLX_3642, "not a level of 'plan'"),
        ("{ sub_id: abc }", FLX_3643, "it is an identifier"),
        ("{ spend: '1' }", FLX_3643, "not categorical"),
        ("{ note: a }", FLX_3643, "nullFraction"),
    ],
)
def test_plan_refuses_what_a_column_cannot_hold(
    tmp_path: Path, values: str, code: object, problem: str
) -> None:
    text = f"conditions:\n  subscriptions:\n    - {{ values: {values}, count: 1000 }}\n"
    refused = _plan_refusal(tmp_path, text)
    assert refused.code == code
    message = str(refused)
    assert problem in message
    assert "subscriptions" in message and "group 0" in message


def test_a_numeric_column_that_declares_a_null_fraction_is_refused_as_not_categorical(
    tmp_path: Path,
) -> None:
    """``init`` writes a nullFraction on every column, so that is not why a number cannot be fixed."""
    bundle = bundle_with(
        tmp_path, "conditions:\n  subscriptions:\n    - { values: { spend: '1' }, count: 1000 }\n"
    )
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    old = "            spend:\n              range:"
    assert text.count(old) == 1
    simulation.write_text(
        text.replace(
            old, "            spend:\n              nullFraction: '0'\n              range:"
        ),
        "utf-8",
    )
    with pytest.raises(FluxError) as refused:
        pipeline.execute(bundle)
    assert refused.value.code == FLX_3643
    assert "it is not categorical" in str(refused.value)
    assert "nullFraction" not in str(refused.value)


def _labelled(
    tmp_path: Path, label: str, conditions: str, *, raise_to: str = "", name: str = "bundle"
) -> Path:
    bundle = bundle_with(tmp_path, conditions + raise_to, name)
    contract = bundle / "plans.fluid.yml"
    text = contract.read_text("utf-8")
    old = "{ name: tier, type: STRING, required: false }"
    assert old in text
    contract.write_text(
        text.replace(old, f"{{ name: tier, type: STRING, required: false, sensitivity: {label} }}"),
        "utf-8",
    )
    return bundle


@pytest.mark.parametrize("label", ["pii", "phi", "restricted", "pseudonymized", "tokenized"])
def test_a_column_whose_sensitivity_needs_grade_p3_cannot_be_fixed(
    tmp_path: Path, label: str
) -> None:
    text = "conditions:\n  subscriptions:\n    - { values: { tier: gold }, count: 1000 }\n"
    bundle = _labelled(tmp_path, label, text)
    with pytest.raises(FluxError) as refused:
        pipeline.execute(bundle)
    assert refused.value.code == FLX_3645
    message = str(refused.value)
    assert "subscriptions" in message and "group 0" in message
    assert "'tier'" in message and label in message
    assert validate(bundle).error_code == FLX_3645.code


def test_a_label_below_p3_can_be_fixed_until_the_simulation_raises_it(tmp_path: Path) -> None:
    text = "conditions:\n  subscriptions:\n    - { values: { tier: gold }, count: 1000 }\n"
    assert validate(_labelled(tmp_path, "internal", text)).exit_code == 0
    raised = "privacy:\n  minimumGrade: { internal: P3 }\n"
    refused = validate(_labelled(tmp_path, "internal", text, raise_to=raised, name="raised"))
    assert refused.error_code == FLX_3645.code


def test_the_levels_a_column_draws_are_the_levels_a_group_may_take(tmp_path: Path) -> None:
    for level in ("APAC", "EU", "US"):
        text = f"conditions:\n  subscriptions:\n    - {{ values: {{ region: {level} }} }}\n"
        bundle = bundle_with(tmp_path, text, f"level-{level}")
        assert validate(bundle).exit_code == 0


def test_validate_refuses_what_plan_refuses(tmp_path: Path) -> None:
    text = "conditions:\n  subscriptions:\n    - { values: { sub_id: abc }, count: 1000 }\n"
    result = validate(bundle_with(tmp_path, text))
    assert result.exit_code == 1
    assert result.error_code == FLX_3643.code
    assert "identifier" in result.stderr


def test_a_relationship_column_cannot_be_fixed(tmp_path: Path) -> None:
    # The billing star's invoice has a foreign key into account: the relational plan decides it.
    bundle = _star(tmp_path, rows=50, name="star")
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    block = "    conditions:\n      invoice:\n        - { values: { i_account: x } }\n"
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + block, 1), "utf-8")
    with pytest.raises(FluxError) as refused:
        pipeline.execute(bundle)
    assert refused.value.code == FLX_3643
    assert "a key or a column of a relationship" in str(refused.value)


def test_an_emit_drawn_from_the_runs_own_events_has_no_column_to_fix(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"
    target = tmp_path / "bundle"
    shutil.copytree(fixture, target)
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    text = text.replace(
        "    emits:\n      payment_recovery_moment:",
        "    conditions:\n      payment_recovery_moment:\n"
        "        - { values: { market: IE } }\n"
        "    emits:\n      payment_recovery_moment:",
        1,
    )
    # Drop the fidelity block, so the emit is the run's own.
    head, _, tail = text.partition("        fidelity:")
    _, _, rest = tail.partition("agentPolicy:")
    simulation.write_text(head + "agentPolicy:" + rest, "utf-8")
    with pytest.raises(FluxError) as refused:
        pipeline.execute(target)
    assert refused.value.code == FLX_3644


def test_kernel_0_1_0_refuses_the_key_naming_the_kernel(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, TWO_GROUPS)
    kernel = KERNEL_V1
    with pytest.raises(FluxError) as refused:
        pipeline.execute(bundle, kernel=kernel)
    assert refused.value.code == FLX_3105
    assert "kernel 0.1.0" in str(refused.value)
    assert "conditions" in str(refused.value)


def test_a_bundle_without_the_key_still_runs_under_kernel_0_1_0(tmp_path: Path) -> None:
    result = pipeline.execute(FIXTURE, kernel=KERNEL_V1)
    assert result.emits[0].resolved.expose_id == EMIT


def test_an_unknown_namespace_key_is_still_refused(tmp_path: Path) -> None:
    refused = _refused(tmp_path, "conditionz: {}")
    assert refused.code == FLX_3105
    assert "'conditions'" in str(refused)


def test_the_seed_row_example_of_the_how_to_draws_the_other_rows_from_the_whole_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The page's example fixes one row and gives every other row a group, so none is left free."""
    import json
    import re

    from flux_engine.cli import main

    page = (
        Path(__file__).resolve().parents[2] / "docs" / "how-to" / "fix-values-and-seed-rows.md"
    ).read_text("utf-8")
    start = page.index("### Seed rows")
    block = re.search(r"```yaml\n(conditions:\n.*?)```", page[start:], re.S)
    assert block is not None
    bundle = bundle_with(tmp_path, block.group(1).rstrip("\n"))
    state = tmp_path / "state"
    capsys.readouterr()
    assert main(["plan", str(bundle), "--state-dir", str(state), "--json"]) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    assert main(["apply", str(bundle), "--run-id", run_id, "--state-dir", str(state)]) == 0
    stream = state / "runs" / run_id / "emits" / "subscriptions.ndjson"
    rows = [json.loads(line) for line in stream.read_text("utf-8").splitlines()]
    assert len(rows) == 1000
    assert rows[0][1:5] == ["premium", "US", "online", "gold"]
    others = [row[1] for row in rows[1:]]
    # Not "the other 999 rows are basic": the plan keeps its declared 0.4 / 0.6 shares.
    assert others.count("premium") == 399 and others.count("basic") == 600


# ---------------------------------------------------------------------------
# What the groups would not decide: allocation, a history, a projection
# ---------------------------------------------------------------------------

HISTORY_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "history-projections"


def _exact(bundle: Path, column: str) -> Path:
    """*bundle* with ``allocation: exact`` on *column*, which must be a root column."""
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    old = f"            {column}: {{ levels: {{ basic: '0.6', premium: '0.4' }} }}\n"
    assert old in text
    new = f"            {column}: {{ allocation: exact, levels: {{ basic: '0.6', premium: '0.4' }} }}\n"
    simulation.write_text(text.replace(old, new, 1), "utf-8")
    return bundle


def _both_refuse(bundle: Path, *needles: str) -> None:
    """`plan` and `validate` both refuse *bundle* with FLX-3643, naming each of *needles*."""
    with pytest.raises(FluxError) as refused:
        pipeline.execute(bundle)
    assert refused.value.code == FLX_3643
    result = validate(bundle)
    assert result.exit_code == 1
    assert result.error_code == FLX_3643.code
    for message in (str(refused.value), result.stderr):
        for needle in needles:
            assert needle in message


@pytest.mark.parametrize(
    "values", ["{ plan: basic }", "{ region: EU }", "{ tier: gold }", "{ plan: basic, tier: gold }"]
)
def test_an_exact_column_joined_to_a_fixed_column_cannot_hold_a_group(
    tmp_path: Path, values: str
) -> None:
    # `plan` is exact, and region, channel and tier are drawn given it, directly or not.
    text = f"conditions:\n  subscriptions:\n    - {{ values: {values}, count: 1000 }}\n"
    bundle = _exact(bundle_with(tmp_path, text), "plan")
    _both_refuse(bundle, "'subscriptions'", "'plan'", "allocation: exact", "drop the group")


def test_an_exact_column_apart_from_the_fixed_ones_keeps_its_conditions(tmp_path: Path) -> None:
    text = "conditions:\n  subscriptions:\n    - { values: { plan: basic }, count: 1000 }\n"
    bundle = bundle_with(tmp_path, text)
    simulation = bundle / "simulation.flux.yml"
    old = "            note:\n              nullFraction: '0.5'\n"
    assert old in simulation.read_text("utf-8")
    simulation.write_text(
        simulation.read_text("utf-8").replace(
            old, "            note:\n              allocation: exact\n", 1
        ),
        "utf-8",
    )
    assert validate(bundle).exit_code == 0


@pytest.mark.parametrize(
    ("emit", "values", "problem"),
    [
        ("subscription_version", "{ plan: pro }", "declares a history"),
        ("subscription_current", "{ plan: pro }", "projected from a history"),
        ("subscription_change", "{ op: c }", "projected from a history"),
        ("subscription_month", "{ plan: pro }", "projected from a history"),
    ],
)
def test_a_history_emit_and_a_projected_emit_refuse_conditions(
    tmp_path: Path, emit: str, values: str, problem: str
) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(HISTORY_FIXTURE, bundle)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count(NAMESPACE) == 1
    block = f"    conditions:\n      {emit}:\n        - {{ values: {values} }}\n"
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + block, 1), "utf-8")
    _both_refuse(bundle, f"'{emit}'", problem, "Remove the conditions")
