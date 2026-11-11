# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §2: canonicalisation, digest domains, masking, §11 builders.

The published vectors in ``tests/vectors/canonical-vectors.json`` are the
spine of this file. Each one carries the exact RFC 8785 bytes a document
canonicalises to, hand-written and reviewed, plus the sha256 of those
bytes -- and the digest assertion recomputes that sha256 with plain
``hashlib`` rather than with the engine, so a change to
``flux_engine.core.digest`` cannot move the vector and its check
together.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.core import artifact
from flux_engine.core.canonical import (
    JCS_MAX_SAFE_INTEGER,
    NON_DIGEST_FIELDS,
    SELF_REFERENTIAL_FIELDS,
    canonical_bytes,
    canonicalise,
    coerce_key,
    field_paths,
    mask_document,
    nfc,
    partition_fields,
    sha256_hex,
)
from flux_engine.core.digest import (
    DigestDomain,
    DigestRef,
    build_input_digest,
    build_plan_digest,
    digest_of_bytes,
    digest_of_document,
    digest_of_stream,
    digests_match,
    require_domain,
)
from flux_engine.core.errors import FluxError

VECTOR_PATH = Path(__file__).resolve().parent.parent / "vectors" / "canonical-vectors.json"
_VECTOR_FILE = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
VECTORS = _VECTOR_FILE["vectors"]
POSITIVE = [vector for vector in VECTORS if "canonical" in vector]
NEGATIVE = [vector for vector in VECTORS if "error" in vector]

_TAGS = {"$decimal", "$int", "$float", "$bytes", "$tuple", "$pairs"}


def decode(node: Any) -> Any:
    """Decode a vector's ``document`` into the Python value it stands for.

    JSON cannot spell a ``Decimal``, a ``float``-that-must-be-rejected, an
    integer outside its own safe range, ``bytes``, or a non-string mapping
    key, so the vector file tags those (see its ``encoding`` block). The
    tags are decoded here, in the test, so the vector file stays readable
    by anything that can read JSON.
    """
    if isinstance(node, dict):
        tags = _TAGS & set(node)
        if tags:
            (tag,) = tags
            payload = node[tag]
            if tag == "$decimal":
                return Decimal(payload)
            if tag == "$int":
                return int(payload)
            if tag == "$float":
                return float(payload)
            if tag == "$bytes":
                return bytes.fromhex(payload)
            if tag == "$tuple":
                return tuple(decode(item) for item in payload)
            return {decode(key): decode(value) for key, value in payload}
        return {key: decode(value) for key, value in node.items()}
    if isinstance(node, list):
        return [decode(item) for item in node]
    return node


def _ids(vectors: List[Dict[str, Any]]) -> List[str]:
    return [vector["name"] for vector in vectors]


@pytest.mark.unit
def test_the_vector_file_is_not_empty_and_has_both_kinds() -> None:
    """A vector file that silently lost its contents must fail loudly."""
    assert _VECTOR_FILE["vectorVersion"] == 1
    assert POSITIVE, "no positive vectors"
    assert NEGATIVE, "no rejection vectors"
    names = [vector["name"] for vector in VECTORS]
    assert len(names) == len(set(names)), "duplicate vector names"


