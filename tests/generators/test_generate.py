# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A generator plugin generates the other columns of an expose, and the engine checks them.

ADR 0032 section 5. The expose's identifier and key columns and its foreign keys are the engine's;
the plugin generates the rest, once per expose (per ``--shard`` range), in the engine's own process,
and every value is held to its column before it is written. A plugin stated ``bytes`` gives the
same values for a range however it is split, so the expose's bytes are the same under ``--jobs 1``
and ``--jobs 4``, under a ``--scale`` below 1, and per ``--shard I/N`` against the whole run.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.generator.plugin import (
    PluginExpose,
    generate_values,
    plugin_expose,
    request_of,
)
from flux_engine.generator.statistics import statistics_from_fidelity
from flux_engine.generators import registry
from flux_engine.generators.request import Request
from flux_engine.seam.contract import compile_expose
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate

from .conftest import (
    EXPOSE,
    FIXTURE,
    PACKAGE,
    add_invariants,
    edit_emit,
    plugin_bundle,
)

pytestmark = pytest.mark.integration

LABEL = "learned (fxgen-plugin 1.0)"
HEX32 = re.compile(r"[0-9a-f]{32}")


def _calls() -> List[Any]:
    return list(sys.modules[f"{PACKAGE}.plugin"].CALLS)


def _lines(result: pipeline.PipelineResult, expose_id: str = EXPOSE) -> bytes:
    return result.emit(expose_id).writer.stream_bytes()


def _run(bundle: Path, **options: Any) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, allow_generators=["learned"], **options)


# ---------------------------------------------------------------------------
# plan, apply and verify, end to end
# ---------------------------------------------------------------------------


def test_plan_apply_and_verify_a_plugin_expose(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = str(plugin_bundle(tmp_path, options={"temperature": "1"}))
    state = ["--state-dir", str(tmp_path / "state")]
    assert main(["--json", "plan", bundle, *state, "--allow-generator", "learned"]) == 0
    artifacts = json.loads(capsys.readouterr().out)["artifacts"]
    run_id = artifacts["run_id"]
    assert main(["apply", bundle, *state, "--run-id", run_id]) == 0
    assert "payment_recovery_moment: 64 rows" in capsys.readouterr().out
    assert main(["verify", bundle, *state, "--run-id", run_id]) == 0
    assert "rows: ok" in capsys.readouterr().out
    # The plugin was called once, for the whole expose, when the plan generated it.
    assert _calls() == [(0, 64)]

    layout = RunLayout.for_state_dir(tmp_path / "state", run_id)
    rows = [json.loads(line) for line in layout.emit_path(EXPOSE).read_text("utf-8").splitlines()]
    assert len(rows) == 64
    identifiers = [row[0] for row in rows]
    assert all(HEX32.fullmatch(value) for value in identifiers) and len(set(identifiers)) == 64
    for _, moment, amount, market in rows:
        assert "2026-07-01T00:00:00.000Z" <= moment <= "2026-09-30T23:59:59.000Z"
        assert (
            Decimal("1") <= Decimal(amount) <= Decimal("10000") and len(amount.split(".")[1]) == 4
        )
        assert market in ("IE", "GB", "FR", "DE")


def test_the_engine_renders_what_the_plugin_returns(installed: Path, tmp_path: Path) -> None:
    """The plugin hands back datetimes and decimals; the lines are the engine's own spelling."""
    result = _run(plugin_bundle(tmp_path))
    first = json.loads(_lines(result).splitlines()[0])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", first[1])
    assert re.fullmatch(r"\d+\.\d{4}", first[2])


def test_the_columns_a_plugin_makes_are_declared_by_the_plugin_in_the_provenance(
    installed: Path, tmp_path: Path
) -> None:
    result = _run(plugin_bundle(tmp_path))
    coverage = result.emit(EXPOSE).coverage
    assert coverage is not None
    found = {row.column: (row.origin.value, row.decided_by, row.source) for row in coverage.columns}
    assert found["subscriber_id"] == ("derived", "identifier", "H+rowIndex")
    for column in ("aborted_at", "amount", "market"):
        assert found[column] == ("declared", "plugin", "generators")


def test_the_plan_binds_the_columns_the_plugin_makes(installed: Path, tmp_path: Path) -> None:
    result = _run(plugin_bundle(tmp_path))
    plan_ = result.emit(EXPOSE).plan
    assert plan_ is not None and plan_.plugin is not None
    assert plan_.as_document()["plugin"] == {"columns": ["aborted_at", "amount", "market"]}


def test_an_expose_with_no_fidelity_block_is_still_generated_by_its_plugin(
    installed: Path, tmp_path: Path
) -> None:
    """The declaration alone puts the expose on the generator path: its rows are the declared count."""
    bundle = plugin_bundle(tmp_path)
    edit_emit(bundle, lambda emit: emit.pop("fidelity"))
    result = _run(bundle)
    lines = _lines(result).splitlines()
    assert len(lines) == 64 and result.emit(EXPOSE).source == "generator"
    first = json.loads(lines[0])
    assert HEX32.fullmatch(first[0]) and first[3].startswith("v")
    assert _calls() == [(0, 64)]


def test_the_seed_a_plugin_is_given_moves_with_the_run_s_root(
    installed: Path, tmp_path: Path
) -> None:
    from flux_engine.core.seedtree import derive_plugin_seed

    result = _run(plugin_bundle(tmp_path))
    seed = derive_plugin_seed(result.root, result.cast.simulation_id, EXPOSE)
    assert len(seed) == 32
    assert seed != derive_plugin_seed(
        result.root.reseed(result.seed + 1), result.cast.simulation_id, EXPOSE
    )
    assert seed != derive_plugin_seed(result.root, result.cast.simulation_id, "other")
    assert seed != derive_plugin_seed(result.root, "other", EXPOSE)


# ---------------------------------------------------------------------------
# What a plugin returns is checked, and nothing is repaired
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "column", "needle"),
    [
        ("type", "market", "row 3 is not a STRING value"),
        ("range", "amount", "row 3 lies outside the range the bundle declares"),
        ("levels", "market", "row 3 is not one of the levels the column may take"),
        ("null", "aborted_at", "row 3 is NULL, and the column is required"),
    ],
)
def test_a_value_the_column_does_not_accept_is_refused_naming_plugin_column_and_row(
    installed: Path, tmp_path: Path, mode: str, column: str, needle: str
) -> None:
    bundle = plugin_bundle(tmp_path, options={"misbehave": mode, "at": "3"})
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3121")
    assert f"{LABEL} for 'payment_recovery_moment': column '{column}', {needle}" in outcome.stderr
    assert not list((tmp_path / "state").glob("runs/*"))


