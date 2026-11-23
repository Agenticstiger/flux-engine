# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's ``overlay`` (ADR 0030 section 7): read, refused, merged and bound into ``H``.

An overlay is a named RFC 7396 merge patch over the profile the Simulation pins. Pinned here:

* ``merge_patch`` is RFC 7396's function, on the RFC's own test vectors;
* the block holds exactly ``name`` and ``patch``, the name matches ``[a-z][a-z0-9-]*`` and has at
  most 63 characters, and the patch touches only the numbers a profile publishes: each other
  member is refused (``FLX-3105``), and so is a restated statistic that carries ``noise``;
* the overlaid profile is read by flux-stats' reader at the pinned profile's format, and a patch
  that names a table or a column the profile does not hold, or makes a document the reader refuses,
  is refused;
* the key moves ``H`` of every emit and no ``G``, only when declared, and a bundle that declares
  none hashes as it did.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.overlay import (
    OVERLAY_KEY,
    Overlay,
    bound_into_h,
    merge_patch,
    overlaid_document,
    overlaid_profile,
    overlay_from_plan,
    read_overlay_block,
)
from flux_engine.seam.resolve import (
    FLX_3105,
    declared_overlay,
    load_bundle,
    resolve_all_emits,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE: Dict[str, Any] = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "author" / "orders.profile.json").read_text("utf-8")
)
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
NAMESPACE = "  com.agentics.flux/1:\n"
SIMULATION = "sim-store-star"
PIN = "profile:\n  digest: sha256:" + "ab" * 32 + "\n"
DOUBLE = {"name": "double", "patch": {"tables": {"orders": {"rowCount": 1200}}}}

# ---------------------------------------------------------------------------
# RFC 7396, appendix A
# ---------------------------------------------------------------------------

RFC_7396_VECTORS = [
    ({"a": "b"}, {"a": "c"}, {"a": "c"}),
    ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
    ({"a": "b"}, {"a": None}, {}),
    ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
    ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
    ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
    ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
    ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
    (["a", "b"], ["c", "d"], ["c", "d"]),
    ({"a": "b"}, ["c"], ["c"]),
    ({"a": "foo"}, None, None),
    ({"a": "foo"}, "bar", "bar"),
    ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
    ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
    ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
]


@pytest.mark.parametrize(("target", "patch", "expected"), RFC_7396_VECTORS)
def test_merge_patch_is_rfc_7396s_function(target: Any, patch: Any, expected: Any) -> None:
    before = copy.deepcopy((target, patch))
    assert merge_patch(target, patch) == expected
    assert (target, patch) == before  # neither argument is changed


def test_the_result_shares_nothing_with_the_patch() -> None:
    patch = {"a": {"b": [1, {"c": 2}]}}
    merged = merge_patch({}, patch)
    merged["a"]["b"][1]["c"] = 3
    assert patch == {"a": {"b": [1, {"c": 2}]}}


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def _read(block: Any) -> Overlay:
    return read_overlay_block(block, where="sim.overlay", code=FLX_3105)


def _refusal(block: Any) -> str:
    with pytest.raises(FluxError) as refused:
        _read(block)
    assert refused.value.code == FLX_3105
    return str(refused.value)


def test_an_overlay_is_its_name_its_patch_and_their_digest() -> None:
    found = _read(DOUBLE)
    assert (found.name, found.patch) == ("double", DOUBLE["patch"])
    assert found.digest.value.startswith("sha256:") and found.to_document() == DOUBLE
    # The digest is over {name, patch}: the same document keeps it, either member moves it.
    assert _read(copy.deepcopy(DOUBLE)).digest == found.digest
    assert _read({**DOUBLE, "name": "triple"}).digest != found.digest
    assert _read({**DOUBLE, "patch": {"tables": {"orders": {"rowCount": 1300}}}}).digest != (
        found.digest
    )
    assert bound_into_h(found) == found.digest.value and bound_into_h(None) is None


