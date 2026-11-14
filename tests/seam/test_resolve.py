# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §8's resolution half, and the two loaders behind it.

Three things are checked here that nothing else can check.

**The engine and the reference validator agree on the shipped example.**
``provenance.contractDigest`` in the telco bundle was computed by the FLUX
reference validator; :func:`resolve_emit` recomputes it with the engine's
own canonicaliser. The test asserts both, so the day the two diverge is
the day this fails rather than the day a run silently binds to the wrong
bytes.

**No float reaches a digest.** The shipped emit declares
``minCredibility: 0.7`` -- a YAML float inside the very entry ``H`` is
taken over. :class:`~flux_engine.seam.resolve.ExactLoader` reads it as
``Decimal("0.7")`` from the source text, and a test walks the whole loaded
bundle asserting it contains no ``float`` anywhere.

**The seam declares nothing outside the published schema.** The fidelity
block, the declared row count and the column mapping live in RFC-02's
document-level ``extensions["com.agentics.flux/1"]`` namespace, so a
bundle that declares them validates unmodified: it resolves with
``tolerate_additive=False``, those same declarations back in
``spec.emits[]`` are refused, and nothing is stripped before the schema
sees a document.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest
import yaml

from flux_engine.core.digest import DigestDomain
from flux_engine.core.errors import FluxError
from flux_engine.seam import resolve as resolve_module
from flux_engine.seam.resolve import (
    ADDITIVE_SEAM_FIELDS,
    ExactLoader,
    LoadedBundle,
    ResolvedEmit,
    SeamBundle,
    flux_schema_validator,
    load_bundle,
    load_documents_exact,
    resolve_all_emits,
    resolve_emit,
    strip_additive_seam_fields,
)
from flux_engine.spec.validate import canonical_digest

from .conftest import (
    TELCO_SIMULATION_ID,
    minimal_contract,
    minimal_emit,
    minimal_simulation,
)

pytestmark = pytest.mark.unit


def _floats_in(value: Any, path: str = "") -> List[str]:
    if isinstance(value, float):
        return [path or "<root>"]
    if isinstance(value, Mapping):
        found: List[str] = []
        for key, item in value.items():
            found.extend(_floats_in(item, f"{path}.{key}" if path else str(key)))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for index, item in enumerate(value):
            found.extend(_floats_in(item, f"{path}[{index}]"))
        return found
    return []


# ---------------------------------------------------------------------------
# The shipped example
# ---------------------------------------------------------------------------


def test_the_shipped_bundle_resolves(telco_emit: ResolvedEmit) -> None:
    assert telco_emit.simulation_id == TELCO_SIMULATION_ID
    assert telco_emit.emit_index == 0
    assert telco_emit.contract_path.name.endswith(".fluid.yml")
    assert telco_emit.product_ref == telco_emit.emit["productRef"]
    assert telco_emit.expose_id == telco_emit.emit["exposeId"]


def test_the_recomputed_contract_digest_matches_the_declared_one(
    telco_emit: ResolvedEmit,
) -> None:
    """KERNEL-SPEC §8: the proven bytes are the shipped bytes."""
    assert telco_emit.declared_contract_digest == telco_emit.contract_digest.value
    assert telco_emit.contract_digest.domain is DigestDomain.JCS_DOCUMENT


def test_the_engine_and_the_reference_validator_agree_on_the_shipped_contract(
    telco_emit: ResolvedEmit,
) -> None:
    """Two canonicalisers, one digest -- on a document with no YAML float."""
    reference = "sha256:" + canonical_digest(telco_emit.contract_document)
    assert reference == telco_emit.contract_digest.value


def test_the_contract_file_digest_is_over_the_bytes_on_disk(
    telco_emit: ResolvedEmit,
) -> None:
    assert telco_emit.contract_file_digest.domain is DigestDomain.RAW_BYTES
    assert telco_emit.contract_bytes == telco_emit.contract_path.read_bytes()


def test_the_shipped_emit_declares_a_yaml_float_and_it_is_loaded_exactly(
    telco_emit: ResolvedEmit,
) -> None:
    """``minCredibility: 0.7`` is a float in YAML and a Decimal after loading."""
    credibility = telco_emit.emit["minCredibility"]
    assert isinstance(credibility, Decimal)
    assert credibility == Decimal("0.7")
    assert str(credibility) == "0.7"


def test_no_loaded_document_carries_a_float_anywhere(telco_bundle: LoadedBundle) -> None:
    """KERNEL-SPEC §2.1 applied to the whole bundle, not just to the emit."""
    offenders: List[str] = []
    for doc_id, document in telco_bundle.flux_documents.items():
        offenders.extend(f"{doc_id}.{path}" for path in _floats_in(document))
    for doc_id, (_, document) in telco_bundle.fluid_documents.items():
        offenders.extend(f"{doc_id}.{path}" for path in _floats_in(document))
    assert offenders == []


