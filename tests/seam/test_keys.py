# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Keys from the contract: primary and foreign keys come from the FLUID contract.

A FLUID 0.7.7 contract declares relationships (composite keys are one
relationship) and primary, unique and natural entities. The engine reads them
(:mod:`flux_engine.seam.keys`), refuses a key that does not resolve
(``FLX-3005``), refuses a Simulation that restates a key (``FLX-3111``),
requires an emit of a keyed contract to pin the contract digest
(``FLX-3112``), and derives a one-column key as an identifier without the
fidelity block saying ``unique``. A contract before 0.7.7 behaves as it did.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

import flux_spec.validate as spec_validate
import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.generator.families import ColumnType, classify_column, is_identifier
from flux_engine.generator.statistics import ColumnStatistics
from flux_engine.seam.contract import CompiledColumn, parse_logical_type
from flux_engine.seam.keys import (
    ExposeKeys,
    check_no_key_blocks,
    check_unique_declarations,
    compile_keys,
)
from flux_engine.seam.resolve import load_bundle, resolve_emit
from flux_engine.spec.validate import canonical_digest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"


def _vendors_fluid_0_7_7() -> bool:
    find = getattr(spec_validate, "fluid_schema_file", None)
    return find is not None and find("0.7.7") is not None


#: A bundle whose contract declares FLUID 0.7.7 validates only against a
#: flux-spec that vendors that preview (FLUX 0.5.2, Agenticstiger/flux#23). CI
#: installs flux_spec from FLUX main, which carries it once that PR merges; the
#: release that raises this engine's floor to flux-spec 0.5.2 drops the mark.
needs_fluid_0_7_7 = pytest.mark.skipif(
    not _vendors_fluid_0_7_7(),
    reason="the installed flux-spec vendors no FLUID 0.7.7 preview (FLUX 0.5.2 brings it)",
)
CONTRACT = "payment-recovery.fluid.yml"
SIMULATION = "simulation.flux.yml"
SIMULATION_ID = "sim-payment-recovery"
EMITTED = "payment_recovery_moment"


def _expose(
    expose_id: str, columns: list, semantics: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "exposeId": expose_id,
        "kind": "table",
        "contract": {"schema": [{"name": name, "type": "STRING"} for name in columns]},
    }
    if semantics is not None:
        entry["semantics"] = dict(semantics)
    return entry


def _contract(*exposes: Dict[str, Any], version: str = "0.7.7") -> Dict[str, Any]:
    return {"fluidVersion": version, "exposes": list(exposes)}


COMPOSITE = {
    "name": "order_to_customer",
    "to": "customer",
    "fromColumns": ["customer_id", "market"],
    "toColumns": ["customer_id", "market"],
    "cardinality": "many_to_one",
}


def _orders_and_customers(
    relationship: Optional[Mapping[str, Any]] = None, **customer_semantics: Any
):
    return _contract(
        _expose(
            "orders",
            ["order_id", "customer_id", "market"],
            {
                "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
                "relationships": [dict(relationship or COMPOSITE)],
            },
        ),
        _expose(
            "customer",
            ["customer_id", "market", "valid_from", "valid_to"],
            customer_semantics or None,
        ),
    )


# ---- compile_keys -------------------------------------------------------------------


def test_a_contract_before_0_7_7_has_no_keys_the_engine_reads() -> None:
    contract = _orders_and_customers()
    contract["fluidVersion"] = "0.7.6"
    assert compile_keys(contract) is None
    assert compile_keys({"exposes": []}) is None


def test_a_composite_key_is_one_relationship_and_a_key_of_its_target() -> None:
    keys = compile_keys(_orders_and_customers())
    assert keys is not None and keys.declares_keys
    orders, customer = keys.expose("orders"), keys.expose("customer")
    (relationship,) = orders.relationships
    assert (relationship.source, relationship.target) == ("orders", "customer")
    assert relationship.from_columns == ("customer_id", "market")
    assert relationship.to_columns == ("customer_id", "market")
    assert (relationship.cardinality, relationship.join_type) == ("many_to_one", "left")
    assert customer.keys == (("customer_id", "market"),)
    assert customer.one_column_keys == frozenset()
    assert orders.keys == (("order_id",),)
    assert orders.one_column_keys == frozenset({"order_id"})


def test_a_one_to_one_relationship_keys_its_own_columns_too() -> None:
    relationship = {
        **COMPOSITE,
        "fromColumns": ["customer_id"],
        "toColumns": ["customer_id"],
        "cardinality": "one_to_one",
    }
    keys = compile_keys(_orders_and_customers(relationship))
    assert keys is not None
    assert ("customer_id",) in keys.expose("orders").keys
    assert keys.expose("customer").one_column_keys == frozenset({"customer_id"})