@pytest.mark.parametrize("name", ["a", "double-churn", "x1", "a-b-c", "a" * 63])
def test_a_name_in_the_pattern_is_read(name: str) -> None:
    assert _read({"name": name, "patch": {}}).name == name


@pytest.mark.parametrize(
    "name", ["", "A", "1a", "-a", "a_b", "a b", "double\n", "a" * 64, "Doublé", None, 3, ["a"]]
)
def test_a_name_outside_the_pattern_is_refused(name: Any) -> None:
    assert "name matches [a-z][a-z0-9-]* and has at most 63 characters" in _refusal(
        {"name": name, "patch": {}}
    )


@pytest.mark.parametrize(
    ("block", "problem"),
    [
        ("double", "is a mapping holding exactly 'name' and 'patch'"),
        (["double"], "is a mapping holding exactly 'name' and 'patch'"),
        ({"patch": {}}, "names its 'name'"),
        ({"name": "x"}, "names its 'patch'"),
        ({"name": "x", "patch": {}, "note": "n"}, "declares ['note']"),
        ({"name": "x", "patch": []}, "patch is a mapping"),
        ({"name": "x", "patch": None}, "patch is a mapping"),
        ({"name": "x", "patch": "tables"}, "patch is a mapping"),
    ],
)
def test_a_malformed_block_is_refused(block: Any, problem: str) -> None:
    assert problem in _refusal(block)


# ---------------------------------------------------------------------------
# What a patch may touch
# ---------------------------------------------------------------------------

ALLOWED = [
    {"tables": {"orders": {"rowCount": 1200}}},
    {"tables": {"orders": {"columns": {"amount": {"metrics": {"min": None}}}}}},
    {"tables": {"orders": {"columns": {"amount": {"statistics": [{"definition": "mean/1"}]}}}}},
    {"tables": {"orders": {"columnPairs": [], "periods": None}}},
    {"relationships": [], "invariants": None},
    {"tables": {"orders": {"rowCount": 1, "columns": {"amount": {"metrics": {}}}}}},
]


@pytest.mark.parametrize("patch", ALLOWED)
def test_a_patch_of_the_numbers_a_profile_publishes_is_read(patch: Dict[str, Any]) -> None:
    assert _read({"name": "x", "patch": patch}).patch == patch


REFUSED_MEMBERS: Tuple[Tuple[Dict[str, Any], str], ...] = (
    ({"_schemaURL": "https://example.test/profile/9-0-0"}, "touches '_schemaURL'"),
    ({"_producer": "urn:example:other"}, "touches '_producer'"),
    ({"encoding": {"fractionalBits": 8}}, "touches 'encoding'"),
    ({"disclosureRules": {"none": {}}}, "touches 'disclosureRules'"),
    ({"privacy": None}, "touches 'privacy'"),
    ({"publicStructure": {}}, "touches 'publicStructure'"),
    ({"references": {"plan": {}}}, "touches 'references'"),
    ({"queries": []}, "touches 'queries'"),
    ({"tables": None}, "patches the tables, so it is a mapping and never null"),
    ({"tables": []}, "patches the tables, so it is a mapping and never null"),
    ({"tables": {"orders": None}}, "patches a table, so it is a mapping and never null"),
    ({"tables": {"orders": {"cliques": []}}}, "touches 'cliques'"),
    ({"tables": {"orders": {"privacyUnit": "x"}}}, "touches 'privacyUnit'"),
    ({"tables": {"orders": {"columns": None}}}, "patches the columns, so it is a mapping"),
    ({"tables": {"orders": {"columns": {"amount": None}}}}, "patches a column"),
    (
        {"tables": {"orders": {"columns": {"amount": {"domain": "category"}}}}},
        "touches 'domain'",
    ),
    ({"tables": {"orders": {"columns": {"amount": {"unit": "eur"}}}}}, "touches 'unit'"),
)


@pytest.mark.parametrize(("patch", "problem"), REFUSED_MEMBERS)
def test_a_patch_that_touches_anything_else_is_refused(patch: Dict[str, Any], problem: str) -> None:
    assert problem in _refusal({"name": "x", "patch": patch})