def test_the_shipped_bundle_declares_no_additive_fields(
    telco_bundle: LoadedBundle, telco_emit: ResolvedEmit
) -> None:
    assert telco_bundle.tolerated_additive_fields == {}
    assert telco_emit.fidelity is None
    assert telco_emit.declared_row_count is None


def test_resolve_all_emits_returns_the_declared_order(telco_bundle: LoadedBundle) -> None:
    resolved = resolve_all_emits(telco_bundle, simulation_id=TELCO_SIMULATION_ID)
    declared = telco_bundle.simulation(TELCO_SIMULATION_ID)["spec"]["emits"]
    assert len(resolved) == len(declared)
    assert [item.emit_index for item in resolved] == list(range(len(declared)))


def test_the_bundle_reports_its_simulations(telco_bundle: LoadedBundle) -> None:
    assert TELCO_SIMULATION_ID in telco_bundle.simulation_ids()


# ---------------------------------------------------------------------------
# The exact loader
# ---------------------------------------------------------------------------


def test_the_exact_loader_reads_a_float_from_the_source_text(tmp_path: Path) -> None:
    """``Decimal("0.7")``, not ``Decimal(0.7)``. The difference is the point."""
    path = tmp_path / "doc.yml"
    path.write_text("a: 0.7\nb: 1.0e+3\nc: 685_230.15\nd: 1e3\n", encoding="utf-8")
    (document,) = load_documents_exact(path)
    assert document["a"] == Decimal("0.7")
    assert document["b"] == Decimal("1.0e+3")
    assert document["c"] == Decimal("685230.15")
    # PyYAML resolves YAML 1.1, whose float pattern needs a dot or a signed
    # exponent -- so a bare ``1e3`` is a *string*, not a number, for the
    # reference validator's loader as much as for this one. Pinned here so a
    # future loader change that starts reading it as a number is visible.
    assert document["d"] == "1e3"
    # The point of reading the source text: Decimal(0.7) is not Decimal("0.7").
    assert document["a"] != Decimal(0.7)


def test_the_exact_loader_keeps_timestamps_as_strings(tmp_path: Path) -> None:
    path = tmp_path / "doc.yml"
    path.write_text("t: 2026-07-01T00:00:00Z\n", encoding="utf-8")
    (document,) = load_documents_exact(path)
    assert document["t"] == "2026-07-01T00:00:00Z"


@pytest.mark.parametrize("text", ["a: .inf\n", "a: -.inf\n", "a: .nan\n", "a: 190:20:30.15\n"])
def test_the_exact_loader_refuses_a_number_with_no_digest(tmp_path: Path, text: str) -> None:
    path = tmp_path / "doc.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(FluxError) as error:
        load_documents_exact(path)
    assert error.value.code.code == "FLX-3105"


#: CPython's limit on the digits it converts between an ``int`` and its
#: decimal text (``sys.int_info.default_max_str_digits``).
_MOST_INTEGER_DIGITS = 4300


@pytest.mark.parametrize(
    "scalar",
    [
        pytest.param("9" * (_MOST_INTEGER_DIGITS + 1), id="one-digit-too-many"),
        pytest.param("-" + "9" * (_MOST_INTEGER_DIGITS + 1), id="negative"),
        pytest.param("1_" * _MOST_INTEGER_DIGITS + "1", id="grouped-by-underscores"),
        pytest.param("1" * (_MOST_INTEGER_DIGITS + 1) + ":00", id="a-sexagesimal-part"),
        pytest.param("!!int '" + "9" * (_MOST_INTEGER_DIGITS + 1) + "'", id="tagged-and-quoted"),
        pytest.param(f"0x{10**_MOST_INTEGER_DIGITS:x}", id="hex-worth-one-digit-too-many"),
    ],
)
def test_the_exact_loader_refuses_an_integer_too_long_to_read(tmp_path: Path, scalar: str) -> None:
    """CPython converts at most 4300 digits between an ``int`` and its text.

    PyYAML builds an integer with ``int()``, so a longer decimal one raised a
    bare ``ValueError`` from inside the read, and a longer value spelled in
    hex loaded, then broke the first message that printed it. Both are
    refused where they are read, by their position, never their value.
    """
    path = tmp_path / "doc.yml"
    path.write_text(f"a: 1\nb: {scalar}\n", encoding="utf-8")
    with pytest.raises(FluxError) as error:
        load_documents_exact(path)
    assert error.value.code.code == "FLX-1003"
    assert error.value.detail == (
        f"doc.yml: an integer of more than {_MOST_INTEGER_DIGITS} digits is too long "
        "to read (line 2, column 4)"
    )