@pytest.mark.unit
@pytest.mark.parametrize("vector", POSITIVE, ids=_ids(POSITIVE))
def test_document_canonicalises_to_the_published_bytes(vector: Dict[str, Any]) -> None:
    assert canonical_bytes(decode(vector["document"])) == vector["canonical"].encode("utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("vector", POSITIVE, ids=_ids(POSITIVE))
def test_published_digest_is_the_sha256_of_the_published_bytes(
    vector: Dict[str, Any],
) -> None:
    """Recomputed with hashlib, deliberately not through the engine.

    If this used ``digest_of_document`` it would only assert that the
    engine agrees with itself.
    """
    expected = "sha256:" + hashlib.sha256(vector["canonical"].encode("utf-8")).hexdigest()
    assert vector["digest"] == expected


@pytest.mark.unit
@pytest.mark.parametrize("vector", POSITIVE, ids=_ids(POSITIVE))
def test_engine_digest_matches_the_published_digest(vector: Dict[str, Any]) -> None:
    ref = digest_of_document(decode(vector["document"]), subject=vector["name"])
    assert ref.value == vector["digest"]
    assert ref.domain is DigestDomain.JCS_DOCUMENT


@pytest.mark.unit
@pytest.mark.parametrize("vector", NEGATIVE, ids=_ids(NEGATIVE))
def test_rejection_vector_raises_its_code(vector: Dict[str, Any]) -> None:
    with pytest.raises(FluxError) as excinfo:
        canonical_bytes(decode(vector["document"]))
    assert excinfo.value.code.code == vector["error"]


@pytest.mark.unit
def test_nfc_and_nfd_spellings_share_one_digest() -> None:
    """KERNEL-SPEC §2.1's normalisation trap, stated as a pair of vectors."""
    by_name = {vector["name"]: vector for vector in POSITIVE}
    composed = by_name["nfc-value-composed"]
    decomposed = by_name["nfd-value-decomposed"]
    assert decode(composed["document"]) != decode(decomposed["document"])
    assert composed["digest"] == decomposed["digest"]


@pytest.mark.unit
def test_decimal_scale_is_part_of_the_digest() -> None:
    """Two Decimals of equal value but different declared scale differ."""
    by_name = {vector["name"]: vector for vector in POSITIVE}
    assert (
        by_name["decimal-keeps-its-scale"]["digest"]
        != by_name["decimal-scale-is-part-of-the-value"]["digest"]
    )
    assert (
        decode(by_name["decimal-keeps-its-scale"]["document"])["amount"]
        == decode(by_name["decimal-scale-is-part-of-the-value"]["document"])["amount"]
    )


# --- the canonicalisation rules, stated directly ------------------------


@pytest.mark.unit
def test_nfc_normalises_keys_and_values() -> None:
    assert nfc("café") == "café"
    canonical = canonicalise({"café": "café"})
    assert canonical == {"café": "café"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "expected"),
    [(True, "true"), (False, "false"), (7, "7"), (None, "null"), (Decimal("1.50"), "1.50")],
)
def test_key_coercion_uses_the_json_spelling(key: Any, expected: str) -> None:
    assert coerce_key(key) == expected


@pytest.mark.unit
def test_float_is_rejected_wherever_it_appears() -> None:
    for document in ({"x": 1.5}, {"x": [1, 1.5]}, {"x": {"y": 1.5}}, [1.5]):
        with pytest.raises(FluxError) as excinfo:
            canonicalise(document)
        assert excinfo.value.code.code == "FLX-1001"


@pytest.mark.unit
def test_bool_is_not_an_int() -> None:
    """``bool`` subclasses ``int``; JSON spells the two differently."""
    assert canonical_bytes({"a": True}) == b'{"a":true}'
    assert canonical_bytes({"a": 1}) == b'{"a":1}'


@pytest.mark.unit
def test_integers_at_the_edge_of_the_jcs_domain() -> None:
    assert canonicalise(JCS_MAX_SAFE_INTEGER) == JCS_MAX_SAFE_INTEGER
    assert canonicalise(-JCS_MAX_SAFE_INTEGER) == -JCS_MAX_SAFE_INTEGER
    for value in (JCS_MAX_SAFE_INTEGER + 1, -(JCS_MAX_SAFE_INTEGER + 1)):
        with pytest.raises(FluxError) as excinfo:
            canonicalise(value)
        assert excinfo.value.code.code == "FLX-1003"


@pytest.mark.unit
def test_canonicalise_is_idempotent() -> None:
    document = {"b": Decimal("1.0"), "a": ["x", {"c": None}]}
    once = canonicalise(document)
    assert canonicalise(once) == once


@pytest.mark.unit
def test_sha256_hex_is_the_stdlib_digest() -> None:
    assert sha256_hex(b"flux") == hashlib.sha256(b"flux").hexdigest()


# --- properties ---------------------------------------------------------

_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1),
    st.text(max_size=8),
)
_JSON_DOCUMENTS = st.dictionaries(
    keys=st.text(max_size=6),
    values=st.recursive(
        _JSON_SCALARS,
        lambda children: st.lists(children, max_size=3)
        | st.dictionaries(st.text(max_size=4), children, max_size=3),
        max_leaves=6,
    ),
    max_size=4,
)