NOISY = {"definition": "levelHistogram/1", "noise": {"mechanism": "laplace", "scale": "1"}}


@pytest.mark.parametrize(
    "patch",
    [
        {"tables": {"orders": {"columns": {"country": {"statistics": [NOISY]}}}}},
        {"tables": {"orders": {"columns": {"country": {"metrics": {"count": {"noise": {}}}}}}}},
        {"tables": {"orders": {"columnPairs": [{"columns": ["a", "b"], "statistics": [NOISY]}]}}},
        {"tables": {"orders": {"periods": [{"statistics": [{"groups": [NOISY]}]}]}}},
        {"relationships": [{"statistics": [NOISY]}]},
        {"invariants": [{"name": "t.r", "statistics": [NOISY]}]},
    ],
)
def test_a_restated_statistic_may_not_carry_noise(patch: Dict[str, Any]) -> None:
    assert "restates a statistic with a 'noise' block" in _refusal({"name": "x", "patch": patch})


@pytest.mark.parametrize("number", [1.5, 0.5])
def test_a_number_written_as_a_decimal_is_refused_as_the_profile_refuses_it(number: float) -> None:
    from decimal import Decimal

    for value in (Decimal(str(number)), number):
        assert "a profile writes it as a string" in _refusal(
            {"name": "x", "patch": {"tables": {"orders": {"rowCount": value}}}}
        )


def test_a_key_that_is_not_a_string_is_refused() -> None:
    assert "a key that is not a string" in _refusal(
        {"name": "x", "patch": {"tables": {"orders": {"rowCount": {1: 2}}}}}
    )


# ---------------------------------------------------------------------------
# The overlaid profile
# ---------------------------------------------------------------------------


def _overlay(patch: Dict[str, Any]) -> Overlay:
    return _read({"name": "x", "patch": patch})


def test_the_overlaid_document_is_the_profile_with_the_patch_merged_in() -> None:
    before = copy.deepcopy(PROFILE)
    merged = overlaid_document(PROFILE, _read(DOUBLE), code=FLX_3105)
    assert PROFILE == before  # the pinned document, whose digest the pin names, is untouched
    assert merged["tables"]["orders"]["rowCount"] == 1200
    other = copy.deepcopy(merged)
    other["tables"]["orders"]["rowCount"] = before["tables"]["orders"]["rowCount"]
    assert other == before


def test_a_new_row_count_for_a_table_with_a_history_is_refused() -> None:
    """The entities come from a history no overlay can patch, so only the chains would change."""
    profile = copy.deepcopy(PROFILE)
    profile["tables"]["orders"]["history"] = {"versionsPerEntity": {}}
    same = _overlay({"tables": {"orders": {"rowCount": profile["tables"]["orders"]["rowCount"]}}})
    assert overlaid_document(profile, same, code=FLX_3105) == profile
    with pytest.raises(FluxError) as caught:
        overlaid_document(profile, _read(DOUBLE), code=FLX_3105)
    assert caught.value.code == FLX_3105
    assert "rowCount of table 'orders'" in str(caught.value)
    assert "history.entities" in str(caught.value)


def test_the_overlaid_profile_is_read_by_the_profile_reader_at_the_pinned_format() -> None:
    profile, document = overlaid_profile(PROFILE, _read(DOUBLE), code=FLX_3105)
    assert profile.tables["orders"].row_count == 1200
    assert document["_schemaURL"] == PROFILE["_schemaURL"]


def test_a_metric_the_overlay_removes_is_gone_from_the_overlaid_profile() -> None:
    patch = {"tables": {"orders": {"columns": {"amount": {"metrics": {"quantiles": None}}}}}}
    profile, document = overlaid_profile(PROFILE, _overlay(patch), code=FLX_3105)
    assert "quantiles" not in document["tables"]["orders"]["columns"]["amount"]["metrics"]
    assert "quantiles" in PROFILE["tables"]["orders"]["columns"]["amount"]["metrics"]
    assert profile.tables["orders"].row_count == PROFILE["tables"]["orders"]["rowCount"]