@pytest.mark.parametrize("value", [10**_MOST_INTEGER_DIGITS - 1, 1 - 10**_MOST_INTEGER_DIGITS])
def test_an_integer_of_at_most_the_most_digits_still_loads(tmp_path: Path, value: int) -> None:
    """The longest integer of each sign, spelled in decimal and in hex."""
    path = tmp_path / "doc.yml"
    path.write_text(f"a: {value}\nb: {value:#x}\n", encoding="utf-8")
    assert load_documents_exact(path) == ({"a": value, "b": value},)


@pytest.mark.parametrize("scalar", ["0b_", "-0x_", "!!int abc", "!!int ''"])
def test_the_exact_loader_refuses_an_integer_it_cannot_form(tmp_path: Path, scalar: str) -> None:
    """YAML reads each of these as an integer, and PyYAML cannot build one.

    It raised a bare ``ValueError`` or ``IndexError``; the scalar is
    unreadable YAML, the loader's own refusal.
    """
    path = tmp_path / "doc.yml"
    path.write_text(f"a: 1\nb: {scalar}\n", encoding="utf-8")
    with pytest.raises(FluxError) as error:
        load_documents_exact(path)
    assert error.value.code.code == "FLX-3105"
    assert error.value.detail == (
        "doc.yml: a scalar read as an integer has no integer value (line 2, column 4)"
    )


def test_unreadable_yaml_is_reported_not_swallowed(tmp_path: Path) -> None:
    path = tmp_path / "doc.yml"
    path.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(FluxError) as error:
        load_documents_exact(path)
    assert error.value.code.code == "FLX-3105"


def test_the_exact_loader_skips_non_mapping_documents(tmp_path: Path) -> None:
    path = tmp_path / "doc.yml"
    path.write_text("- a\n---\nb: 1\n", encoding="utf-8")
    assert load_documents_exact(path) == ({"b": 1},)


def test_the_loader_is_a_safe_loader(tmp_path: Path) -> None:
    path = tmp_path / "doc.yml"
    path.write_text("a: !!python/object/apply:os.system ['echo hi']\n", encoding="utf-8")
    with pytest.raises(FluxError):
        load_documents_exact(path)
    assert issubclass(ExactLoader, yaml.SafeLoader)


# ---------------------------------------------------------------------------
# The extension namespace (RFC-02), and the additive set it emptied
# ---------------------------------------------------------------------------


def test_nothing_is_tolerated_outside_the_published_schema() -> None:
    """The seam declares no additive ``emits[]`` field at all any more.

    Everything this engine adds moved into
    ``extensions["com.agentics.flux/1"]``, which the published schema
    already carries -- so nothing is left to strip and the enumeration
    that used to name the exceptions is empty.
    """
    assert ADDITIVE_SEAM_FIELDS == frozenset()


def test_stripping_with_the_engines_own_empty_set_changes_nothing() -> None:
    document = {"id": "s", "spec": {"emits": [minimal_emit(rows=100)]}}
    stripped, removed = strip_additive_seam_fields(document)
    assert removed == ()
    assert stripped == document


def test_stripping_still_removes_a_field_a_caller_names() -> None:
    """The mechanism survives for the next additive field; only the set is empty."""
    document = {
        "id": "s",
        "rows": "a top-level field of the same name",
        "spec": {
            "rows": "a spec field of the same name",
            "emits": [minimal_emit(rows=100, fidelity={"records": {"min": 1}})],
        },
    }
    stripped, removed = strip_additive_seam_fields(document, fields=frozenset({"fidelity", "rows"}))
    assert removed == ("spec.emits[0].fidelity", "spec.emits[0].rows")
    assert stripped["rows"] == document["rows"]
    assert stripped["spec"]["rows"] == document["spec"]["rows"]
    assert "rows" not in stripped["spec"]["emits"][0]
    assert "fidelity" not in stripped["spec"]["emits"][0]
    # The caller's document is untouched.
    assert document["spec"]["emits"][0]["rows"] == 100


@pytest.mark.parametrize(
    "document",
    [{}, {"spec": "not a mapping"}, {"spec": {}}, {"spec": {"emits": "not a list"}}],
)
def test_stripping_a_document_with_no_emits_returns_it_unchanged(
    document: Dict[str, Any],
) -> None:
    stripped, removed = strip_additive_seam_fields(document)
    assert removed == ()
    assert stripped == document


