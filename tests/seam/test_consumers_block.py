# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``consumers`` block a Simulation declares (ADR 0028 sections 1 and 8).

Pinned here: each accepted shape, each refusal and each bound of the block; that the namespace
accepts the key; the closed list of dialects against sqlglot's; and that declaring the block moves
neither ``H`` nor ``G`` of any emit and changes no byte a run writes.
"""

from __future__ import annotations

import copy
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import (
    DIALECTS,
    Ceilings,
    check_consumer_columns,
    raised_ceilings,
    read_consumers_block,
)
from flux_engine.seam.resolve import (
    declared_consumers,
    load_bundle,
    resolve_all_emits,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "consumers"
BUNDLE = FIXTURE / "bundle"
NAMESPACE = "com.agentics.flux/1"
EMITS = frozenset({"subscriptions"})
WHERE = "consumers"


def _read(block: Any, ceilings: Any = None) -> Any:
    return read_consumers_block(block, emits=EMITS, where=WHERE, ceilings=ceilings)


def _refused(block: Any, code: str, *, mention: str = "", ceilings: Any = None) -> None:
    with pytest.raises(FluxError) as caught:
        _read(block, ceilings)
    assert caught.value.code.code == code
    assert mention in str(caught.value)


def _query(**changes: Any) -> Dict[str, Any]:
    return {"id": "q", "sql": "SELECT 1", **changes}


# ------------------------------------------------------------------ accepted shapes


def test_an_empty_block_is_a_registry_of_defaults() -> None:
    registry = _read({})
    assert registry.dialect == "duckdb"
    assert registry.as_of is None
    assert registry.queries == ()
    assert (registry.limits.seconds, registry.limits.memory) == (30, 1 << 30)
    assert registry.calibration.evaluations == 64
    assert registry.calibration.scale == Fraction(1)


def test_the_fixture_block_reads_in_full() -> None:
    simulation = yaml.safe_load((BUNDLE / "simulation.flux.yml").read_text(encoding="utf-8"))
    registry = declared_consumers(simulation, where="sim")
    assert registry is not None
    assert registry.dialect == "snowflake"
    assert registry.as_of == "2026-06-30"
    assert registry.tables == (("prod.analytics.subscriptions", "subscriptions"),)
    assert registry.prefixes == ("prod.analytics",)
    assert registry.columns == (("subscriptions", (("sub_plan", "plan"),)),)
    assert [view.name for view in registry.views] == ["offered"]
    assert [(q.id, q.held_out, q.unit) for q in registry.queries] == [
        ("premium-renewed", False, "subscriptions"),
        ("renewed-offered", False, "offered"),
        ("renewed-since", True, "subscriptions"),
    ]
    assert registry.queries[0].tolerance == Fraction(2, 100)
    assert registry.queries[1].group_by == "plan"
    assert registry.queries[2].parameters == (("since", "2025-03-01"),)


def test_the_sqlglot_version_that_compiled_the_requirements_is_recorded() -> None:
    assert _read({}).compiled_sqlglot is None
    assert _read({"compiledWith": {"sqlglot": "30.33.0"}}).compiled_sqlglot == "30.33.0"


def test_a_yaml_null_declares_none() -> None:
    assert declared_consumers({"extensions": {NAMESPACE: {"consumers": None}}}) is None
    assert declared_consumers({"extensions": {NAMESPACE: {}}}) is None


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_listed_dialect_is_accepted(dialect: str) -> None:
    assert _read({"dialect": dialect}).dialect == dialect


def test_limits_calibration_and_parameters_are_read_exactly() -> None:
    registry = _read(
        {
            "limits": {"seconds": 600, "memory": "16GiB"},
            "queries": [_query(parameters={"a": "x", "b_2": 7, "c": True}, tolerance="0")],
            "calibration": {
                "parameters": [
                    {
                        "document": "s.yml",
                        "pointer": "/a/b~1c",
                        "min": "-0.5",
                        "max": 1,
                        "step": "0.01",
                    }
                ],
                "evaluations": 4096,
                "scale": "0.25",
                "heldOutTolerance": "1",
                "seed": 7,
            },
        },
        raised_ceilings(600, 16 << 30, 4096),
    )
    assert (registry.limits.seconds, registry.limits.memory) == (600, 16 << 30)
    assert registry.queries[0].parameters == (("a", "x"), ("b_2", 7), ("c", True))
    assert registry.queries[0].tolerance == Fraction(0)
    calibration = registry.calibration
    assert calibration.evaluations == 4096 and calibration.scale == Fraction(1, 4)
    assert calibration.held_out_tolerance == Fraction(1) and calibration.seed == 7
    assert calibration.parameters[0].minimum == Fraction(-1, 2)
    assert registry.limits.memory == 16 << 30
    assert _read({"limits": {"memory": "512MiB"}}).limits.memory == 512 << 20


def test_the_id_grammar_has_its_edges() -> None:
    assert _read({"queries": [_query(id="a" + "z" * 127)]}).queries[0].id.startswith("a")
    assert _read({"queries": [_query(id="a.b-c_9")]}).queries[0].id == "a.b-c_9"


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    ("block", "code"),
    [
        ([], "FLX-3601"),
        ({"surprise": 1}, "FLX-3601"),
        ({"dialect": "klingon"}, "FLX-3602"),
        ({"dialect": 3}, "FLX-3602"),
        ({"asOf": "2026-6-30"}, "FLX-3603"),
        ({"asOf": "2026-02-30"}, "FLX-3603"),
        ({"asOf": 20260630}, "FLX-3603"),
        ({"tables": {"t": "nothing"}}, "FLX-3604"),
        ({"tables": []}, "FLX-3604"),
        ({"prefixes": "a"}, "FLX-3604"),
        ({"columns": {"nothing": {"a": "b"}}}, "FLX-3604"),
        ({"columns": {"subscriptions": ["a"]}}, "FLX-3604"),
        ({"queries": [_query(id="Upper")]}, "FLX-3605"),
        ({"queries": [_query(id="1a")]}, "FLX-3605"),
        ({"queries": [_query(id="a" * 129)]}, "FLX-3605"),
        ({"queries": [_query(), _query()]}, "FLX-3605"),
        ({"queries": [_query(extra=1)]}, "FLX-3605"),
        ({"queries": "q"}, "FLX-3605"),
        (
            {"views": [{"name": "v", "sql": "SELECT 1"}, {"name": "v", "sql": "SELECT 2"}]},
            "FLX-3606",
        ),
        ({"views": [{"name": "subscriptions", "sql": "SELECT 1"}]}, "FLX-3606"),
        ({"views": [{"name": "V", "sql": "SELECT 1"}]}, "FLX-3606"),
        ({"views": [{"name": "v"}]}, "FLX-3607"),
        ({"queries": [_query(sql="")]}, "FLX-3607"),
        ({"queries": [_query(sql="SELECT 1\x00")]}, "FLX-3607"),
        ({"queries": [_query(sql="é" * 32_769)]}, "FLX-3607"),
        ({"queries": [_query(unit="nowhere")]}, "FLX-3608"),
        ({"queries": [_query(groupBy="plan")]}, "FLX-3608"),
        ({"queries": [_query(tolerance=0.5)]}, "FLX-3609"),
        ({"queries": [_query(tolerance="1.5")]}, "FLX-3609"),
        ({"queries": [_query(tolerance="-0.1")]}, "FLX-3609"),
        ({"queries": [_query(heldOut="yes")]}, "FLX-3609"),
        ({"queries": [_query(parameters={"1a": "x"})]}, "FLX-3610"),
        ({"queries": [_query(parameters={"a": 1.5})]}, "FLX-3610"),
        ({"queries": [_query(parameters={"a": None})]}, "FLX-3610"),
        ({"queries": [_query(parameters=["a"])]}, "FLX-3610"),
        ({"limits": {"seconds": 0}}, "FLX-3611"),
        ({"limits": {"seconds": 31}}, "FLX-3614"),
        ({"limits": {"seconds": True}}, "FLX-3611"),
        ({"limits": {"memory": "2GiB"}}, "FLX-3615"),
        ({"limits": {"memory": "1025MiB"}}, "FLX-3615"),
        ({"limits": {"memory": "1GB"}}, "FLX-3611"),
        ({"limits": {"memory": 1024}}, "FLX-3611"),
        ({"limits": {"other": 1}}, "FLX-3611"),
        ({"calibration": {"evaluations": 0}}, "FLX-3612"),
        ({"calibration": {"evaluations": 65}}, "FLX-3616"),
        ({"calibration": {"scale": "0"}}, "FLX-3612"),
        ({"calibration": {"scale": "1.5"}}, "FLX-3612"),
        ({"calibration": {"seed": "7"}}, "FLX-3612"),
        ({"calibration": {"seed": 1.5}}, "FLX-3612"),
        ({"calibration": {"heldOutTolerance": "2"}}, "FLX-3609"),
        ({"calibration": {"other": 1}}, "FLX-3612"),
        ({"compiledWith": "30.33.0"}, "FLX-3601"),
        ({"compiledWith": {}}, "FLX-3601"),
        ({"compiledWith": {"sqlglot": 30}}, "FLX-3601"),
        ({"compiledWith": {"sqlglot": ""}}, "FLX-3601"),
        ({"compiledWith": {"sqlglot": "30 33"}}, "FLX-3601"),
        ({"compiledWith": {"sqlglot": "30.33.0", "duckdb": "2.0.1"}}, "FLX-3601"),
    ],
)
def test_each_refusal_carries_its_code(block: Any, code: str) -> None:
    _refused(block, code)


def test_a_bundle_may_only_lower_the_ceilings() -> None:
    at_the_ceiling = {
        "limits": {"seconds": 30, "memory": "1GiB"},
        "calibration": {"evaluations": 64},
    }
    registry = _read(at_the_ceiling)
    assert (registry.limits.seconds, registry.limits.memory) == (30, 1 << 30)
    lowered = _read(
        {"limits": {"seconds": 1, "memory": "64MiB"}, "calibration": {"evaluations": 1}}
    )
    assert lowered.limits.seconds == 1 and lowered.calibration.evaluations == 1


def test_raised_ceilings_lift_what_a_bundle_may_ask_for() -> None:
    raised = raised_ceilings(seconds=600, memory=16 << 30, evaluations=4096)
    block = {
        "limits": {"seconds": 600, "memory": "16GiB"},
        "calibration": {"evaluations": 4096},
    }
    registry = _read(block, raised)
    assert registry.limits.seconds == 600 and registry.calibration.evaluations == 4096
    _refused(block, "FLX-3614")  # not under the engine's own ceilings
    assert raised_ceilings() == Ceilings(30, 1 << 30, 64)
    _refused({"limits": {"seconds": 301}}, "FLX-3614", ceilings=raised_ceilings(seconds=300))
    _refused({"limits": {"memory": "3GiB"}}, "FLX-3615", ceilings=raised_ceilings(memory=2 << 30))
    _refused(
        {"calibration": {"evaluations": 129}}, "FLX-3616", ceilings=raised_ceilings(evaluations=128)
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seconds": 29},
        {"seconds": 601},
        {"memory": (1 << 30) - 1},
        {"memory": (16 << 30) + 1},
        {"evaluations": 63},
        {"evaluations": 4097},
        {"seconds": True},
    ],
)
def test_a_raised_ceiling_stays_between_the_engines_and_the_most(kwargs: Dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        raised_ceilings(**kwargs)


def _parameter(**changes: Any) -> Dict[str, Any]:
    return {"document": "s.yml", "pointer": "/a", "min": "0", "max": "1", "step": "0.1", **changes}


@pytest.mark.parametrize(
    "parameter",
    [
        _parameter(pointer="a"),
        _parameter(pointer="/a~2"),
        _parameter(min="1"),
        _parameter(min="2"),
        _parameter(step="0"),
        _parameter(step="-1"),
        _parameter(min=0.5),
        _parameter(document=""),
        _parameter(extra=1),
        {"document": "s.yml", "pointer": "/a", "min": "0", "max": "1"},
    ],
)
def test_a_malformed_calibration_parameter_is_refused(parameter: Dict[str, Any]) -> None:
    _refused({"calibration": {"parameters": [parameter]}}, "FLX-3612")


def test_the_bounds_of_counts_are_refused_with_their_code() -> None:
    names = [f"a{index}" for index in range(65)]
    _refused({"prefixes": names}, "FLX-3613", mention="64")
    _refused({"queries": [_query(id=f"q{index}") for index in range(1025)]}, "FLX-3613")
    _refused({"views": [{"name": f"v{i}", "sql": "SELECT 1"} for i in range(257)]}, "FLX-3613")
    _refused({"queries": [_query(parameters={f"p{i}": "x" for i in range(257)})]}, "FLX-3613")
    over = [_parameter(pointer=f"/a{i}") for i in range(65)]
    _refused({"calibration": {"parameters": over}}, "FLX-3613")
    assert len(_read({"prefixes": names[:64]}).prefixes) == 64


def test_a_refusal_names_the_query_and_the_key() -> None:
    _refused({"queries": [_query(id="late", unit="nowhere")]}, "FLX-3608", mention="late")
    _refused({"queries": [_query(id="late", unit="nowhere")]}, "FLX-3608", mention=".unit")
    _refused({"queries": [_query(id="x", tolerance="2")]}, "FLX-3609", mention="tolerance")


def test_sql_at_the_byte_limit_is_accepted() -> None:
    assert _read({"queries": [_query(sql="é" * 32_768)]}).queries[0].sql


def test_a_column_target_must_be_a_contract_column() -> None:
    loaded = load_bundle(BUNDLE)
    resolved = resolve_all_emits(loaded, simulation_id="sim-telco.silver.subscriptions")[0]
    good = _read({"columns": {"subscriptions": {"sub_plan": "plan"}}})
    check_consumer_columns(good, expose_id="subscriptions", expose=resolved.expose, where="t")
    bad = _read({"columns": {"subscriptions": {"sub_plan": "tier"}}})
    with pytest.raises(FluxError) as caught:
        check_consumer_columns(bad, expose_id="subscriptions", expose=resolved.expose, where="t")
    assert caught.value.code.code == "FLX-3604" and "tier" in str(caught.value)


def test_every_listed_dialect_is_a_sqlglot_dialect() -> None:
    dialects = pytest.importorskip("sqlglot.dialects")
    known = {member.value for member in dialects.Dialects}
    assert set(DIALECTS) <= known


# ------------------------------------------------------------------ in a bundle


def test_a_calibration_document_names_a_document_of_the_bundle(tmp_path: Path) -> None:
    def parameter(document: str) -> Dict[str, Any]:
        return {"document": document, "pointer": "/a", "min": "0", "max": "1", "step": "0.1"}

    def resolve(name: str, document: str) -> None:
        bundle = _with_block(tmp_path, name, {"calibration": {"parameters": [parameter(document)]}})
        resolve_all_emits(load_bundle(bundle), simulation_id="sim-telco.silver.subscriptions")

    resolve("simulation", "simulation.flux.yml")
    resolve("contract", "subscriptions.fluid.yaml")
    for index, document in enumerate(
        ["../simulation.flux.yml", "sub/simulation.flux.yml", "notes.txt", "other.flux.yml"]
    ):
        with pytest.raises(FluxError) as caught:
            resolve(f"bad{index}", document)
        assert caught.value.code.code == "FLX-3617"


@pytest.mark.integration
def test_kernel_0_1_0_refuses_the_block_and_names_the_kernel(tmp_path: Path) -> None:
    from flux_engine.stages.plan import plan

    profile = FIXTURE / "subscriptions.profile.json"
    refused = plan(
        _with_block(tmp_path, "old"), state_dir=tmp_path / "s1", profile=profile, kernel="0.1.0"
    )
    assert not refused.success
    assert "FLX-3105" in refused.stderr and "kernel 0.1.0" in refused.stderr
    plain = plan(
        _with_block(tmp_path, "old-plain", None),
        state_dir=tmp_path / "s2",
        profile=profile,
        kernel="0.1.0",
    )
    assert plain.success, plain.stderr
    assert plan(_with_block(tmp_path, "new"), state_dir=tmp_path / "s3", profile=profile).success


def _with_block(tmp_path: Path, name: str, block: Any = ...) -> Path:
    bundle = tmp_path / name
    shutil.copytree(BUNDLE, bundle)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if block is None:
        del document["extensions"][NAMESPACE]["consumers"]
    elif block is not ...:
        document["extensions"][NAMESPACE]["consumers"] = block
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


def _digests(bundle: Path) -> Dict[str, Any]:
    from flux_engine.seam.emit import seam_digests

    found = {}
    loaded = load_bundle(bundle)
    for resolved in resolve_all_emits(loaded, simulation_id="sim-telco.silver.subscriptions"):
        digests = seam_digests(
            resolved.contract_document,
            resolved.emit,
            resolved.compile().column_names,
            resolved.extension,
        )
        found[resolved.expose_id] = (digests.h.value, digests.g.value)
    return found


def test_declaring_the_block_moves_neither_h_nor_g(tmp_path: Path) -> None:
    with_block = _digests(_with_block(tmp_path, "with"))
    without = _digests(_with_block(tmp_path, "without", None))
    assert with_block == without and set(with_block) == {"subscriptions"}


def test_the_namespace_accepts_the_key_and_a_bad_block_is_refused_when_the_bundle_resolves(
    tmp_path: Path,
) -> None:
    resolve_all_emits(
        load_bundle(_with_block(tmp_path, "ok")), simulation_id="sim-telco.silver.subscriptions"
    )
    bad = _with_block(tmp_path, "bad", {"queries": [_query(unit="nowhere")]})
    with pytest.raises(FluxError) as caught:
        resolve_all_emits(load_bundle(bad), simulation_id="sim-telco.silver.subscriptions")
    assert caught.value.code.code == "FLX-3608"
    column = copy.deepcopy({"columns": {"subscriptions": {"a": "not_a_column"}}})
    worse = _with_block(tmp_path, "worse", column)
    with pytest.raises(FluxError) as caught:
        resolve_all_emits(load_bundle(worse), simulation_id="sim-telco.silver.subscriptions")
    assert caught.value.code.code == "FLX-3604"


@pytest.mark.integration
def test_plan_then_apply_writes_the_same_bytes_with_and_without_the_block(tmp_path: Path) -> None:
    from flux_engine.stages.apply import apply
    from flux_engine.stages.layout import RunLayout
    from flux_engine.stages.plan import plan

    profile = FIXTURE / "subscriptions.profile.json"
    written = {}
    for name, block in (("with", ...), ("without", None)):
        bundle = _with_block(tmp_path, name, block)
        state = tmp_path / f"{name}-state"
        planned = plan(bundle, state_dir=state, profile=profile)
        assert planned.success, planned.stderr
        run_id = planned.artifacts["run_id"]
        applied = apply(bundle, run_id=run_id, state_dir=state, auto_plan=False)
        assert applied.success, applied.stderr
        written[name] = (
            RunLayout.for_state_dir(state, run_id).emit_path("subscriptions").read_bytes()
        )
    assert written["with"] and written["with"] == written["without"]