@pytest.mark.parametrize(
    ("patch", "problem"),
    [
        ({"tables": {"shipments": {"rowCount": 1}}}, "table 'shipments', which the pinned profile"),
        (
            {"tables": {"orders": {"columns": {"weight": {"metrics": {}}}}}},
            "column 'weight' of table 'orders', which the pinned profile does not hold",
        ),
    ],
)
def test_a_patch_names_a_table_and_a_column_the_profile_holds(
    patch: Dict[str, Any], problem: str
) -> None:
    with pytest.raises(FluxError) as refused:
        overlaid_profile(PROFILE, _overlay(patch), code=FLX_3105)
    assert refused.value.code == FLX_3105 and problem in str(refused.value)


@pytest.mark.parametrize(
    "patch",
    [
        {"tables": {"orders": {"rowCount": "many"}}},
        {"tables": {"orders": {"rowCount": []}}},
        {"tables": {"orders": {"columns": {"amount": {"metrics": "none"}}}}},
        {"relationships": {"parent": "orders"}},
    ],
)
def test_a_patch_the_profile_reader_refuses_is_refused(patch: Dict[str, Any]) -> None:
    with pytest.raises(FluxError) as refused:
        overlaid_profile(PROFILE, _overlay(patch), code=FLX_3105)
    assert refused.value.code == FLX_3105
    assert "the overlay 'x' makes a profile that is refused" in str(refused.value)


def test_a_plan_records_the_overlay_as_the_namespace_reads_it() -> None:
    assert overlay_from_plan(None, code=FLX_3105) is None
    assert overlay_from_plan(DOUBLE, code=FLX_3105) == _read(DOUBLE)
    with pytest.raises(FluxError):
        overlay_from_plan({"name": "X", "patch": {}}, code=FLX_3105)


# ---------------------------------------------------------------------------
# In a Simulation, and in H
# ---------------------------------------------------------------------------


def _bundle(tmp_path: Path, block: str = "", name: str = "bundle") -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    if block:
        simulation = target / "simulation.flux.yml"
        text = simulation.read_text("utf-8")
        assert text.count(NAMESPACE) == 1
        lines = "".join(f"    {line}\n" for line in block.splitlines())
        simulation.write_text(text.replace(NAMESPACE, NAMESPACE + lines, 1), "utf-8")
    return target


OVERLAY_BLOCK = (
    PIN + "overlay:\n  name: double\n  patch:\n    tables:\n      item: {rowCount: 20}\n"
)


def _digests(bundle: Path) -> Dict[str, Tuple[str, str]]:
    from flux_engine.seam.emit import seam_digests

    found = {}
    for resolved in resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION):
        digests = seam_digests(
            resolved.contract_document,
            resolved.emit,
            resolved.compile().column_names,
            resolved.extension,
        )
        found[resolved.expose_id] = (digests.h.value, digests.g.value)
    return found


def test_the_overlay_moves_h_of_every_emit_and_no_g_and_only_when_declared(
    tmp_path: Path,
) -> None:
    pinned = _digests(_bundle(tmp_path, PIN, "pinned"))
    plain = _digests(_bundle(tmp_path, "", "plain"))
    declared = _digests(_bundle(tmp_path, OVERLAY_BLOCK, "declared"))
    assert pinned == {
        expose: (h, g) for expose, (h, g) in plain.items()
    }, "a profile pin alone moves neither"
    assert set(declared) == set(plain) and len(plain) > 1
    for expose, (h, g) in declared.items():
        assert h != plain[expose][0], expose
        assert g == plain[expose][1], expose
    patch = OVERLAY_BLOCK.replace("rowCount: 20", "rowCount: 40")
    again = _digests(_bundle(tmp_path, patch, "again"))
    assert all(again[expose][0] != declared[expose][0] for expose in declared)