def test_stripping_leaves_a_non_mapping_emit_entry_alone() -> None:
    document = {"spec": {"emits": ["not a mapping"]}}
    stripped, removed = strip_additive_seam_fields(document, fields=frozenset({"fidelity", "rows"}))
    assert removed == ()
    assert stripped == document


def test_a_bundle_declaring_the_block_in_the_extension_namespace_resolves(
    write_bundle: Any,
) -> None:
    """SEAM-FIDELITY-SPEC section 2's block, where the schema accepts it."""
    block = {"records": {"min": 1000, "max": 100000}}
    root = write_bundle(
        flux_documents=minimal_simulation(
            extension_emits={"widget_stream": {"fidelity": block, "rows": 50}}
        )
    )
    loaded = load_bundle(root)
    assert loaded.tolerated_additive_fields == {}
    resolved = resolve_emit(loaded, simulation_id="sim-widget")
    assert resolved.fidelity == block
    assert resolved.declared_row_count == 50
    assert resolved.column_mapping is None


def test_the_block_has_exactly_one_spelling(write_bundle: Any) -> None:
    """SEAM-FIDELITY-SPEC §2 names the block once, and the seam reads it once.

    A second accepted spelling would be two digests of one contract -- the
    same reason §2 refuses a bare support list -- so a key the namespace
    does not define is refused by name rather than quietly ignored.
    """
    root = write_bundle(
        flux_documents=minimal_simulation(
            extension_emits={"widget_stream": {"fidelityBlock": {"records": {"min": 1}}}}
        )
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert "fidelityBlock" in str(error.value)


def test_a_column_mapping_override_is_handed_back_unvalidated(write_bundle: Any) -> None:
    root = write_bundle(
        flux_documents=minimal_simulation(
            extension_emits={"widget_stream": {"mapping": {"amount": "event.data.amount"}}}
        )
    )
    resolved = resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert resolved.column_mapping == {"amount": "event.data.amount"}
    assert resolved.fidelity is None
    assert resolved.declared_row_count is None


def test_the_bundle_the_engine_reads_is_the_bundle_the_schema_accepts(
    write_bundle: Any,
) -> None:
    """With the block in ``extensions``, no relaxation is needed at all."""
    root = write_bundle(
        flux_documents=minimal_simulation(
            extension_emits={"widget_stream": {"fidelity": {"records": {"min": 1}}, "rows": 4}}
        )
    )
    loaded = load_bundle(root, tolerate_additive=False)
    assert resolve_emit(loaded, simulation_id="sim-widget").declared_row_count == 4


def test_the_block_where_it_used_to_live_is_now_refused(write_bundle: Any) -> None:
    """``spec.emits[].fidelity`` is exactly what the published schema closes."""
    root = write_bundle(
        flux_documents=minimal_simulation(
            emits=[minimal_emit(fidelity={"records": {"min": 1}}, rows=8)]
        )
    )
    with pytest.raises(FluxError) as error:
        load_bundle(root)
    assert error.value.code.code == "FLX-3105"
    assert "fidelity" in str(error.value)


def test_a_field_outside_the_published_schema_is_refused(write_bundle: Any) -> None:
    root = write_bundle(flux_documents=minimal_simulation(emits=[minimal_emit(somethingElse=1)]))
    with pytest.raises(FluxError) as error:
        load_bundle(root)
    assert error.value.code.code == "FLX-3105"


def test_an_unknown_key_in_the_extension_block_is_refused(write_bundle: Any) -> None:
    root = write_bundle(
        flux_documents=minimal_simulation(extension_emits={"widget_stream": {"rowz": 4}})
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3105"
    assert "rowz" in str(error.value)


def test_an_extension_block_for_an_expose_the_simulation_never_emits_is_refused(
    write_bundle: Any,
) -> None:
    """A typo'd exposeId would otherwise be a declaration nothing ever reads."""
    root = write_bundle(
        flux_documents=minimal_simulation(extension_emits={"widget_strem": {"rows": 4}})
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3105"
    assert "widget_strem" in str(error.value)


@pytest.mark.parametrize(
    "namespace_value",
    ["not a mapping", {"emits": "not a mapping"}, {"emits": {"widget_stream": "not a mapping"}}],
)
def test_a_malformed_extension_namespace_is_refused(
    write_bundle: Any, namespace_value: Any
) -> None:
    documents = minimal_simulation()
    documents[1]["extensions"] = {resolve_module.EXTENSION_NAMESPACE: namespace_value}
    root = write_bundle(flux_documents=documents)
    with pytest.raises(FluxError) as error:
        resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3105"


def test_another_dialects_namespace_is_left_alone(write_bundle: Any) -> None:
    """RFC-02 gives every namespace its own contents; this engine reads one."""
    documents = minimal_simulation()
    documents[1]["extensions"] = {"com.example.flux/1": {"emits": {"anything": "at all"}}}
    root = write_bundle(flux_documents=documents)
    resolved = resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert resolved.extension is None
    assert resolved.fidelity is None


def test_a_declared_row_count_that_is_not_an_integer_is_ignored(write_bundle: Any) -> None:
    root = write_bundle(
        flux_documents=minimal_simulation(extension_emits={"widget_stream": {"rows": True}})
    )
    resolved = resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert resolved.declared_row_count is None


@pytest.mark.parametrize("value", ["not a mapping", ["a", "b"], 7])
def test_a_fidelity_value_that_is_not_a_mapping_is_still_a_declaration(
    write_bundle: Any, value: Any
) -> None:
    """Handed on as declared, for the block's one parser to refuse (``FLX-5030``).

    Read as ``None`` it was no block at all: ``validate`` passed the bundle
    and ``verify`` reported the emit ``not declared``.
    """
    root = write_bundle(
        flux_documents=minimal_simulation(extension_emits={"widget_stream": {"fidelity": value}})
    )
    resolved = resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert resolved.fidelity == value


def test_a_null_fidelity_value_declares_no_block(write_bundle: Any) -> None:
    """As a null namespace, ``emits`` map or entry declares nothing."""
    root = write_bundle(
        flux_documents=minimal_simulation(extension_emits={"widget_stream": {"fidelity": None}})
    )
    assert resolve_emit(load_bundle(root), simulation_id="sim-widget").fidelity is None


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_a_bundle_that_does_not_validate_is_flx_3105(write_bundle: Any) -> None:
    documents = minimal_simulation()
    documents[1]["spec"]["worldRef"] = "no-such-world"
    root = write_bundle(flux_documents=documents)
    with pytest.raises(FluxError) as error:
        load_bundle(root)
    assert error.value.code.code == "FLX-3105"
    assert "no-such-world" in str(error.value)


def test_an_unresolvable_product_ref_is_flx_3102(write_bundle: Any) -> None:
    """Caught by the validator first, so the bundle never loads at all."""
    root = write_bundle(
        flux_documents=minimal_simulation(emits=[minimal_emit(product_ref="test.gold.absent")])
    )
    with pytest.raises(FluxError) as error:
        load_bundle(root)
    assert error.value.code.code == "FLX-3105"
    assert "does not resolve" in str(error.value)


def test_resolve_reports_flx_3102_when_the_product_is_gone_after_loading(
    write_bundle: Any,
) -> None:
    root = write_bundle()
    loaded = load_bundle(root)
    stripped = LoadedBundle(
        directory=loaded.directory,
        flux_documents=loaded.flux_documents,
        fluid_documents={},
        file_bytes=loaded.file_bytes,
        flux_file_names=loaded.flux_file_names,
        fluid_file_names=loaded.fluid_file_names,
        tolerated_additive_fields={},
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(stripped, simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3102"


def test_an_unknown_expose_id_is_flx_3103(write_bundle: Any) -> None:
    root = write_bundle(
        flux_documents=minimal_simulation(emits=[minimal_emit(expose_id="no_such_expose")])
    )
    with pytest.raises(FluxError) as error:
        load_bundle(root)
    assert error.value.code.code == "FLX-3105"

    # ... and once past the validator, resolve reports the seam-level code.
    good = load_bundle(write_bundle(directory=root.parent / "good"))
    document = dict(good.simulation("sim-widget"))
    document["spec"] = dict(document["spec"])
    document["spec"]["emits"] = [minimal_emit(expose_id="no_such_expose")]
    patched = LoadedBundle(
        directory=good.directory,
        flux_documents={**good.flux_documents, "sim-widget": document},
        fluid_documents=good.fluid_documents,
        file_bytes=good.file_bytes,
        flux_file_names=good.flux_file_names,
        fluid_file_names=good.fluid_file_names,
        tolerated_additive_fields={},
    )
    with pytest.raises(FluxError) as expose_error:
        resolve_emit(patched, simulation_id="sim-widget")
    assert expose_error.value.code.code == "FLX-3103"


def test_a_version_pin_the_contract_does_not_satisfy_is_flx_3104(write_bundle: Any) -> None:
    root = write_bundle(
        flux_documents=minimal_simulation(emits=[minimal_emit(fluid_version="0.7.3")]),
        fluid_documents=[minimal_contract()],
    )
    with pytest.raises(FluxError) as error:
        load_bundle(root)
    assert error.value.code.code == "FLX-3105"


def test_a_semver_range_is_resolved_by_the_reference_implementation(
    write_bundle: Any,
) -> None:
    """``^0.7.3`` accepts the contract's ``0.7.5`` -- the validator's own rule."""
    root = write_bundle(
        flux_documents=minimal_simulation(emits=[minimal_emit(fluid_version="^0.7.3")])
    )
    resolved = resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert resolved.fluid_version == "^0.7.3"
    assert resolved.contract_document["fluidVersion"] == "0.7.5"


def test_a_wrong_contract_digest_is_flx_3101(write_bundle: Any) -> None:
    wrong = "sha256:" + "0" * 64
    root = write_bundle(
        flux_documents=minimal_simulation(
            emits=[minimal_emit(provenance={"contractDigest": wrong})]
        )
    )
    with pytest.raises(FluxError) as error:
        load_bundle(root)
    # The reference validator catches it first, with its own message.
    assert error.value.code.code == "FLX-3105"
    assert "contractDigest" in str(error.value)


def test_resolve_reports_flx_3101_when_the_contract_moves_after_validation(
    write_bundle: Any,
) -> None:
    contract = minimal_contract()
    digest = "sha256:" + canonical_digest(contract)
    root = write_bundle(
        flux_documents=minimal_simulation(
            emits=[minimal_emit(provenance={"contractDigest": digest})]
        ),
        fluid_documents=[contract],
    )
    loaded = load_bundle(root)
    changed = yaml.safe_load(yaml.safe_dump(contract))
    changed["name"] = "Renamed after the plan was taken"
    patched = LoadedBundle(
        directory=loaded.directory,
        flux_documents=loaded.flux_documents,
        fluid_documents={"test.gold.widget": (loaded.directory / "contract.fluid.yml", changed)},
        file_bytes=loaded.file_bytes,
        flux_file_names=loaded.flux_file_names,
        fluid_file_names=loaded.fluid_file_names,
        tolerated_additive_fields={},
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(patched, simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3101"
    assert digest in str(error.value)


def test_the_diagnostic_says_when_a_mismatch_is_a_canonicalisation_difference(
    write_bundle: Any, tmp_path: Path
) -> None:
    """A digest taken by the reference validator over a float-carrying document.

    The two canonicalisers agree on every document with no YAML float and
    differ on every document with one. When the declared digest is the
    reference validator's, the diagnostic says so -- because "re-run plan"
    and "the two implementations disagree" are different problems.
    """
    contract = minimal_contract()
    # ``exposeQoS.errorBudget`` is a genuine ``{"type": "number"}`` field of
    # the FLUID schema, so this contract is legal FLUID *and* carries a YAML
    # float -- which is exactly the situation the two canonicalisers differ on.
    contract["exposes"][0]["qos"] = {"errorBudget": 0.5}
    directory = tmp_path / "float-bundle"
    reference_digest = "sha256:" + canonical_digest(contract)
    root = write_bundle(
        flux_documents=minimal_simulation(
            emits=[minimal_emit(provenance={"contractDigest": reference_digest})]
        ),
        fluid_documents=[contract],
        directory=directory,
    )
    loaded = load_bundle(root)
    with pytest.raises(FluxError) as error:
        resolve_emit(loaded, simulation_id="sim-widget")
    message = str(error.value)
    assert error.value.code.code == "FLX-3101"
    assert "reference validator's canonicalisation" in message


@pytest.mark.parametrize("index", [-1, 1, 99])
def test_an_emit_index_outside_the_declared_set_is_refused(write_bundle: Any, index: int) -> None:
    loaded = load_bundle(write_bundle())
    with pytest.raises(FluxError) as error:
        resolve_emit(loaded, simulation_id="sim-widget", emit_index=index)
    assert error.value.code.code == "FLX-3105"


def test_an_unknown_simulation_is_refused(write_bundle: Any) -> None:
    loaded = load_bundle(write_bundle())
    with pytest.raises(FluxError) as error:
        loaded.simulation("no-such-simulation")
    assert error.value.code.code == "FLX-3105"


def test_a_world_is_not_a_simulation(write_bundle: Any) -> None:
    loaded = load_bundle(write_bundle())
    with pytest.raises(FluxError) as error:
        loaded.simulation("world-widget")
    assert error.value.code.code == "FLX-3105"


def test_the_compiled_expose_comes_from_the_resolution(write_bundle: Any) -> None:
    resolved = resolve_emit(load_bundle(write_bundle()), simulation_id="sim-widget")
    assert resolved.compile().column_names == ("widget_id", "amount")


# ---------------------------------------------------------------------------
# SeamBundle keeps every check the base loader makes
#
# The subclass overrides one method -- Bundle.load -- so that the schema
# sees a copy with the additive emit fields removed. Everything else that
# method did has to survive the override, because each of those checks is
# a way a bundle can be malformed that nothing downstream re-checks. A
# subclass that quietly dropped one would let a bundle through the seam
# that `fluxsim validate` refuses, which is the exact failure ADR-0005's
# "one enforcement decision function, imported, not reimplemented" exists
# to rule out.
#
# Each case below is paired with the same bundle minus the defect, asserted
# to load, so "it reported an error" is never just "this bundle never
# loads".
# ---------------------------------------------------------------------------


def _errors_for(root: Path) -> List[str]:
    bundle = SeamBundle(root, flux_schema_validator())
    return list(bundle.run())


def test_the_minimal_bundle_is_the_control_and_has_no_errors(
    write_bundle: Any,
) -> None:
    """The negative control every case below is measured against."""
    assert _errors_for(write_bundle()) == []


@pytest.mark.parametrize("suffix", ["flux.yml", "fluid.yml"])
def test_unreadable_yaml_is_reported_by_file(write_bundle: Any, suffix: str) -> None:
    root = write_bundle()
    (root / f"broken.{suffix}").write_text("key: [unclosed\n", encoding="utf-8")
    errors = _errors_for(root)
    assert any(f"broken.{suffix}" in error for error in errors)


@pytest.mark.parametrize("suffix", ["flux.yml", "fluid.yml"])
def test_a_document_that_is_not_a_mapping_is_reported(write_bundle: Any, suffix: str) -> None:
    root = write_bundle()
    (root / f"scalar.{suffix}").write_text("just a string\n", encoding="utf-8")
    errors = _errors_for(root)
    assert any("not a mapping" in error for error in errors)


def test_a_duplicate_flux_document_id_is_reported(write_bundle: Any) -> None:
    root = write_bundle()
    documents = minimal_simulation()
    (root / "again.flux.yml").write_text(
        yaml.safe_dump(documents[1], sort_keys=False), encoding="utf-8"
    )
    assert any("duplicate document id" in error for error in _errors_for(root))


def test_a_duplicate_fluid_document_id_is_reported(write_bundle: Any) -> None:
    root = write_bundle(fluid_documents=[minimal_contract(), minimal_contract()])
    assert any("duplicate FLUID document id" in error for error in _errors_for(root))


def test_a_fluid_document_with_no_string_id_is_reported(write_bundle: Any) -> None:
    root = write_bundle()
    contract = dict(minimal_contract())
    contract["id"] = 17
    (root / "numbered.fluid.yml").write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8"
    )
    assert any("no string 'id'" in error for error in _errors_for(root))


def test_a_fluid_id_that_collides_with_a_flux_id_is_reported(write_bundle: Any) -> None:
    """Two documents of different kinds may not answer to one id."""
    root = write_bundle(fluid_documents=[minimal_contract(product_id="sim-widget")])
    assert any("collides with a FLUX document id" in error for error in _errors_for(root))


def _loaded_with(simulation: Mapping[str, Any], directory: Path) -> LoadedBundle:
    """A LoadedBundle around one hand-made Simulation.

    ``load_bundle`` cannot produce these cases: the published FLUX schema
    makes ``emits`` required and its entries objects, so a bundle shaped
    this way never gets past validation. ``LoadedBundle`` and
    ``resolve_emit`` are public, though, and a caller assembling one in
    process is exactly who the resolver's own guards are for.
    """
    return LoadedBundle(
        directory=directory,
        flux_documents={str(simulation["id"]): simulation},
        fluid_documents={},
        file_bytes={},
        flux_file_names=(),
        fluid_file_names=(),
        tolerated_additive_fields={},
    )


def test_a_simulation_that_declares_no_emits_is_refused(tmp_path: Path) -> None:
    _, simulation = minimal_simulation()
    spec = {key: value for key, value in simulation["spec"].items() if key != "emits"}
    loaded = _loaded_with({**simulation, "spec": spec}, tmp_path)
    with pytest.raises(FluxError) as error:
        resolve_emit(loaded, simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3105"
    assert "no emits" in (error.value.detail or "")
    # resolve_all_emits reads the same field and reports nothing to resolve
    # rather than raising, so a caller can ask a Simulation what it emits.
    assert resolve_all_emits(loaded, simulation_id="sim-widget") == ()


def test_an_emits_entry_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    _, simulation = minimal_simulation()
    loaded = _loaded_with(
        {**simulation, "spec": {**simulation["spec"], "emits": ["not a mapping"]}}, tmp_path
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(loaded, simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3105"
    assert "not a mapping" in (error.value.detail or "")


def test_a_pin_the_contract_does_not_satisfy_is_flx_3104(tmp_path: Path) -> None:
    """FLX-3104 is raisable, and names the pin and the declaration.

    ``load_bundle`` never gets this far -- the reference validator refuses
    the same mismatch first, with FLX-3105 (see the test above, which pins
    that ordering). The seam keeps its own check anyway, because
    ``resolve_emit`` is public and a caller who assembled a LoadedBundle in
    process has had no validator run over it.
    """
    contract = dict(minimal_contract())
    contract["fluidVersion"] = "0.6.0"
    _, simulation = minimal_simulation()
    loaded = LoadedBundle(
        directory=tmp_path,
        flux_documents={"sim-widget": simulation},
        fluid_documents={"test.gold.widget": (tmp_path / "contract.fluid.yml", contract)},
        file_bytes={"contract.fluid.yml": b""},
        flux_file_names=(),
        fluid_file_names=("contract.fluid.yml",),
        tolerated_additive_fields={},
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(loaded, simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3104"
    assert "0.6.0" in (error.value.detail or "")


def test_a_contract_whose_declared_version_is_not_a_string_is_flx_3104(
    tmp_path: Path,
) -> None:
    contract = {**minimal_contract(), "fluidVersion": 7}
    _, simulation = minimal_simulation()
    loaded = LoadedBundle(
        directory=tmp_path,
        flux_documents={"sim-widget": simulation},
        fluid_documents={"test.gold.widget": (tmp_path / "contract.fluid.yml", contract)},
        file_bytes={"contract.fluid.yml": b""},
        flux_file_names=(),
        fluid_file_names=("contract.fluid.yml",),
        tolerated_additive_fields={},
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(loaded, simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3104"


@pytest.mark.parametrize("dropped", ["flux", "fluid"])
def test_two_reads_that_disagree_about_the_documents_are_refused(
    write_bundle: Any, monkeypatch: pytest.MonkeyPatch, dropped: str
) -> None:
    """The validating read and the exact read must see the same documents.

    ``load_bundle`` reads the bundle twice -- once through the reference
    validator's loader, which decides whether the bundle is legal, and once
    through ExactLoader, which supplies the documents every digest is taken
    over. If those two ever disagreed about which documents exist, a digest
    would be taken over a document nothing validated. The disagreement is
    simulated here by making the exact read skip a file, because no input
    produces it: that is exactly why it is worth a guard rather than an
    assumption.
    """
    root = write_bundle()
    real = resolve_module.load_documents_exact

    def skipping(path: Path) -> Any:
        if path.name.endswith(f".{dropped}.yml"):
            return ()
        return real(path)

    monkeypatch.setattr(resolve_module, "load_documents_exact", skipping)
    with pytest.raises(FluxError) as error:
        load_bundle(root)
    assert error.value.code.code == "FLX-3105"
    assert "disagree" in (error.value.detail or "")


def test_the_diagnostic_stays_quiet_when_the_mismatch_is_not_a_canonicalisation_one(
    write_bundle: Any,
) -> None:
    """The note is only added when it is true.

    The pair to the test above. There the declared digest *is* the reference
    validator's canonicalisation of the contract on disk, and the diagnostic
    says so. Here it is neither canonicaliser's digest of anything in the
    bundle -- the contract really did move -- so the note must be absent,
    or "the two implementations disagree" would be printed over a plain
    stale digest and send a reader after the wrong problem.
    """
    contract = minimal_contract()
    digest = "sha256:" + canonical_digest(contract)
    root = write_bundle(
        flux_documents=minimal_simulation(
            emits=[minimal_emit(provenance={"contractDigest": digest})]
        ),
        fluid_documents=[contract],
    )
    loaded = load_bundle(root)
    # Both the in-memory document and the file move, so neither
    # canonicalisation of anything on disk equals the declared digest.
    changed = yaml.safe_load(yaml.safe_dump(contract))
    changed["name"] = "Renamed"
    (loaded.directory / "contract.fluid.yml").write_text(
        yaml.safe_dump(changed, sort_keys=False), encoding="utf-8"
    )
    patched = LoadedBundle(
        directory=loaded.directory,
        flux_documents=loaded.flux_documents,
        fluid_documents={"test.gold.widget": (loaded.directory / "contract.fluid.yml", changed)},
        file_bytes=loaded.file_bytes,
        flux_file_names=loaded.flux_file_names,
        fluid_file_names=loaded.fluid_file_names,
        tolerated_additive_fields={},
    )
    with pytest.raises(FluxError) as error:
        resolve_emit(patched, simulation_id="sim-widget")
    assert error.value.code.code == "FLX-3101"
    assert "canonicalisation" not in str(error.value)
