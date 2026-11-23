# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``asOf`` key of the namespace: its reader, its checks and its binding into ``H`` (ADR 0017 section 8).

* **The reader.** An entry is ``{emit, relationship, date}``; an unknown key is ``FLX-3105`` and any other
  malformed value ``FLX-3403``, each naming the entry and the key.
* **The checks.** The relationship is a to-one relationship of the emit whose target has a history and whose
  ``toColumns`` are that history's natural entity, and the date is a DATE or TIMESTAMP column of the emit
  (``FLX-3403``).
* **The binding.** An entry is bound into the reading emit's entry for ``H`` and into nothing else, ``G`` is
  blind to it, and a bundle without the key keeps its digests.
* **The kernel.** Kernel 0.1.0 refuses the key with ``FLX-3105`` and names the kernel.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator.relations import FLX_3403
from flux_engine.seam.asof import (
    ASOF_KEY,
    AsOfEntry,
    asof_entries_of,
    bound_into_h,
    check_asof,
    read_asof_block,
)
from flux_engine.seam.emit import seam_digests
from flux_engine.seam.history import HistoryTarget, history_target
from flux_engine.seam.keys import compile_keys
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_all_emits
from flux_engine.stages import pipeline
from tests.generator.test_history_asof import ASOF, EMIT, contract

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "history-asof"
NAMESPACE = "com.agentics.flux/1"
SIMULATION = "sim-subscriptions"
ENTRY = {"emit": "invoice", "relationship": "subscription", "date": "invoice_date"}
EMITS = frozenset({"invoice", "subscription_version"})