@pytest.mark.property
@given(document=_JSON_DOCUMENTS)
def test_canonical_bytes_do_not_depend_on_key_insertion_order(
    document: Dict[str, Any],
) -> None:
    """KERNEL-SPEC §2.1: the digest is of the content, not of the ordering."""
    reversed_document = dict(reversed(list(document.items())))
    assert canonical_bytes(document) == canonical_bytes(reversed_document)


@pytest.mark.property
@given(text=st.text(max_size=12))
def test_canonical_bytes_do_not_depend_on_unicode_spelling(text: str) -> None:
    import unicodedata

    decomposed = unicodedata.normalize("NFD", text)
    assert canonical_bytes({"k": text}) == canonical_bytes({"k": decomposed})


@pytest.mark.property
@given(document=_JSON_DOCUMENTS)
def test_canonicalise_is_a_fixed_point(document: Dict[str, Any]) -> None:
    once = canonicalise(document)
    assert canonicalise(once) == once


# --- masking and the §2.4 totality rule ---------------------------------


def _receipt() -> Dict[str, Any]:
    """A receipt-shaped document carrying every masked field there is.

    Built from the two mask sets themselves rather than from a typed
    list, so a field added to either set appears here automatically and
    the totality assertions below keep covering the whole rule.
    """
    document: Dict[str, Any] = {
        "kernelVersion": "0.1.0",
        "run": {"seed": 42, "nested": {"depth": 3}},
        "properties": [{"property": "mean", "column": "amount"}],
    }
    for field in sorted(NON_DIGEST_FIELDS):
        document[field] = f"volatile:{field}"
    for field in sorted(SELF_REFERENTIAL_FIELDS):
        document[field] = "sha256:" + "0" * 64
    document["run"]["generated_at"] = "2026-01-01T00:00:00Z"
    return document


@pytest.mark.unit
def test_partition_fields_splits_a_synthetic_document_totally() -> None:
    """``partition_fields`` itself, on a document built from the mask sets.

    NOT §2.4's totality test, whatever it used to be called: the document
    is constructed by iterating ``NON_DIGEST_FIELDS`` and
    ``SELF_REFERENTIAL_FIELDS``, so every field in it is masked because it
    was built from the mask sets, and ``all_fields == digested | masked``
    is a tautology. §2.4 asks for the test over the artifact the engine
    actually writes; that one lives in
    ``tests/stages/test_receipt_totality.py`` and enumerates a real
    ``receipt.json``.
    """
    receipt = _receipt()
    partition = partition_fields(receipt)
    assert set(partition.digested).isdisjoint(partition.masked)
    assert partition.all_fields == field_paths(receipt)
    assert set(partition.all_fields) == set(partition.digested) | set(partition.masked)
    # Masking is by field name at any depth, not only at the top level: a
    # nested block's timestamp is as volatile as a top-level one.
    assert "run.generated_at" in partition.masked
    assert "run.seed" in partition.digested


@pytest.mark.unit
def test_masked_fields_are_exactly_the_two_declared_sets() -> None:
    partition = partition_fields(_receipt())
    leaf_names = {path.split(".")[-1] for path in partition.masked}
    assert leaf_names == NON_DIGEST_FIELDS | SELF_REFERENTIAL_FIELDS


@pytest.mark.unit
def test_masking_reaches_nested_blocks() -> None:
    masked = mask_document(_receipt())
    assert "generated_at" not in masked
    assert "generated_at" not in masked["run"]
    assert masked["run"]["seed"] == 42


@pytest.mark.unit
def test_volatile_fields_do_not_move_the_digest() -> None:
    first = _receipt()
    second = _receipt()
    for field in NON_DIGEST_FIELDS:
        second[field] = "something else entirely"
    second["planDigest"] = "sha256:" + "1" * 64
    assert (
        digest_of_document(mask_document(first), subject="a").value
        == digest_of_document(mask_document(second), subject="b").value
    )