@pytest.mark.parametrize(
    ("mode", "needle"),
    [
        ("short", "column 'market' came back as 63 values; 64 are asked for"),
        ("many", "column 'market' came back as 65 values; 64 are asked for"),
    ],
)
def test_a_list_of_the_wrong_length_is_refused(
    installed: Path, tmp_path: Path, mode: str, needle: str
) -> None:
    bundle = plugin_bundle(tmp_path, options={"misbehave": mode})
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3121")
    assert needle in outcome.stderr and LABEL in outcome.stderr


def test_a_refusal_never_quotes_a_value(installed: Path, tmp_path: Path) -> None:
    bundle = plugin_bundle(tmp_path, options={"misbehave": "levels", "at": "3"})
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert outcome.error_code == "FLX-3121" and "ZZ" not in outcome.stderr


def _expose(declared: str, required: bool, block: Optional[Dict[str, Any]]) -> PluginExpose:
    """The one column ``c`` of a table, as the engine reads its contract and its fidelity block."""
    expose = compile_expose(
        {
            "exposeId": "t",
            "contract": {"schema": [{"name": "c", "type": declared, "required": required}]},
        }
    )
    fidelity = None if block is None else {"columns": {"c": block}}
    return plugin_expose(expose, statistics_from_fidelity(expose, fidelity), frozenset())


class _Returns:
    def __init__(self, value: Any) -> None:
        self.value = value

    def generate(self, request: Request, start: int, stop: int) -> Any:
        return self.value


def _ask(
    returned: Any,
    declared: str = "INT",
    required: bool = False,
    block: Optional[Dict[str, Any]] = None,
    rows: int = 2,
) -> Dict[str, Any]:
    expose = _expose(declared, required, block)
    request = request_of(expose, "t", n=10, seed=b"\x00" * 32, options={}, start=4, stop=4 + rows)
    return generate_values(_Returns(returned), LABEL, expose, request)


_RANGE = {"range": {"min": "1", "max": "5"}}