def test_a_versioned_target_keeps_its_key_per_point_in_time() -> None:
    window = {
        "entities": [{"name": "customer", "type": "natural", "expr": "customer_id"}],
        "dimensions": [
            {
                "name": "valid_from",
                "type": "time",
                "typeParams": {"validityParams": {"isStart": True}},
            },
            {"name": "valid_to", "type": "time", "typeParams": {"validityParams": {"isEnd": True}}},
        ],
    }
    relationship = {**COMPOSITE, "fromColumns": ["customer_id"], "toColumns": ["customer_id"]}
    customer = compile_keys(_orders_and_customers(relationship, **window)).expose("customer")
    assert customer.versioned
    assert customer.keys == ()
    assert customer.instant_keys == (("customer_id",),)
    assert customer.repeated_keys == frozenset({"customer_id"})


@pytest.mark.parametrize(
    "change, fragment",
    [
        ({"to": "nowhere"}, "names no expose of this contract"),
        ({"toColumns": ["customer_id"]}, "pairs 2 fromColumns with 1 toColumns"),
        ({"fromColumns": ["customer_id", "customer_id"]}, "names a column twice"),
        ({"fromColumns": ["customer_id", "region"]}, "which expose 'orders' does not declare"),
        ({"toColumns": ["customer_id", "region"]}, "which expose 'customer' does not declare"),
        ({"joinType": "outer"}, "joinType"),
    ],
)
def test_a_relationship_that_does_not_resolve_is_refused(
    change: Dict[str, Any], fragment: str
) -> None:
    with pytest.raises(FluxError) as caught:
        compile_keys(_orders_and_customers({**COMPOSITE, **change}))
    assert caught.value.code.code == "FLX-3005"
    assert fragment in str(caught.value)


def test_a_relationship_name_is_unique_within_its_expose() -> None:
    contract = _orders_and_customers()
    contract["exposes"][0]["semantics"]["relationships"].append(dict(COMPOSITE))
    with pytest.raises(FluxError, match="declared twice") as caught:
        compile_keys(contract)
    assert caught.value.code.code == "FLX-3005"


@pytest.mark.parametrize(
    "entity",
    [
        {"name": "order", "type": "primary", "expr": "concat(order_id, market)"},
        {"name": "order", "type": "unique"},
        {"name": "order", "type": "natural", "expr": "missing"},
    ],
)
def test_a_key_entity_names_a_column(entity: Dict[str, Any]) -> None:
    contract = _orders_and_customers()
    contract["exposes"][0]["semantics"]["entities"] = [entity]
    with pytest.raises(FluxError, match="is not a column of the expose") as caught:
        compile_keys(contract)
    assert caught.value.code.code == "FLX-3005"


def test_a_foreign_entity_is_a_join_name_not_a_key() -> None:
    contract = _orders_and_customers()
    contract["exposes"][0]["semantics"]["entities"].append(
        {"name": "customer", "type": "foreign", "expr": "anything"}
    )
    keys = compile_keys(contract)
    assert keys is not None
    assert [entity.kind for entity in keys.expose("orders").entities] == ["primary"]


def test_a_key_of_another_type_is_drawn_as_its_type_not_refused() -> None:
    integer_key = CompiledColumn(
        name="order_id",
        logical_type=parse_logical_type("INTEGER"),
        required=True,
        declared_type="INTEGER",
        key=True,
    )
    statistics = ColumnStatistics(column="order_id")
    assert not is_identifier(integer_key, statistics)
    assert classify_column(integer_key, statistics) is ColumnType.INT


# ---- restatement checks --------------------------------------------------------------


def test_an_extensions_block_never_carries_keys() -> None:
    check_no_key_blocks(["rows", "mapping"], where="block")
    with pytest.raises(FluxError, match="FLUX does not restate them") as caught:
        check_no_key_blocks(["rows", "primaryKey"], where="block")
    assert caught.value.code.code == "FLX-3111"


def test_unique_may_agree_with_the_contract_but_never_contradict_it() -> None:
    keys = ExposeKeys(expose_id="orders", keys=(("order_id",),))
    check_unique_declarations(keys, {"columns": {"order_id": {"unique": True}}}, where="emit")
    check_unique_declarations(keys, {"columns": {"market": {"unique": False}}}, where="emit")
    with pytest.raises(FluxError, match="makes 'order_id' a key") as caught:
        check_unique_declarations(keys, {"columns": {"order_id": {"unique": False}}}, where="emit")
    assert caught.value.code.code == "FLX-3111"