def bundle_with(tmp_path: Path, name: str, block: Optional[Any]) -> Path:
    """A copy of the fixture whose ``asOf`` block is *block*; ``None`` removes the key."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][NAMESPACE]
    if block is None:
        flux.pop(ASOF_KEY, None)
    else:
        flux[ASOF_KEY] = block
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def resolved(bundle: Path) -> Dict[str, Any]:
    return {
        emit.expose_id: emit
        for emit in resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    }


def digests(bundle: Path) -> Dict[str, Tuple[str, str]]:
    found = {}
    for expose_id, emit in resolved(bundle).items():
        made = seam_digests(
            emit.contract_document, emit.emit, emit.compile().column_names, emit.extension
        )
        found[expose_id] = (made.h.value, made.g.value)
    return found


def refused(block: Any, code: Any = FLX_3403) -> str:
    with pytest.raises(FluxError) as caught:
        read_asof_block(block, emits=EMITS, where="asOf", code=FLX_3105, refusal=FLX_3403)
    assert caught.value.code is code, str(caught.value)
    return str(caught.value)


# --- the reader --------------------------------------------------------------------------------


def test_an_entry_reads_and_round_trips_through_the_bound_member() -> None:
    found = read_asof_block([ENTRY], emits=EMITS, where="asOf", code=FLX_3105, refusal=FLX_3403)
    assert found == (AsOfEntry("invoice", "subscription", "invoice_date"),)
    member = bound_into_h(found, "invoice")
    assert member == [{"relationship": "subscription", "date": "invoice_date"}]
    assert asof_entries_of({ASOF_KEY: member}, "invoice") == found
    assert bound_into_h(found, "subscription_version") is None
    assert asof_entries_of(None, "invoice") == ()


def test_entries_are_kept_in_relationship_order() -> None:
    found = read_asof_block(
        [
            {**ENTRY, "relationship": "b"},
            {**ENTRY, "relationship": "a"},
        ],
        emits=EMITS,
        where="asOf",
        code=FLX_3105,
        refusal=FLX_3403,
    )
    assert [entry.relationship for entry in found] == ["a", "b"]
    assert [item["relationship"] for item in bound_into_h(found, "invoice") or []] == ["a", "b"]


def test_an_unknown_key_is_flx_3105_naming_the_entry() -> None:
    message = refused([{**ENTRY, "when": "x"}], FLX_3105)
    assert "asOf[0]" in message and "when" in message


@pytest.mark.parametrize("block", ["invoice", {"emit": "invoice"}, [ENTRY, "x"]])
def test_a_block_or_an_entry_that_is_not_a_list_or_a_mapping_is_flx_3105(block: Any) -> None:
    refused(block, FLX_3105)


@pytest.mark.parametrize("key", ["emit", "relationship", "date"])
@pytest.mark.parametrize("value", [None, "", 3])
def test_a_name_that_is_not_a_non_empty_string_is_flx_3403(key: str, value: Any) -> None:
    message = refused([{**ENTRY, key: value}])
    assert f"asOf[0].{key}" in message


def test_an_emit_the_simulation_does_not_emit_is_flx_3403() -> None:
    assert "elsewhere" in refused([{**ENTRY, "emit": "elsewhere"}])


def test_a_relationship_declared_twice_is_flx_3403() -> None:
    assert "twice" in refused([ENTRY, dict(ENTRY)])


# --- the checks against the contract ----------------------------------------------------------


def targets(
    change: Optional[Any] = None, *, histories: Tuple[str, ...] = (EMIT,)
) -> Tuple[HistoryTarget, Dict[str, HistoryTarget]]:
    document = contract(rules=["plan_then"])
    if change is not None:
        change(document)
    keys = compile_keys(document)
    assert keys is not None
    found = {
        expose["exposeId"]: history_target(
            expose,
            keys.expose(expose["exposeId"]),
            fidelity=None,
            time_bounds=None,
            counts=[],
            links=[],
        )
        for expose in document["exposes"]
    }
    return found["invoice"], {name: found[name] for name in histories}


def checked(entry: AsOfEntry = ASOF, change: Optional[Any] = None, **kwargs: Any) -> str:
    reader, held = targets(change, **kwargs)
    with pytest.raises(FluxError) as caught:
        check_asof(entry, reader, held, refusal=FLX_3403)
    assert caught.value.code is FLX_3403
    return str(caught.value)


def test_a_relationship_into_a_history_with_a_date_passes() -> None:
    reader, held = targets()
    check_asof(ASOF, reader, held, refusal=FLX_3403)
    reader, held = targets(
        lambda document: document["exposes"][1]["contract"]["schema"].append(
            {"name": "i_at", "type": "TIMESTAMP", "required": False}
        )
    )
    check_asof(AsOfEntry("invoice", "sub", "i_at"), reader, held, refusal=FLX_3403)


def test_a_relationship_the_emit_does_not_declare_is_refused() -> None:
    assert "not a to-one relationship" in checked(AsOfEntry("invoice", "nope", "i_date"))


def test_a_target_that_declares_no_history_is_refused() -> None:
    assert "declares no history" in checked(histories=())


def test_a_relationship_whose_columns_are_not_the_natural_entity_is_refused() -> None:
    def other(document: Dict[str, Any]) -> None:
        document["exposes"][1]["semantics"]["relationships"][0]["toColumns"] = ["version_id"]
        document["exposes"][1]["contract"]["schema"][1]["type"] = "INT"

    assert "natural entity" in checked(change=other)


@pytest.mark.parametrize("column", ["i_id", "i_sub", "missing"])
def test_a_date_that_is_not_a_date_or_timestamp_column_is_refused(column: str) -> None:
    assert column in checked(AsOfEntry("invoice", "sub", column))


# --- H, G and the plan --------------------------------------------------------------------------


def test_an_entry_moves_h_of_its_reading_emit_and_neither_g_nor_another_emit(
    tmp_path: Path,
) -> None:
    plain = digests(bundle_with(tmp_path, "plain", None))
    moved = digests(bundle_with(tmp_path, "asof", [ENTRY]))
    assert moved["invoice"][0] != plain["invoice"][0]
    assert moved["invoice"][1] == plain["invoice"][1]
    for other in ("customer", "subscription_version", "subscription_change"):
        assert moved[other] == plain[other]


def test_the_member_is_bound_only_to_the_emit_that_has_an_entry(tmp_path: Path) -> None:
    emits = resolved(bundle_with(tmp_path, "bound", [ENTRY]))
    assert (emits["invoice"].extension or {})[ASOF_KEY] == [
        {"relationship": "subscription", "date": "invoice_date"}
    ]
    assert ASOF_KEY not in (emits["subscription_version"].extension or {})
    assert emits["invoice"].history_emits == frozenset({"subscription_version"})


def test_a_bundle_without_the_key_keeps_its_digests(tmp_path: Path) -> None:
    first = digests(bundle_with(tmp_path, "none", None))
    assert first == digests(bundle_with(tmp_path, "empty", []))


@pytest.mark.parametrize(
    ("block", "words"),
    [
        ([{**ENTRY, "relationship": "nope"}], "not a to-one relationship"),
        ([{**ENTRY, "date": "invoice_id"}], "not a DATE or TIMESTAMP"),
        ([{"emit": "customer", "relationship": "x", "date": "signed_up_on"}], "not a to-one"),
    ],
)
def test_a_refused_entry_is_flx_3403_naming_the_entry(
    tmp_path: Path, block: Any, words: str
) -> None:
    with pytest.raises(FluxError) as caught:
        resolved(bundle_with(tmp_path, "refused", block))
    assert caught.value.code is FLX_3403
    assert words in str(caught.value) and "asOf (" in str(caught.value)


def test_an_unknown_key_in_a_bundle_is_flx_3105(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        resolved(bundle_with(tmp_path, "unknown", [{**ENTRY, "when": "x"}]))
    assert caught.value.code is FLX_3105


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle_with(tmp_path, "k1", [ENTRY]), kernel=KERNEL_V1)
    assert caught.value.code is FLX_3105
    assert "0.2.0" in str(caught.value) and "0.1.0" in str(caught.value)


def test_a_declared_entry_plans_the_reader_with_the_history_it_reads(tmp_path: Path) -> None:
    result = pipeline.execute(bundle_with(tmp_path, "k2", [ENTRY]), kernel=KERNEL_V2)
    emit = next(e for e in result.emits if e.expose.expose_id == "invoice")
    assert emit.plan is not None and emit.plan.rules is not None
    assert [entry.date for entry in emit.plan.rules.asof] == ["invoice_date"]
    assert "subscription_version" in dict(emit.plan.reads)


def test_the_fixture_declares_the_entry_the_tests_start_from() -> None:
    document = yaml.safe_load((FIXTURE / "simulation.flux.yml").read_text("utf-8"))
    assert document["extensions"][NAMESPACE][ASOF_KEY] == [ENTRY]