@pytest.mark.parametrize(
    ("declared", "required", "block", "value", "reason"),
    [
        ("INT", False, None, "7", "is not a INT value"),
        ("INT", False, None, True, "is not a INT value"),
        ("INT", False, None, 1.5, "is not a INT value"),
        ("INT", True, None, None, "is NULL, and the column is required"),
        ("INT", False, _RANGE, 6, "outside the range"),
        ("INT", False, _RANGE, 0, "outside the range"),
        ("DECIMAL(7,2)", False, {"range": {"min": "1", "max": "99"}}, Decimal("99.01"), "outside"),
        ("DECIMAL(7,2)", False, None, Decimal("123456.00"), "is not a DECIMAL(7,2) value"),
        (
            "DATE",
            False,
            {"range": {"min": "2026-01-01", "max": "2026-12-31"}},
            "2027-01-01",
            "outside the range",
        ),
        (
            "TIMESTAMP",
            False,
            {"range": {"min": "2026-01-01T00:00:00Z", "max": "2026-12-31T00:00:00Z"}},
            "2025-01-01T00:00:00Z",
            "outside the range",
        ),
        ("STRING", False, {"support": {"values": ["a", "b"]}}, "c", "not one of the levels"),
        ("enum('a','b')", False, None, "c", "not one of the levels"),
        ("varchar(3)", False, None, "abcd", "longer than the 3 characters"),
        ("STRING", False, {"identifierLength": 2}, "abc", "longer than the 2 characters"),
        ("BOOL", False, None, 1, "is not a BOOL value"),
    ],
)
def test_each_check_refuses_naming_the_column_and_the_first_failing_row(
    declared: str, required: bool, block: Optional[Dict[str, Any]], value: Any, reason: str
) -> None:
    with pytest.raises(FluxError) as refused:
        _ask([[value, value]], declared, required, block)
    message = str(refused.value)
    assert refused.value.code.code == "FLX-3121"
    assert f"{LABEL} for 't': column 'c', row 4 " in message and reason in message


@pytest.mark.parametrize(
    ("declared", "block", "value"),
    [
        ("INT", _RANGE, 1),
        ("INT", _RANGE, 5),
        ("DECIMAL(7,2)", {"range": {"min": "1", "max": "99"}}, Decimal("99.004")),
        ("DATE", {"range": {"min": "2026-01-01", "max": "2026-12-31"}}, "2026-12-31"),
        ("STRING", {"support": {"values": ["a", "b"]}}, "b"),
        ("varchar(3)", None, "abc"),
        ("JSON", None, {"a": [1, 2]}),
        ("BYTES", None, b"\x00\x01"),
    ],
)
def test_a_value_inside_every_bound_is_accepted(
    declared: str, block: Optional[Dict[str, Any]], value: Any
) -> None:
    assert _ask([[value, None]], declared, False, block) == {"c": (value, None)}


def test_the_first_failure_is_the_one_named_and_a_good_value_is_accepted() -> None:
    assert _ask([[1, 5]], block=_RANGE) == {"c": (1, 5)}
    with pytest.raises(FluxError) as refused:
        _ask([[2, 9]], block=_RANGE)
    assert "row 5 " in str(refused.value)
    assert _ask([[]], rows=0) == {"c": ()}


@pytest.mark.parametrize("returned", [None, "ab", [[1, 2], [3, 4]], [], [[1]], [[1, 2, 3]], [7]])
def test_the_shape_of_what_comes_back_is_checked(returned: Any) -> None:
    with pytest.raises(FluxError) as refused:
        _ask(returned)
    assert refused.value.code.code == "FLX-3121" and LABEL in str(refused.value)


def test_a_plugin_that_raises_is_refused_by_the_name_of_its_error() -> None:
    class Raises:
        def generate(self, request: Request, start: int, stop: int) -> Any:
            raise RuntimeError("secret detail")

    expose = _expose("INT", False, None)
    request = request_of(expose, "t", n=2, seed=b"\x00" * 32, options={})
    with pytest.raises(FluxError) as refused:
        generate_values(Raises(), LABEL, expose, request)
    assert "RuntimeError" in str(refused.value) and "secret detail" not in str(refused.value)


# ---------------------------------------------------------------------------
# The contract's checks
# ---------------------------------------------------------------------------