@pytest.mark.unit
def test_a_digested_field_does_move_the_digest() -> None:
    """The negative control for the test above."""
    first = _receipt()
    second = _receipt()
    second["run"]["seed"] = 43
    assert (
        digest_of_document(mask_document(first), subject="a").value
        != digest_of_document(mask_document(second), subject="b").value
    )


@pytest.mark.unit
def test_artifact_envelope_mask_is_a_subset_of_the_kernel_mask() -> None:
    """``core.artifact`` masks the envelope; ``core.canonical`` masks the rule.

    The envelope set is deliberately smaller (an envelope has no
    ``hostname``), but it must never contain a field the kernel would
    happily hash, or two modules would disagree about what a digest covers.
    """
    assert artifact.ENVELOPE_NON_DIGEST_FIELDS <= NON_DIGEST_FIELDS


# --- digest references --------------------------------------------------


@pytest.mark.unit
def test_digest_ref_rejects_a_malformed_value() -> None:
    for value in ("sha256:abc", "deadbeef", "sha256:" + "A" * 64, "md5:" + "0" * 32):
        with pytest.raises(ValueError):
            DigestRef(value=value, domain=DigestDomain.RAW_BYTES, subject="x")


@pytest.mark.unit
def test_the_same_bytes_in_two_domains_are_two_claims() -> None:
    """KERNEL-SPEC §2.3: a cross-domain comparison is FLX-1002, not False."""
    payload = b"{}"
    raw = digest_of_bytes(payload, subject="file")
    stream = digest_of_stream(payload, subject="stream")
    assert raw.value == stream.value
    with pytest.raises(FluxError) as excinfo:
        digests_match(raw, stream)
    assert excinfo.value.code.code == "FLX-1002"


@pytest.mark.unit
def test_equality_ignores_the_subject_but_not_the_value() -> None:
    left = digest_of_bytes(b"a", subject="left")
    same = digest_of_bytes(b"a", subject="right")
    other = digest_of_bytes(b"b", subject="left")
    assert left == same
    assert left != other
    assert len({left, same}) == 1


@pytest.mark.unit
def test_require_domain_names_the_domain_it_wanted() -> None:
    ref = digest_of_bytes(b"a", subject="lockfile")
    assert require_domain(ref, DigestDomain.RAW_BYTES) is ref
    with pytest.raises(FluxError) as excinfo:
        require_domain(ref, DigestDomain.JCS_DOCUMENT)
    assert "jcs-document" in str(excinfo.value)


@pytest.mark.unit
def test_digest_ref_is_not_equal_to_a_bare_string() -> None:
    ref = digest_of_bytes(b"a", subject="file")
    assert ref != ref.value
    assert str(ref) == ref.value
    assert ref.hex == ref.value.split(":")[1]


# --- §11 builders -------------------------------------------------------

_KERNEL_VERSION = "0.1.0"
_SEED_TREE_VERSION = "flux-seed/1"
_TRACE_VERSION = "flux-trace/1"


def _input_digest(**overrides: Any) -> Any:
    arguments: Dict[str, Any] = {
        "kernel_version": _KERNEL_VERSION,
        "seed_tree_version": _SEED_TREE_VERSION,
        "trace_version": _TRACE_VERSION,
        "documents": {"world.flux.yml": b"kind: World\n", "sim.flux.yml": b"kind: Simulation\n"},
        "fluid_documents": {"payment.fluid.yml": b"kind: DataProduct\n"},
        "lock": None,
        "attribute_model_digest": None,
    }
    arguments.update(overrides)
    return build_input_digest(**arguments)


@pytest.mark.unit
def test_input_digest_document_has_the_spec_field_set() -> None:
    built = _input_digest()
    assert set(built.document) == {
        "kernelVersion",
        "seedTreeVersion",
        "traceVersion",
        "documents",
        "fluidDocuments",
        "lock",
        "attributeModelDigest",
    }
    assert built.ref.domain is DigestDomain.JCS_DOCUMENT


@pytest.mark.unit
def test_input_digest_documents_are_raw_byte_digests_in_sorted_order() -> None:
    built = _input_digest()
    assert list(built.document["documents"]) == sorted(built.document["documents"])
    expected = "sha256:" + hashlib.sha256(b"kind: World\n").hexdigest()
    assert built.document["documents"]["world.flux.yml"] == expected