def test_unique_cannot_hold_a_key_the_validity_window_repeats() -> None:
    keys = ExposeKeys(expose_id="customer", instant_keys=(("customer_id",),), versioned=True)
    for value in (True, 1, "true", [1]):
        with pytest.raises(FluxError, match="repeats 'customer_id' across versions") as caught:
            check_unique_declarations(
                keys, {"columns": {"customer_id": {"unique": value}}}, where="emit"
            )
        assert caught.value.code.code == "FLX-3111"


@pytest.mark.parametrize("value", [False, 0, None, ""])
def test_any_declaration_the_generator_reads_as_not_unique_contradicts_a_key(value: Any) -> None:
    keys = ExposeKeys(expose_id="orders", keys=(("order_id",),))
    with pytest.raises(FluxError) as caught:
        check_unique_declarations(keys, {"columns": {"order_id": {"unique": value}}}, where="emit")
    assert caught.value.code.code == "FLX-3111"


def test_a_versioned_one_to_one_source_keys_its_columns_per_point_in_time() -> None:
    window = [
        {"name": "valid_from", "type": "time", "typeParams": {"validityParams": {"isStart": True}}},
        {"name": "valid_to", "type": "time", "typeParams": {"validityParams": {"isEnd": True}}},
    ]
    customer_semantics = {
        "entities": [{"name": "customer", "type": "natural", "expr": "customer_id"}],
        "dimensions": window,
        "relationships": [
            {
                "name": "customer_profile",
                "to": "profile",
                "fromColumns": ["customer_id"],
                "toColumns": ["profile_id"],
                "cardinality": "one_to_one",
            }
        ],
    }
    contract = _contract(
        _expose("customer", ["customer_id", "valid_from", "valid_to"], customer_semantics),
        _expose("profile", ["profile_id"]),
    )
    keys = compile_keys(contract)
    assert keys is not None
    customer = keys.expose("customer")
    assert customer.keys == ()
    assert customer.instant_keys == (("customer_id",),)
    assert customer.one_column_keys == frozenset()
    assert customer.repeated_keys == frozenset({"customer_id"})


# ---- through the seam ------------------------------------------------------------------


def _keyed_bundle(
    tmp_path: Path,
    *,
    pin: bool = True,
    simulation: Optional[Callable[[Dict[str, Any]], None]] = None,
    contract_version: str = "0.7.7",
) -> Path:
    """The small telco bundle, its contract on *contract_version* with keys.

    ``payment_recovery_moment`` gets a primary entity on ``subscriber_id``,
    and a new ``subscriber_market`` expose relates to it on the composite
    key ``(subscriber_id, market)``.
    """
    bundle = tmp_path / "bundle"
    shutil.copytree(FIXTURE, bundle)
    (bundle / "flux-manifest.json").unlink()
    contract = yaml.safe_load((bundle / CONTRACT).read_text(encoding="utf-8"))
    contract["fluidVersion"] = contract_version
    moment = contract["exposes"][0]
    moment["contract"]["schema"][0].pop("tags")
    moment["semantics"] = {
        "entities": [{"name": "subscriber", "type": "primary", "expr": "subscriber_id"}]
    }
    market = copy.deepcopy(moment)
    market.update(
        exposeId="subscriber_market",
        semantics={
            "relationships": [
                {
                    "name": "market_to_moment",
                    "to": EMITTED,
                    "fromColumns": ["subscriber_id", "market"],
                    "toColumns": ["subscriber_id", "market"],
                    "cardinality": "many_to_one",
                }
            ]
        },
    )
    market["binding"]["location"]["topic"] = "billing.subscriber-market.v1"
    contract["exposes"].append(market)
    (bundle / CONTRACT).write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")

    document = yaml.safe_load((bundle / SIMULATION).read_text(encoding="utf-8"))
    emit = document["spec"]["emits"][0]
    emit["fluidVersion"] = contract_version
    if pin:
        emit["provenance"]["contractDigest"] = "sha256:" + canonical_digest(contract)
    else:
        emit.pop("provenance")  # it carries only the digest, and an empty block is invalid
    if simulation is not None:
        simulation(document)
    (bundle / SIMULATION).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