def test_a_check_the_plugin_s_values_satisfy_is_enforced_and_plans(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    add_invariants(bundle, '        - id: positive\n          check: "amount >= 1"\n')
    assert plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",)).exit_code == 0


def test_a_check_a_row_fails_is_refused_naming_plugin_rule_columns_and_row(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    add_invariants(bundle, '        - id: large\n          check: "amount > 9000"\n')
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3121")
    assert re.search(
        r"learned \(fxgen-plugin 1\.0\) for 'payment_recovery_moment': row \d+ does not satisfy "
        r"rule 'large', which reads 'amount'",
        outcome.stderr,
    )


def test_a_check_with_when_covers_only_the_rows_its_condition_selects(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    add_invariants(
        bundle,
        '        - id: only_ie\n          when: "market = \'ZZ\'"\n          check: "amount > 99999"\n',
    )
    assert plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",)).exit_code == 0


# ---------------------------------------------------------------------------
# Never split by jobs; the same bytes under every split
# ---------------------------------------------------------------------------


def test_a_plugin_is_called_once_for_the_expose_whatever_jobs_says(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    one = _run(bundle, jobs=1)
    assert _calls() == [(0, 64)]
    four = _run(bundle, jobs=4)
    assert _calls() == [(0, 64), (0, 64)]
    assert four.emit(EXPOSE).processes == 1
    assert _lines(one) == _lines(four)


def test_the_expose_s_bytes_are_the_same_under_jobs_1_and_jobs_4(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    one, four = _run(bundle, jobs=1), _run(bundle, jobs=4)
    assert _lines(one) == _lines(four)
    assert one.emit(EXPOSE).result.output_digest == four.emit(EXPOSE).result.output_digest


@pytest.mark.parametrize("scale", ["0.5", "0.25"])
def test_the_expose_s_bytes_are_the_same_under_a_scale_below_one(
    installed: Path, tmp_path: Path, scale: str
) -> None:
    bundle = plugin_bundle(tmp_path)
    rows = int(64 * Decimal(scale))
    one, four = _run(bundle, scale=Decimal(scale), jobs=1), _run(
        bundle, scale=Decimal(scale), jobs=4
    )
    assert len(_lines(one).splitlines()) == rows
    assert _lines(one) == _lines(four)
    assert _calls()[0] == (0, rows)
    # The plugin is asked for the scaled expose, and the plan records the scale it ran at.
    assert one.scale == Decimal(scale)


@pytest.mark.parametrize("count", [1, 2, 3, 5])
@pytest.mark.parametrize("scale", [None, "0.5"])
def test_the_shares_joined_in_order_are_the_bytes_of_the_whole_run(
    installed: Path, tmp_path: Path, count: int, scale: Optional[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    options: Dict[str, Any] = {} if scale is None else {"scale": Decimal(scale)}
    whole = _run(bundle, **options)
    sys.modules[f"{PACKAGE}.plugin"].CALLS.clear()
    parts = [_run(bundle, shard=(index, count), jobs=4, **options) for index in range(1, count + 1)]
    assert b"".join(_lines(part) for part in parts) == _lines(whole)
    # Each share asked the plugin for its own range, once.
    rows = len(_lines(whole).splitlines())
    calls = _calls()
    assert len(calls) == count and calls[0][0] == 0 and calls[-1][1] == rows
    assert all(later[0] == earlier[1] for earlier, later in zip(calls, calls[1:], strict=False))


def test_apply_shard_of_a_stored_run_is_refused_and_generates_nothing(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = str(plugin_bundle(tmp_path))
    state = ["--state-dir", str(tmp_path / "state")]
    assert main(["--json", "plan", bundle, *state, "--allow-generator", "learned"]) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    sys.modules[f"{PACKAGE}.plugin"].CALLS.clear()
    layout = RunLayout.for_state_dir(tmp_path / "state", run_id)
    for flags in ([], ["--allow-generator", "learned"]):
        for index in (1, 2, 3):
            command = ["apply", bundle, *state, "--run-id", run_id, "--shard", f"{index}/3"]
            assert main([*command, *flags]) == 1
            err = capsys.readouterr().err
            assert "FLX-3123" in err and "without --shard" in err
            assert not layout.shard_emit_path(EXPOSE, index, 3).exists()
    assert _calls() == [], "a refused share never asks the plugin for rows"
    # The stage is still there, so the whole run commits from it.
    assert main(["apply", bundle, *state, "--run-id", run_id]) == 0
    assert _calls() == []


def test_a_range_of_a_bytes_plugin_is_the_same_however_it_is_split(
    installed: Path, tmp_path: Path
) -> None:
    result = _run(plugin_bundle(tmp_path))
    outcome = result.emit(EXPOSE)
    assert outcome.plan is not None and outcome.plan.plugin is not None
    sys.path.insert(0, str(installed))
    try:
        loaded = registry.load(registry.resolve("learned", "fxgen-plugin", "1.0"))
    finally:
        sys.path.remove(str(installed))
    plugin = loaded({})
    request = request_of(outcome.plan.plugin, EXPOSE, n=64, seed=bytes(range(32)), options={})

    def values(start: int, stop: int) -> List[List[Any]]:
        return plugin.generate(request.covering(start, stop), start, stop)  # type: ignore[no-any-return]

    def joined(*bounds: int) -> List[List[Any]]:
        parts = [values(a, b) for a, b in zip(bounds, bounds[1:], strict=False)]
        return [sum((part[column] for part in parts), []) for column in range(len(parts[0]))]

    assert values(0, 64) == joined(0, 20, 64) == joined(0, 1, 2, 3, 40, 64) == joined(0, 0, 64)
    assert values(7, 31) == joined(7, 13, 31) and values(5, 5) == [[], [], []]
    assert values(0, 64) != plugin.generate(
        request_of(
            outcome.plan.plugin, EXPOSE, n=64, seed=bytes(range(1, 33)), options={}
        ).covering(0, 64),
        0,
        64,
    )


# ---------------------------------------------------------------------------
# A bundle without generators keeps its digests
# ---------------------------------------------------------------------------


def test_a_bundle_without_generators_plans_with_no_plugin_in_its_plan(tmp_path: Path) -> None:
    outcome = plan(FIXTURE, state_dir=tmp_path / "state", stage=False)
    assert outcome.exit_code == 0, outcome.stderr
    document = outcome.artifacts["plan_document"]
    assert "plugin" not in json.dumps(document) and "generators" not in json.dumps(document)
    assert "privacyGrade" not in document


# ---------------------------------------------------------------------------
# What this release cannot combine with a plugin expose is refused at validate
# ---------------------------------------------------------------------------


def _refused(bundle: Path) -> str:
    outcome = validate(bundle)
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3105"), outcome.stderr
    return outcome.stderr


def test_a_rule_that_sets_a_plugin_column_is_refused_at_validate(tmp_path: Path) -> None:
    bundle = plugin_bundle(tmp_path)
    add_invariants(bundle, '        - id: fixed\n          derive: amount\n          expr: "1"\n')
    message = _refused(bundle)
    assert (
        "a rule or a copy sets 'amount'" in message and "emit 'payment_recovery_moment'" in message
    )


def test_a_check_with_a_band_is_refused_at_validate(tmp_path: Path) -> None:
    bundle = plugin_bundle(tmp_path)
    add_invariants(
        bundle,
        '        - id: large\n          check: "amount > 9000"\n          mustBeLessThan: 5\n'
        "          unit: percent\n",
    )
    assert "rule 'large' declares a band" in _refused(bundle)


def test_a_column_drawn_given_another_is_refused_at_validate(tmp_path: Path) -> None:
    bundle = plugin_bundle(tmp_path)

    def given(emit: Dict[str, Any]) -> None:
        emit["fidelity"]["columns"]["amount"] = {
            "bins": ["1", "5000", "10000"],
            "given": "market",
            "binWeightsGiven": {level: ["1", "1"] for level in ("IE", "GB", "FR", "DE")},
        }

    edit_emit(bundle, given)
    assert "column 'amount' is drawn given another column" in _refused(bundle)


def test_a_reference_expose_cannot_be_a_plugin_expose(tmp_path: Path) -> None:
    bundle = plugin_bundle(tmp_path)
    edit_emit(bundle, lambda emit: emit.update(reference=True))
    message = validate(bundle).stderr
    assert "reference" in message and "FLX-3105" in message


def test_an_expose_with_nothing_for_a_plugin_to_generate_is_refused() -> None:
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits
    from flux_engine.stages._plugin_exposes import refuse_plugin_combinations

    (resolved,) = resolve_all_emits(load_bundle(FIXTURE), simulation_id="sim-payment-recovery")
    with pytest.raises(FluxError) as refused:
        refuse_plugin_combinations(
            resolved, None, {EXPOSE: PluginExpose(())}, where="sim-payment-recovery"
        )
    assert refused.value.code.code == "FLX-3105"
    assert "nothing for the plugin to generate" in str(refused.value)


def test_a_refusal_never_prints_the_simulation_id_raw() -> None:
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits
    from flux_engine.stages._plugin_exposes import refuse_plugin_combinations

    (resolved,) = resolve_all_emits(load_bundle(FIXTURE), simulation_id="sim-payment-recovery")
    with pytest.raises(FluxError) as refused:
        refuse_plugin_combinations(
            resolved, None, {EXPOSE: PluginExpose(())}, where="sim\x1b[2J\nFLX-0000: forged\u202e"
        )
    message = str(refused.value)
    assert "\x1b" not in message and "\u202e" not in message and "\n" not in message
    assert "forged" in message


STAR = Path(__file__).resolve().parents[1] / "fixtures" / "related-star"


def _star(tmp_path: Path, *, reads_item: bool = False) -> Path:
    """The related star with its ``item`` expose made by the test plugin."""
    import yaml

    from .conftest import NAMESPACE

    target = tmp_path / "star"
    shutil.copytree(STAR, target)
    simulation = target / "simulation.flux.yml"
    document = yaml.safe_load(simulation.read_text(encoding="utf-8"))
    document["extensions"][NAMESPACE]["generators"] = {
        "item": {
            "plugin": "learned",
            "distribution": "fxgen-plugin",
            "version": "1.0",
            "grade": "P5",
            "reproducibility": "bytes",
        }
    }
    block = document["extensions"][NAMESPACE]["emits"]["item"]["fidelity"]
    # The item's price is the plugin's, so the property that checks its mean is replaced by one
    # about its brands, which the levels keep.
    block["properties"] = [
        {"property": "support", "column": "i_brand", "tolerance": {"type": "count", "max": 0}}
    ]
    simulation.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    if reads_item:
        contract = target / "store-star.fluid.yml"
        text = contract.read_text(encoding="utf-8")
        anchor = '        - { name: ss_net_paid, type: "DECIMAL(7,2)", required: true }\n'
        assert text.count(anchor) == 1
        contract.write_text(
            text.replace(
                anchor,
                anchor + "      invariants:\n        - id: net\n          derive: ss_net_paid\n"
                '          expr: "item.i_price * ss_quantity"\n',
            ),
            encoding="utf-8",
        )
        _repin(contract, simulation)
    return target


def _repin(contract: Path, simulation: Path) -> None:
    import yaml

    from flux_engine.core.digest import digest_of_document
    from flux_engine.seam.resolve import load_documents_exact

    (document,) = [
        doc for doc in load_documents_exact(contract) if doc.get("id") == "retail.store_star"
    ]
    parsed = yaml.safe_load(simulation.read_text(encoding="utf-8"))
    for emit in parsed["spec"]["emits"]:
        emit["provenance"]["contractDigest"] = digest_of_document(
            document, subject="contractDigest:retail.store_star"
        ).value
    simulation.write_text(yaml.safe_dump(parsed, sort_keys=False), encoding="utf-8")


def test_a_foreign_key_into_a_plugin_expose_names_a_row_the_plugin_expose_holds(
    installed: Path, tmp_path: Path
) -> None:
    result = _run(_star(tmp_path))
    items = result.emit("item")
    keys = {row[0] for row in items.rows}
    assert len(items.rows) == 200 and keys == set(range(1, 201))
    assert {row[1] for row in items.rows} <= {"acme", "globex", "initech"}
    sales = result.emit("store_sales")
    names = sales.expose.column_names
    assert {row[names.index("ss_item_sk")] for row in sales.rows} <= keys


def test_a_rule_that_reads_a_plugin_expose_through_a_link_is_refused_at_validate(
    installed: Path, tmp_path: Path
) -> None:
    plain = _star(tmp_path, reads_item=True)
    # The same rule is accepted when no plugin generates the item expose.
    import yaml

    simulation = plain / "simulation.flux.yml"
    document = yaml.safe_load(simulation.read_text(encoding="utf-8"))
    from .conftest import NAMESPACE

    declared = document["extensions"][NAMESPACE].pop("generators")
    simulation.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert validate(plain).exit_code == 0, validate(plain).stderr
    document["extensions"][NAMESPACE]["generators"] = declared
    simulation.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    message = _refused(plain)
    assert "emit 'store_sales'" in message and "a row of 'item' through item" in message


def test_the_plugin_is_never_imported_by_validate(installed: Path, tmp_path: Path) -> None:
    assert validate(plugin_bundle(tmp_path)).exit_code == 0
    assert PACKAGE not in sys.modules and f"{PACKAGE}.plugin" not in sys.modules
