# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The fidelity check: predicate, digests, boundary, verifier, record, gate.

Implements SEAM-FIDELITY-SPEC §2-§8. The pipeline, in the order a run
walks it:

``predicate``
    Parse and validate the ``emits[].fidelity`` block against the
    referenced expose, offline, before any statistic is formed (§2).
``compiler``
    Take ``H`` and ``G``, propagate each property's encoding-error bound
    ``ε``, compile the compared margin ``τ′ = τ − ε``, and bound every
    intermediate the comparison forms (§3, KERNEL-SPEC §12).
``boundary``
    Fix a record set as a committed state: the per-column commitment
    ``C1``, the derived statistic ``S``, the commitment ``C2`` over ``S``
    and a blinding value, and the check counter (§4).
``verifier``
    Recompute the synthetic dataset's statistics on the same grid and
    compare each property in integer cross-multiplied form, fail-closed
    (§5).
``record``
    Write and sign one fidelity record per verified emit, with the
    generated scope statement (§6).
``gate``
    Re-run every check at ship time and return a decision the ship stage
    turns into a bundle or a withheld event (§7).

The module names in this package are internal. Every diagnostic, log line
and docstring here calls the check a **fidelity check** and its artefact a
**fidelity record**, which are the names on every public surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List

# The one module that registers this package's error codes is imported with the package, so
# the catalogue has them however the package is reached; the rest are loaded on first use
# (PEP 562). Importing a module of this package, as the generator does for the predicate,
# no longer loads the compiler, the verifier, the record and the gate with it: a worker
# process that only generates rows has no use for them.
from flux_engine.concordance.errors import OWNED_CODES

if TYPE_CHECKING:
    from flux_engine.concordance.boundary import (
        BoundaryStore,
        ColumnCommitment,
        CommittedState,
        DerivedStatistic,
        SliceReference,
        commit_state,
        compute_statistic,
        read_slice,
    )
    from flux_engine.concordance.compiler import (
        CompiledFidelity,
        CompiledProperty,
        compile_fidelity,
    )
    from flux_engine.concordance.errors import (
        OWNED_CODES,
    )
    from flux_engine.concordance.gate import (
        GateAction,
        GateCheck,
        GateDecision,
        PackedEmit,
        run_gate,
    )
    from flux_engine.concordance.predicate import (
        ColumnDeclaration,
        DeclaredExpose,
        ExposeSchema,
        FidelityBlock,
        PropertyDeclaration,
        PropertyKind,
        expose_from_contract,
        parse_fidelity_block,
    )
    from flux_engine.concordance.record import (
        Ed25519Signer,
        FidelityRecord,
        SyntheticReference,
        UnsignedFallbackSigner,
        build_record,
        load_record,
        verify_record,
    )
    from flux_engine.concordance.verifier import (
        ClearStatistic,
        FidelityEvaluation,
        NoProofSystem,
        PropertyOutcome,
        Refusal,
    )

__all__ = [
    "OWNED_CODES",
    "BoundaryStore",
    "ClearStatistic",
    "ColumnCommitment",
    "ColumnDeclaration",
    "CommittedState",
    "CompiledFidelity",
    "CompiledProperty",
    "DeclaredExpose",
    "DerivedStatistic",
    "Ed25519Signer",
    "ExposeSchema",
    "FidelityBlock",
    "FidelityEvaluation",
    "FidelityRecord",
    "GateAction",
    "GateCheck",
    "GateDecision",
    "NoProofSystem",
    "PackedEmit",
    "PropertyDeclaration",
    "PropertyKind",
    "PropertyOutcome",
    "Refusal",
    "SliceReference",
    "SyntheticReference",
    "UnsignedFallbackSigner",
    "build_record",
    "commit_state",
    "compile_fidelity",
    "compute_statistic",
    "expose_from_contract",
    "load_record",
    "parse_fidelity_block",
    "read_slice",
    "run_gate",
    "verify_record",
]

_HOME = {
    "BoundaryStore": "flux_engine.concordance.boundary",
    "ClearStatistic": "flux_engine.concordance.verifier",
    "ColumnCommitment": "flux_engine.concordance.boundary",
    "ColumnDeclaration": "flux_engine.concordance.predicate",
    "CommittedState": "flux_engine.concordance.boundary",
    "CompiledFidelity": "flux_engine.concordance.compiler",
    "CompiledProperty": "flux_engine.concordance.compiler",
    "DeclaredExpose": "flux_engine.concordance.predicate",
    "DerivedStatistic": "flux_engine.concordance.boundary",
    "Ed25519Signer": "flux_engine.concordance.record",
    "ExposeSchema": "flux_engine.concordance.predicate",
    "FidelityBlock": "flux_engine.concordance.predicate",
    "FidelityEvaluation": "flux_engine.concordance.verifier",
    "FidelityRecord": "flux_engine.concordance.record",
    "GateAction": "flux_engine.concordance.gate",
    "GateCheck": "flux_engine.concordance.gate",
    "GateDecision": "flux_engine.concordance.gate",
    "NoProofSystem": "flux_engine.concordance.verifier",
    "OWNED_CODES": "flux_engine.concordance.errors",
    "PackedEmit": "flux_engine.concordance.gate",
    "PropertyDeclaration": "flux_engine.concordance.predicate",
    "PropertyKind": "flux_engine.concordance.predicate",
    "PropertyOutcome": "flux_engine.concordance.verifier",
    "Refusal": "flux_engine.concordance.verifier",
    "SliceReference": "flux_engine.concordance.boundary",
    "SyntheticReference": "flux_engine.concordance.record",
    "UnsignedFallbackSigner": "flux_engine.concordance.record",
    "build_record": "flux_engine.concordance.record",
    "commit_state": "flux_engine.concordance.boundary",
    "compile_fidelity": "flux_engine.concordance.compiler",
    "compute_statistic": "flux_engine.concordance.boundary",
    "expose_from_contract": "flux_engine.concordance.predicate",
    "load_record": "flux_engine.concordance.record",
    "parse_fidelity_block": "flux_engine.concordance.predicate",
    "read_slice": "flux_engine.concordance.boundary",
    "run_gate": "flux_engine.concordance.gate",
    "verify_record": "flux_engine.concordance.record",
}


def __getattr__(name: str) -> Any:
    home = _HOME.get(name)
    if home is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(home), name)
    globals()[name] = value
    return value


def __dir__() -> List[str]:
    return sorted({*globals(), *__all__})
