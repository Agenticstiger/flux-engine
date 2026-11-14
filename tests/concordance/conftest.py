# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the fidelity-check suite.

The published vectors (``tests/vectors/fidelity-vectors.json``) are the
single source of the contract, the fidelity block and the committed slice
these tests run against. Nothing here restates a number from that file:
every count, digest and margin the tests assert is either read back from
the file or recomputed, and the file itself is regenerated from the same
authored inputs.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterator, List, Mapping, Tuple

import pytest

from flux_engine.concordance.boundary.slice import CommittedState, SliceReference, commit_state
from flux_engine.concordance.compiler import CompiledFidelity, compile_fidelity
from flux_engine.concordance.predicate import (
    DeclaredExpose,
    FidelityBlock,
    PropertyKind,
    parse_fidelity_block,
)
from flux_engine.concordance.record import Ed25519Signer

REPO_ROOT = Path(__file__).resolve().parents[2]
VECTORS_PATH = REPO_ROOT / "tests" / "vectors" / "fidelity-vectors.json"

#: Where the FLUX spec repo's shipped telco bundle lives when it is checked
#: out beside this repo. Tests that need the real documents skip without it.
_EXAMPLE_CANDIDATES = (
    REPO_ROOT.parent / "FLUX" / "examples" / "telco-payment-recovery",
    REPO_ROOT / "examples" / "telco-payment-recovery",
)

#: A fixed 32-byte signing key. Not a secret and not drawn from entropy:
#: a test that signed with a random key could not pin a signature, and a
#: key material source is the caller's decision (see record.py).
TEST_PRIVATE_KEY = bytes(range(32, 64))

_DOCUMENT: Dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

#: Property kinds added after the published vectors file (``vectorsVersion``
#: 1), each with the module that pins it instead. The file is a pinning
#: artefact, and a new kind added to its one block would move every digest
#: it pins. The coverage tests over the file accept exactly these kinds as
#: absent from it, and only while the named module exists, so a kind can be
#: neither dropped from the file nor added to the engine without a pin.
PINNED_OUTSIDE_THE_VECTORS: Mapping[PropertyKind, str] = MappingProxyType(
    {PropertyKind.LEVEL_HISTOGRAM: "tests/concordance/test_level_histogram.py"}
)


def telco_example_dir() -> Path | None:
    """Return the shipped telco bundle directory, or ``None`` if absent."""
    for candidate in _EXAMPLE_CANDIDATES:
        if (candidate / "payment-recovery.fluid.yml").is_file():
            return candidate
    return None


def total_variation(a: Mapping[Any, int], b: Mapping[Any, int]) -> Fraction:
    """Total variation between two count tables, in exact rationals.

    The definition a total-variation verdict is held to: half the summed
    absolute difference of the two sides' shares, over every cell either
    side names. A cell one side lacks counts as zero there.
    """
    k_a, k_b = sum(a.values()), sum(b.values())
    cells = set(a) | set(b)
    return Fraction(1, 2) * sum(
        (abs(Fraction(a.get(cell, 0), k_a) - Fraction(b.get(cell, 0), k_b)) for cell in cells),
        Fraction(0),
    )


def vectors_document() -> Dict[str, Any]:
    """A deep copy of the published vectors document."""
    return copy.deepcopy(_DOCUMENT)


def build_expose(document: Mapping[str, Any]) -> DeclaredExpose:
    """The vectors file's expose, as the declaration the block is checked against.

    Reads the declared type spellings off the vectors file's own contract
    rather than restating them, so ``support: declared`` (which takes a
    column's levels from its ``enum(...)`` type) resolves here exactly the
    way it resolves against a real ``.fluid.yml``.
    """
    declared = document["expose"]
    return DeclaredExpose(
        expose_id=declared["exposeId"],
        column_names=tuple(declared["columns"]),
        column_types=contract_column_types(document),
    )