@pytest.mark.unit
def test_input_digest_moves_when_any_input_byte_moves() -> None:
    baseline = _input_digest().ref
    changed = _input_digest(
        documents={"world.flux.yml": b"kind: World \n", "sim.flux.yml": b"kind: Simulation\n"}
    ).ref
    assert baseline != changed


@pytest.mark.unit
def test_a_missing_lockfile_is_hashed_as_a_fact() -> None:
    """ "No lockfile" and "this lockfile" must not collide."""
    without = _input_digest().ref
    with_lock = _input_digest(lock=b"lockfile bytes").ref
    assert without != with_lock
    assert _input_digest().document["lock"] is None


@pytest.mark.unit
def test_input_digest_refuses_a_reference_from_the_wrong_domain() -> None:
    document_ref = digest_of_document({"a": 1}, subject="parsed")
    with pytest.raises(FluxError) as excinfo:
        _input_digest(documents={"world.flux.yml": document_ref})
    assert excinfo.value.code.code == "FLX-1002"


@pytest.mark.unit
def test_plan_digest_masks_its_own_digest_and_the_timestamp() -> None:
    plan = {
        "planDigest": "sha256:" + "0" * 64,
        "generated_at": "2026-01-01T00:00:00Z",
        "steps": [{"id": "apply", "seed": 42}],
    }
    stamped = dict(plan, generated_at="2026-06-01T12:00:00Z", planDigest="sha256:" + "f" * 64)
    assert build_plan_digest(plan).ref == build_plan_digest(stamped).ref
    assert set(build_plan_digest(plan).document) == {"steps"}


@pytest.mark.unit
def test_plan_digest_moves_when_the_plan_body_moves() -> None:
    plan = {"generated_at": "t", "steps": [{"id": "apply", "seed": 42}]}
    tampered = {"generated_at": "t", "steps": [{"id": "apply", "seed": 43}]}
    assert build_plan_digest(plan).ref != build_plan_digest(tampered).ref


@pytest.mark.unit
def test_plan_digest_document_is_the_masked_form_that_was_hashed() -> None:
    plan = {"generated_at": "t", "steps": [{"id": "apply"}]}
    built = build_plan_digest(plan)
    assert built.ref.value == "sha256:" + sha256_hex(canonical_bytes(built.document))


@pytest.mark.unit
def test_canonical_bytes_of_a_tuple_document() -> None:
    """Sequences other than list canonicalise as arrays; bytes do not."""
    assert canonical_bytes({"a": (1, 2)}) == b'{"a":[1,2]}'


@pytest.mark.unit
def test_field_paths_elides_list_indices() -> None:
    paths: Tuple[str, ...] = field_paths(
        {"properties": [{"property": "mean"}, {"property": "support"}], "empty": {}}
    )
    assert paths == ("empty", "properties[].property")


# --- guards -------------------------------------------------------------


@pytest.mark.unit
def test_a_value_class_with_no_json_form_is_refused() -> None:
    for value in ({1, 2}, object(), Path(".")):
        with pytest.raises(FluxError) as excinfo:
            canonicalise({"x": value})
        assert excinfo.value.code.code == "FLX-1005"


@pytest.mark.unit
def test_the_attribute_model_digest_is_carried_in_either_spelling() -> None:
    """§11 names ``attributeModelDigest``; it is a parsed-document digest."""
    ref = digest_of_document({"attributes": []}, subject="attributeModel")
    from_ref = _input_digest(attribute_model_digest=ref)
    from_string = _input_digest(attribute_model_digest=ref.value)
    assert from_ref.document["attributeModelDigest"] == ref.value
    assert from_ref.ref == from_string.ref
    assert from_ref.ref != _input_digest().ref


@pytest.mark.unit
def test_an_attribute_model_digest_from_the_wrong_domain_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        _input_digest(attribute_model_digest=digest_of_bytes(b"raw", subject="file"))
    assert excinfo.value.code.code == "FLX-1002"


@pytest.mark.unit
def test_a_malformed_attribute_model_digest_is_refused() -> None:
    with pytest.raises(ValueError):
        _input_digest(attribute_model_digest="not-a-digest")