def test_the_one_path_that_takes_g_is_blind_to_the_overlay() -> None:
    """The seam's exclusion and the compiler's, which calls it, drop the member, so plan and ship agree."""
    from flux_engine.concordance.compiler import strip_excluded as compiler_strip_excluded
    from flux_engine.seam.emit import reusable_extension, strip_excluded

    member = {OVERLAY_KEY: "sha256:" + "0" * 64}
    assert strip_excluded({**member, "other": 1}) == {"other": 1}
    assert compiler_strip_excluded({**member, "other": 1}) == {"other": 1}
    assert reusable_extension(member) is None
    assert reusable_extension(member, keep_facts=True) is None


def test_each_emit_binds_the_digest_of_name_and_patch(tmp_path: Path) -> None:
    resolved = resolve_all_emits(
        load_bundle(_bundle(tmp_path, OVERLAY_BLOCK)), simulation_id=SIMULATION
    )
    expected = _read({"name": "double", "patch": {"tables": {"item": {"rowCount": 20}}}})
    assert len(resolved) > 1
    for emit in resolved:
        assert emit.extension is not None
        assert emit.extension[OVERLAY_KEY] == expected.digest.value
    none = resolve_all_emits(load_bundle(_bundle(tmp_path, PIN, "none")), simulation_id=SIMULATION)
    assert all(OVERLAY_KEY not in (emit.extension or {}) for emit in none)


def _refused(tmp_path: Path, block: str) -> str:
    bundle = _bundle(tmp_path, block, name=f"bad-{len(list(tmp_path.iterdir()))}")
    with pytest.raises(FluxError) as refused:
        resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    assert refused.value.code == FLX_3105
    return str(refused.value)


def test_a_simulation_that_pins_no_profile_cannot_declare_an_overlay(tmp_path: Path) -> None:
    message = _refused(tmp_path, OVERLAY_BLOCK.replace(PIN, ""))
    assert "patches the pinned profile, and the Simulation pins none" in message


def test_a_malformed_overlay_in_a_simulation_is_named_by_its_place(tmp_path: Path) -> None:
    message = _refused(tmp_path, PIN + "overlay:\n  name: Double\n  patch: {}\n")
    assert "extensions['com.agentics.flux/1'].overlay.name matches [a-z][a-z0-9-]*" in message
    message = _refused(tmp_path, PIN + "overlay:\n  name: x\n  patch: {privacy: null}\n")
    assert "touches 'privacy'" in message


def test_a_null_overlay_declares_none(tmp_path: Path) -> None:
    from flux_engine.runtime import resolve_cast

    loaded = load_bundle(_bundle(tmp_path, PIN + "overlay:\n"))
    cast = resolve_cast(loaded.flux_documents, simulation_id=SIMULATION)
    assert declared_overlay(cast.simulation, where=SIMULATION) is None


def test_a_declared_overlay_is_read_as_a_typed_record(tmp_path: Path) -> None:
    from flux_engine.runtime import resolve_cast

    loaded = load_bundle(_bundle(tmp_path, OVERLAY_BLOCK))
    cast = resolve_cast(loaded.flux_documents, simulation_id=SIMULATION)
    found = declared_overlay(cast.simulation, where=SIMULATION)
    assert found is not None and found.name == "double"
    assert found.patch == {"tables": {"item": {"rowCount": 20}}}


@pytest.mark.parametrize(
    ("code", "words"),
    [
        ("FLX-3105", ["profile overlay", "no overlay can patch", "rowCount", "noise"]),
        ("FLX-3113", ["profile overlay", "source: overlay", "overlaid profile"]),
    ],
)
def test_the_error_catalogue_names_the_overlay_causes(
    capsys: pytest.CaptureFixture[str], code: str, words: list[str]
) -> None:
    from flux_engine.cli import main

    capsys.readouterr()
    assert main(["explain", code]) == 0
    text = " ".join(capsys.readouterr().out.split())
    for word in words:
        assert word in text, word