def contract_column_types(document: Mapping[str, Any]) -> Mapping[str, str]:
    """Column name -> declared type spelling, read off the vectors' contract."""
    for expose in document["contract"]["exposes"]:
        if expose["exposeId"] == document["expose"]["exposeId"]:
            return {
                str(column["name"]): str(column.get("type", ""))
                for column in expose["contract"]["schema"]
            }
    raise KeyError(document["expose"]["exposeId"])


def build_compiled(document: Mapping[str, Any]) -> CompiledFidelity:
    """Compile the vectors file's block.

    The block lives in the document-level extension entry
    (SEAM-FIDELITY-SPEC §2), which is also what ``H`` and ``G`` are taken
    over -- so the compiler is handed the emit entry and the extension
    entry separately, exactly as :mod:`flux_engine.stages.pipeline` hands
    them over.
    """
    return compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=document["extension"],
        expose=build_expose(document),
    )


def blinding_of(document: Mapping[str, Any]) -> bytes:
    """The blinding value the vectors file commits under."""
    return bytes.fromhex(document["blindingHex"])


def build_committed(document: Mapping[str, Any], compiled: CompiledFidelity) -> CommittedState:
    """Commit the vectors file's slice as a committed state."""
    return commit_state(
        rows=document["committedSlice"],
        compiled=compiled,
        ordinal=document["stateOrdinal"],
        committed_at=document["committedAt"],
        blinding=blinding_of(document),
        slice_reference=SliceReference(
            uri="file:///committed.ndjson",
            first_line=1,
            last_line=len(document["committedSlice"]),
        ),
    )


@pytest.fixture(scope="session")
def vectors() -> Dict[str, Any]:
    """The published vectors document."""
    return _DOCUMENT


@pytest.fixture()
def document(vectors: Mapping[str, Any]) -> Dict[str, Any]:
    """A mutable deep copy of the vectors document, for mutation cases."""
    return copy.deepcopy(dict(vectors))


@pytest.fixture()
def expose(document: Mapping[str, Any]) -> DeclaredExpose:
    """The vectors file's expose."""
    return build_expose(document)


@pytest.fixture()
def block(document: Mapping[str, Any], expose: DeclaredExpose) -> FidelityBlock:
    """The parsed fidelity block."""
    return parse_fidelity_block(document["extension"]["fidelity"], expose=expose)


@pytest.fixture()
def compiled(document: Mapping[str, Any]) -> CompiledFidelity:
    """The compiled fidelity block."""
    return build_compiled(document)


@pytest.fixture()
def committed(document: Mapping[str, Any], compiled: CompiledFidelity) -> CommittedState:
    """The committed state over the vectors file's slice."""
    return build_committed(document, compiled)


@pytest.fixture()
def committed_rows(document: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The committed slice's records."""
    return [dict(row) for row in document["committedSlice"]]


@pytest.fixture()
def signer() -> Ed25519Signer:
    """The suite's signing key."""
    return Ed25519Signer(private_key=TEST_PRIVATE_KEY, key_id="test-verifier")


def evaluation_cases(document: Mapping[str, Any]) -> Iterator[Tuple[str, Mapping[str, Any]]]:
    """Yield ``(id, case)`` for every published evaluation case."""
    for case in document["evaluations"]:
        yield case["id"], case


def evaluation_ids() -> List[str]:
    """The published evaluation case ids, for parametrisation."""
    return [case["id"] for case in _DOCUMENT["evaluations"]]


def evaluation_case(identifier: str) -> Mapping[str, Any]:
    """One published evaluation case by id."""
    for case in _DOCUMENT["evaluations"]:
        if case["id"] == identifier:
            return copy.deepcopy(case)
    raise KeyError(identifier)


def write_slice(path: Path, rows: List[Mapping[str, Any]]) -> str:
    """Write *rows* as a newline-delimited JSON slice; return its ``file://`` URL."""
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    return path.as_uri()