def _block(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["extensions"]["com.agentics.flux/1"]["emits"][EMITTED]


@needs_fluid_0_7_7
def test_a_keyed_contract_resolves_and_its_key_is_an_identifier(tmp_path: Path) -> None:
    resolved = resolve_emit(load_bundle(_keyed_bundle(tmp_path)), simulation_id=SIMULATION_ID)
    assert resolved.keys is not None
    assert resolved.keys.keys == (("subscriber_id",), ("subscriber_id", "market"))
    columns = {column.name: column for column in resolved.compile().columns}
    assert columns["subscriber_id"].key and not columns["market"].key
    # The contract alone makes it an identifier: no tag, no fidelity `unique`.
    assert columns["subscriber_id"].tags == ()
    assert is_identifier(columns["subscriber_id"], ColumnStatistics(column="subscriber_id"))
    assert not is_identifier(columns["market"], ColumnStatistics(column="market"))


@needs_fluid_0_7_7
def test_an_emit_of_a_keyed_contract_pins_its_digest(tmp_path: Path) -> None:
    with pytest.raises(FluxError, match="pin provenance.contractDigest: 'sha256:") as caught:
        resolve_emit(load_bundle(_keyed_bundle(tmp_path, pin=False)), simulation_id=SIMULATION_ID)
    assert caught.value.code.code == "FLX-3112"


@needs_fluid_0_7_7
def test_an_unpinned_keyed_contract_with_a_float_says_no_one_pin_fits_both(tmp_path: Path) -> None:
    # The reference validator digests a YAML float as RFC 8785 does, this engine
    # as an exact decimal (KERNEL-SPEC section 2.1): no one pin satisfies both.
    bundle = _keyed_bundle(tmp_path, pin=False)
    contract = yaml.safe_load((bundle / CONTRACT).read_text(encoding="utf-8"))
    contract["exposes"][0]["qos"] = {"completenessTarget": 0.95}
    (bundle / CONTRACT).write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    with pytest.raises(FluxError, match="a YAML float, or text not in Unicode NFC") as caught:
        resolve_emit(load_bundle(bundle), simulation_id=SIMULATION_ID)
    assert caught.value.code.code == "FLX-3112"


@needs_fluid_0_7_7
def test_a_moved_key_stops_the_seam_until_the_new_digest_is_pinned(tmp_path: Path) -> None:
    bundle = _keyed_bundle(tmp_path)
    contract = yaml.safe_load((bundle / CONTRACT).read_text(encoding="utf-8"))
    relationship = contract["exposes"][1]["semantics"]["relationships"][0]
    relationship["fromColumns"].reverse()
    relationship["toColumns"].reverse()
    (bundle / CONTRACT).write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    with pytest.raises(FluxError) as caught:
        resolve_emit(load_bundle(bundle), simulation_id=SIMULATION_ID)
    # The reference validator sees the stale pin first (FLX-3105 wraps its
    # report); the engine's own check is FLX-3101. Either way the seam stops.
    assert caught.value.code.code in {"FLX-3101", "FLX-3105"}
    assert "contractDigest" in str(caught.value)


@needs_fluid_0_7_7
def test_an_extensions_block_that_restates_keys_is_refused(tmp_path: Path) -> None:
    def restate(document: Dict[str, Any]) -> None:
        _block(document)["primaryKey"] = ["subscriber_id"]

    with pytest.raises(FluxError, match="FLUX does not restate them") as caught:
        resolve_emit(
            load_bundle(_keyed_bundle(tmp_path, simulation=restate)), simulation_id=SIMULATION_ID
        )
    assert caught.value.code.code == "FLX-3111"


@needs_fluid_0_7_7
def test_a_fidelity_unique_that_contradicts_a_key_is_refused(tmp_path: Path) -> None:
    def contradict(document: Dict[str, Any]) -> None:
        _block(document)["fidelity"]["columns"]["subscriber_id"] = {"unique": False}

    with pytest.raises(FluxError, match="makes 'subscriber_id' a key") as caught:
        resolve_emit(
            load_bundle(_keyed_bundle(tmp_path, simulation=contradict)), simulation_id=SIMULATION_ID
        )
    assert caught.value.code.code == "FLX-3111"


def test_a_contract_before_0_7_7_resolves_as_before(tmp_path: Path) -> None:
    # 0.7.6 has no relationships, so the second expose's block goes; its
    # entities stay and are not read: no pin required, no key marked.
    def drop_pin(document: Dict[str, Any]) -> None:
        document["spec"]["emits"][0].pop("provenance", None)

    bundle = _keyed_bundle(tmp_path, contract_version="0.7.6", simulation=drop_pin)
    contract = yaml.safe_load((bundle / CONTRACT).read_text(encoding="utf-8"))
    contract["exposes"].pop()
    (bundle / CONTRACT).write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    resolved = resolve_emit(load_bundle(bundle), simulation_id=SIMULATION_ID)
    assert resolved.keys is None
    assert not any(column.key for column in resolved.compile().columns)
