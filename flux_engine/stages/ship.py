# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim ship``: the fidelity gate, then the evidence pack.

SEAM-FIDELITY-SPEC §7, on an already-applied run directory: for every emit
that declares a fidelity block, re-form the committed state of the
caller's ``--committed-slice`` through
:meth:`~flux_engine.concordance.boundary.store.BoundaryStore.commit` and
run :func:`flux_engine.concordance.gate.run_gate`.
Any withholding refuses the whole ship (fail-closed across the run, not
per emit -- a bundle that ships is a bundle every declared check passed):
no evidence pack is written, the process exits 1 with ``FLX-8001``, and
the withheld :class:`~flux_engine.concordance.gate.GateDecision` renders
as the CloudEvent SEAM-FIDELITY-SPEC §7 names
(``com.agentics.flux.ship.withheld``) -- NDJSON, one line per withheld
emit, carried in :attr:`~flux_engine.api.stages.StageResult.stdout` (the
"configured sink (stdout NDJSON by default)" the spec describes; nothing
here builds a second sink) so the CLI, not this function, decides when it
actually reaches a stream. A successful ship also writes ``EVIDENCE.md``
into the run directory before packing, so it travels inside the archive
under ``run/EVIDENCE.md``.

**A declared block with no ``--committed-slice`` is also a withholding.**
There is no committed-real-data pipeline in this build (see the REQUEST
in this module's build report) -- an owner has to supply the slice this
run's synthetic stream is checked against, and refusing silently to check
a check the emit's own contract requires would be exactly the kind of
badge this project's standing rules forbid claiming.

**An emit with no fidelity block ships unconditionally** -- there is
nothing SEAM-FIDELITY-SPEC asks it to satisfy.

**Two ships of one applied run over one slice write one record.** The
blinding value ``C2`` commits under is per-STATE (§4), so it is drawn once
when those records are first committed and kept in the boundary store
under ``<state_dir>/boundary/`` -- with the ``stateOrdinal`` and the
commitment time that were fixed with it. Ship the same run again over the
same committed slice under the same ``--signing-key`` and
``fidelity-record-<exposeId>.json`` comes out byte-identical, down to the
signature. Drawn per invocation, as it used to be, the same unchanged
records produced a different ``C2`` and a different signed record on every
call.

**The archive around it is not byte-identical, and does not claim to be.**
``EVIDENCE.md`` carries the instant it was generated, and it is staged
into the archive, so a second pack of one unchanged run has a different
archive digest and a different Merkle root. What is stable across two
ships is the run's own evidence -- the fidelity record, the recomputed
component digests and the ``runDigest`` the in-toto Statement names -- and
that is the identity
:func:`~flux_engine.evidence.pack._occupied_by_another_run` compares two
packs by, for exactly this reason. Compare two ships by ``runDigest`` and
by the record, never by the archive's digest alone.

**The artifacts are re-derived before anything is packed.** Every entry
of KERNEL-SPEC §10's ``components`` map is recomputed from the files in
the run directory (:func:`~flux_engine.stages.verify.recompute_components`)
and compared with ``run-digest.json``. A difference is ``FLX-2110`` and
the ship is refused: an archive whose in-toto Statement names a
``runDigest`` its own rows do not produce is worse than no archive, and
the emits with no fidelity block -- which the gate below has nothing to
say about -- had no other check standing between an edited stream and a
signed pack.

**Signing, and who it attributes the record to.** Pass ``--signing-key``
(a file holding a 32-byte ed25519 seed, raw or hex) and the record is
signed with the operator's own key. The trust anchor the gate checks
against is a separate argument (*trust_key*): supply it and the signature
check is a statement about the operator. Supply none and it is derived
from the signing key of this very call, which makes the check a statement
about internal consistency alone -- so the decision records that check as
``self-attested`` in those words, and ``EVIDENCE.md`` says the same under
"What Was Not Proven". With no signing key at all, ship falls back to a
per-invocation key generated in memory; ``keyId`` is
``ephemeral:<public-key prefix>``, and the record's own ``signer`` block
-- inside the signed bytes, so it cannot be swapped for somebody else's
key -- carries the public half and the ``ephemeral`` flag.

**The state's age is reported, not assumed.** ``budget.maxStateAge`` is
measured against a commitment time the owner declares (*committed_at*).
Without one, ship stamps the commitment itself and there is no
independent instant to measure against, so the gate records the age check
as not checked rather than as passed -- see
:func:`~flux_engine.concordance.gate.run_gate`.

**The record is written only once the gate says EMIT.** A withheld ship
leaves no signed record on disk to be mistaken for one that passed.
"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from flux_engine.api.stages import StageResult
from flux_engine.api.verdict import Reproducibility
from flux_engine.concordance.errors import FLX_8001
from flux_engine.concordance.gate import GateAction, GateCheck, GateDecision, run_gate
from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.evidence.pack import pack_evidence
from flux_engine.evidence.register import (
    BoundRegister,
    RegisterSummary,
    render_register_line,
    render_register_note,
)
from flux_engine.evidence.report import render_evidence_report
from flux_engine.safeio import (
    is_regular_file,
    read_text_nofollow,
    require_real_directory,
    write_text_atomic,
)
from flux_engine.seam.coverage import ExposeCoverage
from flux_engine.stages.apply import (
    load_recorded_plan,
    plan_kernel,
    require_run_digest_binds_plan,
)
from flux_engine.stages.errors import NoAppliedRun, RunArtifactsMoved
from flux_engine.stages.layout import RunLayout, atomic_write_text
from flux_engine.stages.plan import read_column_provenance, read_parameter_register
from flux_engine.stages.verify import (
    ProfileCheck,
    component_mismatches,
    packed_emit,
    require_known_slice_exposes,
    shape_report,
    trust_anchor_for,
)

if TYPE_CHECKING:
    from flux_engine.concordance.scorecard import Scorecard
    from flux_engine.seam.profile import PinnedProfile

__all__ = ["EmitOutcome", "ship"]


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(read_text_nofollow(path))


def _plain(value: Any) -> Any:
    """Recursively resolve a ``MappingProxyType``-bearing structure to plain JSON types."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if hasattr(value, "items") and not isinstance(value, (str, bytes)):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _bundle_contract_digest(directory: Path, product_ref: str) -> Optional[str]:
    """Digest the bundle's own ``.fluid.yml`` for *product_ref*, or ``None``.

    The one operand in the whole binding chain that does not come out of
    the run directory. ``plan.json``, ``fidelity-input-<id>.json`` and
    ``seam-attestation-<id>.json`` all live inside the run tree, so a
    consistently rewritten tree agreed with itself the entire way to a
    signed evidence pack; this reads the contract from the bundle the
    caller named on the command line, which is also the bundle about to be
    packed.

    Loaded with the seam's own exact loader -- the read every
    ``contractDigest`` in this engine is taken over (KERNEL-SPEC section 8)
    -- so the value compared here is the same quantity and not a second
    spelling of it.
    """
    from flux_engine.seam.resolve import load_documents_exact
    from flux_stats.exact_yaml import ReadBudget

    paths = sorted(directory.glob("*.fluid.yml")) + sorted(directory.glob("*.fluid.yaml"))
    budget = ReadBudget()  # one for every contract read, as a bundle's read has
    for path in paths:
        for document in load_documents_exact(path, budget=budget):
            if document.get("id") == product_ref:
                return digest_of_document(document, subject="contractDigest").value
    return None


def _binding_checks(
    directory: Path,
    compiled: Any,
    contract_document: Any,
    attestation: Dict[str, Any],
    planned: Dict[str, Any],
) -> List[GateCheck]:
    """Pin the compiled contract against the plan and the attestation, before a record exists.

    The gate's own ``binding.H`` compares the record's ``H`` to
    *compiled*'s -- and both used to come from the one object built out of
    the run directory's copy of the contract, so the check compared a
    value to itself and could not fail. These three comparisons are what
    make it mean something: the recompiled ``H`` and ``G`` are pinned
    against the values ``plan.json`` recorded for this emit (written by
    ``plan``, from the bundle, before this run existed), and the
    contract's own ``jcs-document`` digest against the one the seam
    attestation recorded for it. Anything the run directory's copy of the
    contract has picked up since is a difference against all three.
    """
    from flux_engine.core.digest import digest_of_document

    checks: List[GateCheck] = []
    planned_h = str(planned.get("H"))
    planned_g = str(planned.get("G"))
    h_matches = compiled.h.value == planned_h
    checks.append(
        GateCheck(
            "contract.plannedH",
            h_matches,
            (
                "the contract in the run directory recompiles to the seam digest the plan "
                "recorded"
                if h_matches
                else f"the contract in the run directory recompiles to {compiled.h.value}; "
                f"plan.json recorded {planned_h}"
            ),
        )
    )
    g_matches = compiled.g.value == planned_g
    checks.append(
        GateCheck(
            "contract.plannedG",
            g_matches,
            (
                "the reusable digest matches the plan's"
                if g_matches
                else f"the reusable digest recompiles to {compiled.g.value}; "
                f"plan.json recorded {planned_g}"
            ),
        )
    )
    declared_digest = str(attestation.get("reference", {}).get("contractDigest"))
    recomputed_digest = digest_of_document(contract_document, subject="contractDigest").value
    digest_matches = recomputed_digest == declared_digest
    checks.append(
        GateCheck(
            "contract.digest",
            digest_matches,
            (
                "the contract digests to the value the seam attestation recorded"
                if digest_matches
                else f"the contract digests to {recomputed_digest}; the seam attestation "
                f"records {declared_digest}"
            ),
        )
    )
    # The one comparison whose operands do not both come out of the run
    # directory: the contract as it stands in the bundle being packed,
    # against the digest the seam attestation recorded for it.
    product_ref = str(attestation.get("reference", {}).get("productRef", ""))
    in_bundle = _bundle_contract_digest(directory, product_ref)
    checks.append(
        GateCheck(
            "contract.inBundle",
            in_bundle == declared_digest,
            (
                "the bundle's own contract digests to the value the seam attestation recorded"
                if in_bundle == declared_digest
                else (
                    f"the bundle declares no FLUID document {product_ref!r}"
                    if in_bundle is None
                    else f"the bundle's contract digests to {in_bundle}; the seam "
                    f"attestation records {declared_digest}"
                )
            ),
        )
    )
    return checks


@dataclass(frozen=True)
class EmitOutcome:
    """One emit's gate decision, and the record it earned if it passed.

    The record is carried rather than written because a ship is
    fail-closed across the whole run: whether *this* emit's record belongs
    on disk is not known until every other emit has been decided too.
    """

    decision: GateDecision
    #: The signed fidelity record, or ``None`` when the gate withheld.
    record: Optional[Dict[str, Any]] = None
    #: The emit compared with the run's pinned profile, for the scorecard;
    #: ``None`` when the run's plan pins no profile.
    scored: Optional[ProfileCheck] = None


def _synthetic_reference(reference_cls: Any, expose_id: str, attestation: Dict[str, Any]) -> Any:
    """Build the record's ``synthetic`` block, refusing an absent generator digest.

    ``generatorDigest`` is what makes a moved generator move the record
    (:mod:`flux_engine.generator.rows`). An absent one used to be replaced
    by ``sha256:`` + 64 zeros -- a value of exactly the right shape, in
    the one document this engine signs, indistinguishable to any consumer
    from a digest something was actually taken over. An unknown digest is
    an absent statement, not a placeholder: the ship is withheld instead
    (``FLX-8001``).
    """
    synthetic = attestation.get("synthetic", {})
    generator_digest = synthetic.get("generatorDigest")
    if not generator_digest:
        raise FluxError(
            FLX_8001,
            f"the seam attestation for {expose_id!r} records no generatorDigest, so the "
            "fidelity record would name a generator nothing was digested from",
        )
    return reference_cls(
        output_digest=synthetic["outputDigest"],
        rows=synthetic["rows"],
        generator_digest=generator_digest,
    )


def _decide_emit(
    layout: RunLayout,
    bundle_directory: Path,
    expose_id: str,
    receipt_entry: Dict[str, Any],
    slice_uri: Optional[str],
    planned_emit: Dict[str, Any],
    *,
    signing_key: Optional[bytes] = None,
    trust_key: Optional[bytes] = None,
    committed_at: Optional[str] = None,
    now_ms: Optional[int] = None,
    pinned: Optional["PinnedProfile"] = None,
) -> EmitOutcome:
    """Decide one emit: its committed slice's gate, its pinned profile's, or both.

    With a committed slice the gate of SEAM-FIDELITY-SPEC §7 runs as it
    always has and earns a signed record. With the run's pinned profile
    (*pinned*), the emit's properties are compared with the profile too
    (PROFILE-SPEC.md §7), and an evaluation that is not within withholds it;
    that comparison writes no record, and its outcomes reach the scorecard.
    With neither, a declared block has nothing to be checked against and is
    withheld.
    """
    if not receipt_entry.get("declaresFidelity"):
        return EmitOutcome(
            decision=GateDecision(action=GateAction.EMIT, expose_id=expose_id, checks=()),
            scored=ProfileCheck(expose_id=expose_id) if pinned is not None else None,
        )

    from flux_engine.concordance.compiler import compile_fidelity
    from flux_engine.concordance.predicate import expose_from_contract
    from flux_engine.stages._scorecard import min_credibility

    if slice_uri is None and pinned is None:
        return EmitOutcome(
            decision=GateDecision(
                action=GateAction.WITHHOLD,
                expose_id=expose_id,
                checks=(_no_slice_check(expose_id),),
                code=FLX_8001.code,
            )
        )

    try:
        fidelity_input = _read_json(layout.fidelity_input_path(expose_id))
        attestation = _read_json(layout.seam_attestation_path(expose_id))
        contract_document = fidelity_input["contract"]
        emit_entry = fidelity_input["emit"]
        expose = expose_from_contract(contract_document, expose_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        # The run's own record of what this emit declared, unreadable: the
        # emit is withheld with a check that names the file's kind of fault,
        # never quotes it, rather than failing the whole stage with a raw error.
        return EmitOutcome(
            decision=GateDecision(
                action=GateAction.WITHHOLD,
                expose_id=expose_id,
                checks=(
                    GateCheck(
                        "fidelity-input.readable",
                        False,
                        f"{layout.fidelity_input_path(expose_id).name} or "
                        f"{layout.seam_attestation_path(expose_id).name} cannot be read "
                        f"({type(error).__name__})",
                    ),
                ),
                code=FLX_8001.code,
            ),
            scored=(
                ProfileCheck(
                    expose_id=expose_id,
                    evaluation=_unreadable(expose_id, type(error).__name__),
                )
                if pinned is not None
                else None
            ),
        )
    compiled = compile_fidelity(contract=contract_document, emit=emit_entry, expose=expose)
    scored = ProfileCheck(expose_id=expose_id, compiled=compiled) if pinned is not None else None

    binding = _binding_checks(
        bundle_directory, compiled, contract_document, attestation, planned_emit
    )
    if not all(check.passed for check in binding):
        # Refused here, before a record is built: a record signed over a
        # contract the plan never saw is exactly the document the H
        # binding exists to make impossible, and writing one and then
        # failing it downstream would leave that document on disk.
        return EmitOutcome(
            decision=GateDecision(
                action=GateAction.WITHHOLD,
                expose_id=expose_id,
                checks=tuple(binding),
                code=FLX_8001.code,
            ),
            scored=_not_compared(scored, "the emit's binding to the plan did not hold"),
        )
    if scored is not None:
        # Read once the binding holds: H covers the emit entry, so this is the
        # minCredibility the run was planned under.
        scored = replace(scored, min_credibility=min_credibility(fidelity_input))

    if slice_uri is None:
        assert pinned is not None and scored is not None
        return _decide_against_profile(layout, attestation, binding, pinned, scored)

    return _decide_against_slice(
        layout,
        expose_id,
        attestation,
        binding,
        compiled,
        slice_uri,
        pinned=pinned,
        scored=scored,
        signing_key=signing_key,
        trust_key=trust_key,
        committed_at=committed_at,
        now_ms=now_ms,
    )


def _decide_against_slice(
    layout: RunLayout,
    expose_id: str,
    attestation: Dict[str, Any],
    binding: Sequence[GateCheck],
    compiled: Any,
    slice_uri: str,
    *,
    pinned: Optional["PinnedProfile"],
    scored: Optional[ProfileCheck],
    signing_key: Optional[bytes],
    trust_key: Optional[bytes],
    committed_at: Optional[str],
    now_ms: Optional[int],
) -> EmitOutcome:
    """An emit with a committed slice: SEAM-FIDELITY-SPEC §7's gate, which earns a signed record.

    With the run's pinned profile (*pinned*) the same rows are compared with
    it as well, and the emit ships only when both references find it within.
    """
    from flux_engine.concordance.boundary.slice import read_slice
    from flux_engine.concordance.boundary.store import BoundaryStore
    from flux_engine.concordance.record import Ed25519Signer, SyntheticReference, build_record
    from flux_engine.concordance.verifier import ClearStatistic
    from flux_engine.stages._scorecard import evaluate_against_profile

    rows, reference = read_slice(slice_uri, maximum=compiled.block.records.maximum)
    # The blinding value, the stateOrdinal and the commitment time all
    # belong to the committed state, not to this invocation: the store
    # draws them once, the first time these records are committed, keeps
    # them under `<state_dir>/boundary/` (never the run directory, which
    # is staged into the evidence pack) and hands the same ones back to
    # every later check of the same records. Drawn per call, as they used
    # to be, two ships of one applied run under one signing key produced
    # two different C2 values, two different fidelity records and two
    # archives with different Merkle roots over a state nobody had
    # touched.
    store = BoundaryStore.for_state_dir(
        layout.state_dir, max_checks_per_state=compiled.block.budget.max_checks_per_state
    )
    # *committed_at* is the owner's own statement about when the slice was
    # fixed. Absent one, this process stamps a state it is forming for the
    # first time -- and then the age the gate would measure is the
    # distance from this instant to this instant, which is why `now_ms` is
    # withheld too and the age check reports itself unchecked rather than
    # green.
    state = store.commit(
        rows=rows,
        compiled=compiled,
        slice_reference=reference,
        committed_at=committed_at,
        stamped_at=datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    )

    emit_path = layout.emit_path(expose_id)
    # Through the catalogued guards, never a bare `json.loads` and a bare
    # `zip(..., strict=True)`: a malformed line, or a stream whose rows do
    # not match the header's column count, is FLX-8001 naming the line and
    # both widths -- not a ValueError with `"code": null` two stages after
    # the leg that should have caught it.
    try:
        packed = packed_emit(attestation, expose_id, _emit_rows(emit_path))
        synthetic_rows = packed.as_mappings()
    except FluxError as error:
        return EmitOutcome(
            decision=GateDecision(
                action=GateAction.WITHHOLD,
                expose_id=expose_id,
                checks=(GateCheck("synthetic.rows", False, str(error)),),
                code=FLX_8001.code,
            ),
            scored=_not_compared(scored, str(error)),
        )
    evaluation = ClearStatistic(
        compiled=compiled, committed=state, synthetic_rows=synthetic_rows
    ).evaluate()

    check_ordinal = store.record_check(state.ordinal, g=compiled.g.value)

    key_material = signing_key if signing_key is not None else _ephemeral_key()
    ephemeral = signing_key is None
    # The public key is derived from the key material before the key id is
    # chosen, so an ephemeral key names itself as one in the record.
    signing_public_key = Ed25519Signer(private_key=key_material, key_id="_").public_key
    signer = Ed25519Signer(
        private_key=key_material,
        key_id=(
            f"ephemeral:{signing_public_key.hex()[:16]}" if ephemeral else f"flux-ship-{expose_id}"
        ),
    )
    # The trust anchor comes from the caller when it can. Falling back to
    # the public half of the key this call is about to sign with makes the
    # gate's signature check a statement about internal consistency and
    # nothing else -- so that fallback is flagged, and the check says
    # "self-attested" in those words rather than reading as attribution.
    # One derivation, shared with `verify`'s own trust anchor
    # (`flux_engine.stages.verify.trust_anchor_for`): `fluxsim ship
    # --signing-key k` and `fluxsim verify --signing-key k` must reach the
    # same verdict about one record, and two copies of this two-line rule
    # are how they would stop doing so.
    trust_anchor, self_attested = trust_anchor_for(key_material, trust_key)
    reference = attestation.get("reference", {})
    record = build_record(
        compiled=compiled,
        committed=state,
        evaluation=evaluation,
        synthetic=_synthetic_reference(SyntheticReference, expose_id, attestation),
        # SEAM-FIDELITY-SPEC §6's `reference` block names four distinct
        # things, and three of them used to be something else: the
        # productRef was the exposeId, the fluidVersion was the wildcard
        # default of a key nothing ever wrote, and the contractDigest was
        # a second copy of `H`. All three are recorded, resolved, in the
        # seam attestation this function already has open -- productRef is
        # the FLUID data product the record answers for, fluidVersion is
        # the pin `resolve_emit` enforced, and contractDigest is
        # KERNEL-SPEC §9's digest of the referenced `.fluid.yml`, which is
        # a different quantity from `H` and now appears in the record
        # beside it rather than instead of it.
        product_ref=str(reference.get("productRef", compiled.block.expose_id)),
        fluid_version=str(reference.get("fluidVersion", "*")),
        contract_digest=str(reference.get("contractDigest", "")),
        check_ordinal=check_ordinal,
        signer=signer,
        ephemeral_key=ephemeral,
    )
    # The signer identity is inside the signed body (`signer`), so nothing
    # is bolted onto the excluded `signature` object afterwards: a key
    # written there travels with the record without being covered by it,
    # which is what let a forged record carry its own forger's key.
    record_document = _plain(record.document)

    decision = run_gate(
        compiled=compiled,
        record_document=record_document,
        committed=state,
        packed=packed,
        now_ms=now_ms,
        trusted_keys=trust_anchor,
        self_attested=self_attested,
    )
    decision = GateDecision(
        action=decision.action,
        expose_id=decision.expose_id,
        checks=tuple([*binding, *decision.checks]),
        evaluation=decision.evaluation,
        code=decision.code,
    )
    if pinned is not None and scored is not None:
        # The run pins a profile as well: the same rows are compared with it,
        # and an emit is shipped only when both references agree it is within.
        scored = replace(
            scored, evaluation=evaluate_against_profile(compiled, pinned, synthetic_rows)
        )
        decision = _with_check(decision, _profile_check(scored.evaluation))
    accepted = decision.action is GateAction.EMIT
    # Handed back, not written: a ship is fail-closed across the whole run
    # (see this module's docstring), so an emit the gate accepted must not
    # leave a signed record on disk while a *later* emit is still
    # undecided. The write phase runs once, after every emit has been
    # decided and none was withheld.
    return EmitOutcome(
        decision=decision, record=record_document if accepted else None, scored=scored
    )


def _decide_against_profile(
    layout: RunLayout,
    attestation: Dict[str, Any],
    binding: Sequence[GateCheck],
    pinned: "PinnedProfile",
    scored: ProfileCheck,
) -> EmitOutcome:
    """An emit with no committed slice, gated on its comparison with the pinned profile.

    No record is signed: the record of SEAM-FIDELITY-SPEC §6 answers for a
    committed state, and a profile is not one. A stream that cannot be read
    withholds the emit, and its table is refused on the scorecard.
    """
    from flux_engine.stages._scorecard import evaluate_against_profile

    expose_id = scored.expose_id
    assert scored.compiled is not None
    try:
        packed = packed_emit(attestation, expose_id, _emit_rows(layout.emit_path(expose_id)))
        synthetic_rows = packed.as_mappings()
    except FluxError as error:
        return EmitOutcome(
            decision=GateDecision(
                action=GateAction.WITHHOLD,
                expose_id=expose_id,
                checks=(*binding, GateCheck("synthetic.rows", False, str(error))),
                code=FLX_8001.code,
            ),
            scored=_not_compared(scored, str(error)),
        )
    evaluation = evaluate_against_profile(scored.compiled, pinned, synthetic_rows)
    decision = GateDecision(
        action=GateAction.EMIT, expose_id=expose_id, checks=tuple(binding), evaluation=evaluation
    )
    return EmitOutcome(
        decision=_with_check(decision, _profile_check(evaluation)),
        scored=replace(scored, evaluation=evaluation),
    )


def _with_check(decision: GateDecision, *checks: GateCheck) -> GateDecision:
    """*decision* with *checks* added after its own; a failed one withholds (``FLX-8001``)."""
    passed = all(check.passed for check in checks)
    return GateDecision(
        action=decision.action if passed else GateAction.WITHHOLD,
        expose_id=decision.expose_id,
        checks=(*decision.checks, *checks),
        evaluation=decision.evaluation,
        code=decision.code if passed else FLX_8001.code,
    )


def _profile_check(evaluation: Any) -> GateCheck:
    """The gate check a comparison with the pinned profile adds to an emit's decision."""
    from flux_engine.stages._scorecard import profile_check_detail

    return GateCheck(
        name="profile.properties",
        passed=evaluation.within,
        detail=profile_check_detail(evaluation, name_failures=True),
    )


def _unreadable(expose_id: str, kind: str) -> Any:
    """The refused evaluation of an emit whose recorded fidelity input cannot be read."""
    from flux_engine.stages._scorecard import refused_evaluation

    return refused_evaluation(
        expose_id, FLX_8001.code, f"the run's fidelity input cannot be read ({kind})"
    )


def _not_compared(scored: Optional[ProfileCheck], reason: str) -> Optional[ProfileCheck]:
    """*scored* with an evaluation that refused before any property was compared.

    The scorecard then lists the emit's table as refused, and why, rather
    than as a table with nothing in it.
    """
    if scored is None:
        return None
    from flux_engine.stages._scorecard import refused_evaluation

    return replace(
        scored,
        evaluation=refused_evaluation(scored.expose_id, FLX_8001.code, f"not compared: {reason}"),
    )


def _emit_rows(emit_path: Path) -> List[List[Any]]:
    """Decode an emit stream's rows, refusing (``FLX-8001``) a line that is not JSON."""
    rows: List[List[Any]] = []
    for index, line in enumerate(read_text_nofollow(emit_path).splitlines()):
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise FluxError(
                FLX_8001,
                f"{emit_path.name}: line {index + 1} is not JSON ({error.msg})",
            ) from None
    return rows


def _no_slice_check(expose_id: str) -> Any:
    return GateCheck(
        name="committed-slice.present",
        passed=False,
        detail=f"{expose_id} declares a fidelity block; ship needs --committed-slice for it",
    )


def _ephemeral_key() -> bytes:
    import os

    return os.urandom(32)


def _now_ms() -> int:
    """The host clock, in epoch milliseconds -- the instant a state's age is measured at."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


@dataclass(frozen=True)
class _ScoredRun:
    """What ``EVIDENCE.md`` says of a run scored against its profile."""

    scorecard: "Scorecard"
    #: The emits checked against the profile rather than a committed slice.
    profiled: FrozenSet[str]
    #: Whether this ship's slice gate signed and wrote any emit's record.
    records_written: bool
    #: The profile-checked emits whose run directory holds a fidelity record
    #: this ship did not write: an earlier ship's, or a file put there by
    #: hand. This ship never reads it, so nothing here attributes it.
    earlier_records: FrozenSet[str] = frozenset()


def _scored_run(
    layout: RunLayout,
    scorecard: Optional["Scorecard"],
    outcomes: Mapping[str, EmitOutcome],
    slices: Collection[str],
) -> Optional[_ScoredRun]:
    if scorecard is None:
        return None
    profiled = frozenset(
        expose_id
        for expose_id, outcome in outcomes.items()
        if outcome.scored is not None and expose_id not in slices
    )
    earlier = frozenset(
        expose_id
        for expose_id in profiled
        if is_regular_file(layout.fidelity_record_path(expose_id))
    )
    return _ScoredRun(
        scorecard=scorecard,
        profiled=profiled,
        records_written=any(outcome.record is not None for outcome in outcomes.values()),
        earlier_records=earlier,
    )


def _score_and_gate(
    plan_document: Mapping[str, Any],
    run_id: str,
    run_digest_document: Mapping[str, Any],
    pinned: Optional["PinnedProfile"],
    outcomes: Mapping[str, EmitOutcome],
) -> Tuple[Optional["Scorecard"], Dict[str, GateDecision]]:
    """The run's scorecard and the decisions it gates, or no scorecard for a run that pins none.

    The scorecard is formed from every emit's comparison with the pinned
    profile, then its missed thresholds and floors withhold the emits they
    are about (:func:`_gate_on_scorecard`).
    """
    decisions = {expose_id: outcome.decision for expose_id, outcome in outcomes.items()}
    if pinned is None:
        return None, decisions
    from flux_engine.stages._scorecard import build_run_scorecard

    scorecard = build_run_scorecard(
        plan_document=plan_document,
        run_id=run_id,
        run_digest=digest_of_document(run_digest_document, subject="runDigest").value,
        pinned=pinned,
        tables=[
            (outcome.scored or ProfileCheck(expose_id=expose_id)).table_input()
            for expose_id, outcome in sorted(outcomes.items())
        ],
    )
    return scorecard, _gate_on_scorecard(decisions, scorecard)


def _refuse_unformed_scorecard(layout: RunLayout) -> None:
    """Refuse a ``scorecard.json`` in the run directory of a run that pins no profile.

    The pack copies the run directory whole, and ``run/scorecard.json`` reads
    as the gate's scorecard. A run whose plan pins no profile has none, so a
    file of that name was put there by something other than this stage, and
    is refused (``FLX-8001``) rather than shipped as if it had been decided on.
    """
    from flux_engine.concordance.scorecard import SCORECARD_FILE

    stray = layout.run_dir / SCORECARD_FILE
    if stray.exists() or stray.is_symlink():
        raise FluxError(
            FLX_8001,
            f"nothing packed -- {stray} is in the run directory, but this run's plan pins no "
            "profile, so no scorecard was formed for it; remove the file",
        )


def _register_files(layout: RunLayout, register: Optional[BoundRegister]) -> Dict[str, bytes]:
    """The register's checked bytes, to pack under its own name; nothing for a run with none."""
    return {layout.parameter_register_path.name: register.data} if register is not None else {}


def _register_artifact(summary: Optional[RegisterSummary]) -> Dict[str, Any]:
    """The ``parameterRegister`` artifact key, or nothing for a run whose plan binds no register."""
    return {"parameterRegister": summary.as_document()} if summary is not None else {}


def _register_line(summary: Optional[RegisterSummary]) -> str:
    """The shares line of ``ship``'s summary, or nothing for a run whose plan binds no register."""
    return render_register_line(summary) if summary is not None else ""


def _scorecard_artifact(scorecard: Optional["Scorecard"]) -> Dict[str, Any]:
    """The ``scorecard`` artifact key, or nothing for a run that pins no profile."""
    return {"scorecard": scorecard.to_document()} if scorecard is not None else {}


def _write_scorecard(layout: RunLayout, scorecard: Optional["Scorecard"]) -> Dict[str, bytes]:
    """Write ``scorecard.json`` beside the records; return the bytes to pack.

    The pack takes the bytes computed here rather than the file read back
    off the disk (``verified_run_files``), so it holds exactly the scorecard
    this gate decided on. A run that pins no profile writes nothing.
    """
    if scorecard is None:
        return {}
    from flux_engine.concordance.scorecard import SCORECARD_FILE

    scorecard_bytes = scorecard.to_bytes()
    atomic_write_text(layout.run_dir / SCORECARD_FILE, scorecard_bytes.decode("utf-8"))
    return {SCORECARD_FILE: scorecard_bytes}


def _gate_on_scorecard(
    decisions: Dict[str, GateDecision], scorecard: "Scorecard"
) -> Dict[str, GateDecision]:
    """Withhold every emit a missed threshold or ``minCredibility`` is about.

    A missed table threshold, or a table whose measured score is below its
    emit's ``minCredibility``, withholds that emit. A missed dimension or
    overall threshold withholds every emit whose properties feed the score
    (:meth:`~flux_engine.concordance.scorecard.Scorecard.contributors`). Each
    gets a failed check saying what was missed and where to look first.
    """
    extra: Dict[str, List[GateCheck]] = {}
    for result in scorecard.thresholds:
        if result.met:
            continue
        detail = scorecard.threshold_line(result)
        for expose_id in scorecard.contributors(result.threshold):
            extra.setdefault(expose_id, []).append(GateCheck("scorecard.threshold", False, detail))
    for credibility in scorecard.credibility:
        if credibility.met is False:
            extra.setdefault(credibility.expose_id, []).append(
                GateCheck("scorecard.credibility", False, credibility.line())
            )
    gated = dict(decisions)
    for expose_id, checks in extra.items():
        # A score about an emit this run did not decide cannot be traced to
        # one, so the miss withholds every emit rather than none.
        for target in [expose_id] if expose_id in gated else sorted(gated):
            gated[target] = _with_check(gated[target], *checks)
    return gated


def _withheld_summary(
    withheld: Dict[str, GateDecision], scorecard: Optional["Scorecard"] = None
) -> str:
    """What a person reading a terminal is told when a ship is withheld.

    The refusal used to arrive as one long CloudEvent on stdout with a
    single line on stderr naming the expose -- so the failing property,
    the check that refused and what it found existed only inside a JSON
    blob, and the most consequential thing this engine does read as a wall
    of machine format. The events still travel, as data, in ``artifacts``.
    """
    lines: List[str] = []
    for expose_id in sorted(withheld):
        decision = withheld[expose_id]
        lines.append(f"withheld: {expose_id}")
        for check in decision.failures:
            lines.append(f"  - {check.name}: {check.detail}")
        evaluation = decision.evaluation
        if evaluation is not None:
            for outcome in evaluation.outcomes:
                # A censored property was not scored: the profile withheld
                # what it would be compared with. It is no failure, and it
                # is not listed as one.
                if outcome.outcome not in ("within", "censored"):
                    # "property x: outside" named neither the statistic
                    # nor the columns nor the margin it landed outside of,
                    # so the reader had to open the bundle's fidelity
                    # block to learn what had just been refused.
                    columns = ", ".join(outcome.columns)
                    declared = ", ".join(f"{k}={v}" for k, v in sorted(outcome.tolerance.items()))
                    lines.append(
                        f"  - property {outcome.property_id} ({outcome.kind.value} of "
                        f"{columns}): {outcome.outcome} the declared margin "
                        f"{outcome.margin} (declared {declared})"
                    )
            for refusal in evaluation.refusals:
                lines.append(f"  - refusal: {refusal.to_document()}")
    if scorecard is not None:
        lines.extend(scorecard.summary_lines())
    lines.append("nothing was written to the output directory")
    return "\n".join(lines)


#: Said on an emit's ``EVIDENCE.md`` line when the run directory holds a
#: fidelity record for it that this ship did not write: an earlier ship's, or
#: a file put there by hand. This ship never reads it.
_UNWRITTEN_RECORD = (
    "; the run directory also holds a fidelity record that this ship did "
    "not write, read or check, and this report attributes it to no key"
)


def _fidelity_summary_text(
    decisions: Dict[str, GateDecision],
    profiled: Collection[str] = frozenset(),
    earlier_records: Collection[str] = frozenset(),
) -> str:
    """One line per emit, for ``EVIDENCE.md``'s Fidelity section.

    Every decision reaching this point already passed the gate (a
    withholding returns before this is called) -- so this reports what
    each emit's checks found, never a pass/fail this stage has not
    already acted on. *profiled* names the emits checked against the
    pinned profile rather than a committed slice; a property the profile
    withheld was not scored, and the line says how many were.
    *earlier_records* names the emits whose run directory holds a fidelity
    record this ship did not write, and each one's line says so, whether or
    not the emit declares a fidelity block.
    """
    if not decisions:
        return "This run declared no exposes."
    return "\n".join(
        _fidelity_line(
            expose_id,
            decisions[expose_id],
            profiled=expose_id in profiled,
            unwritten=_UNWRITTEN_RECORD if expose_id in earlier_records else "",
        )
        for expose_id in sorted(decisions)
    )


def _fidelity_line(
    expose_id: str, decision: GateDecision, *, profiled: bool, unwritten: str
) -> str:
    """One emit's line under ``EVIDENCE.md``'s Fidelity section (:func:`_fidelity_summary_text`)."""
    if decision.evaluation is None:
        return f"- `{expose_id}`: no fidelity block declared{unwritten}"
    count = len(decision.evaluation.outcomes)
    properties = "property" if count == 1 else "properties"
    if not profiled:
        return f"- `{expose_id}`: fidelity checked, {count} declared {properties} within tolerance"
    censored = len(decision.evaluation.censored)
    return (
        f"- `{expose_id}`: fidelity checked against the pinned profile, "
        f"{count - censored} of {count} declared {properties} within tolerance"
        + (f", {censored} censored and not scored" if censored else "")
        + unwritten
    )


def _scorecard_summary_text(scorecard: "Scorecard") -> str:
    """The scorecard's paragraph under ``EVIDENCE.md``'s Fidelity section."""
    from flux_engine.concordance.scorecard import SCORECARD_FILE, display

    lines = [
        f"\n\nScorecard (`run/{SCORECARD_FILE}`), against the pinned profile "
        f"`{scorecard.profile_digest}`:",
        "",
        f"- overall: {display(scorecard.overall.score)}",
    ]
    lines.extend(f"- {line}" for line in scorecard.detail_lines())
    for result in scorecard.credibility:
        if result.basis == "measured":
            lines.append(
                f"- `{result.expose_id}`: the measured score replaces the Playback's declared "
                "credibility as the basis of minCredibility"
            )
    return "\n".join(lines)


def _scorecard_not_proven(scorecard: "Scorecard") -> str:
    """What the scorecard does not show, for ``EVIDENCE.md``'s last section."""
    unmeasured = [name for name, aggregate in scorecard.dimensions if aggregate.score is None]
    text = (
        "The scorecard compares this run with the pinned profile's published aggregates, "
        "under the properties this bundle declares, and with nothing else. A property the "
        "profile withheld is censored: it was not scored."
    )
    if unmeasured:
        text += " These dimensions were not measured at all: " + ", ".join(unmeasured) + "."
    text += (
        " A score says how close the run is to the profile, not that the profile "
        "describes its source accurately."
    )
    if scorecard.shape is not None:
        text += (
            " The shape flags test every profiled column at a level of 1/1000 for the "
            "whole run: a column with no flag was not shown to match the profile, a check "
            "the profile gave nothing to read was not run, and no flag withheld anything."
        )
    return text


def _signature_attribution(
    ephemeral_signing: bool, self_attested: bool, *, subject: str = "this run's fidelity records"
) -> str:
    """What ``EVIDENCE.md`` says about who the record's signature attributes it to.

    Three honest answers, and the pack gets whichever one is true. The
    middle one is the case that used to go unsaid: a real operator key
    signed the record, but the only key the gate checked it against was
    derived from that same key inside this process, so the check was
    about the bytes and not about the operator.

    *subject* names the records the sentence is about. A run that pins a
    profile can hold a record this ship never wrote, so its sentence is
    about the records this ship wrote and no others.
    """
    if ephemeral_signing:
        return (
            f"\n- Signature: {subject} were signed with a key generated "
            "in memory for this invocation and checked against that same key. It "
            "attributes the records to no operator; pass `--signing-key` to sign with one "
            "that does, and `--trust-key` with the public half the gate should check "
            "against."
        )
    if self_attested:
        return (
            f"\n- Signature: {subject} were signed with a supplied key "
            "and checked against the public half of that same key -- self-attested. It "
            "shows the record has not been altered since this process signed it; it is "
            "not an independent statement about who signed. Pass `--trust-key` with the "
            "key a reader is expected to trust to make it one."
        )
    return (
        f"\n- Signature: {subject} were signed with a supplied key and "
        "checked against a trust anchor supplied separately."
    )


def _signature_text(
    ephemeral_signing: bool, self_attested: bool, scored: Optional["_ScoredRun"]
) -> str:
    """``EVIDENCE.md``'s Signature sentence, or nothing when this ship signed no record.

    A run that pins no profile keeps the sentence it always had. A run scored
    against its profile attributes only the records this ship signed: one
    scored against the profile alone signed none, and a record an earlier
    ship left is named, unattributed, on its emit's line.
    """
    if scored is None:
        return _signature_attribution(ephemeral_signing, self_attested)
    if not scored.records_written:
        return ""
    return _signature_attribution(
        ephemeral_signing, self_attested, subject="the fidelity records this ship wrote"
    )


def _write_evidence_report(
    layout: RunLayout,
    *,
    bundle_name: str,
    kernel_version: str,
    run_digest: DigestRef,
    generated_at: str,
    decisions: Dict[str, GateDecision],
    ephemeral_signing: bool,
    self_attested: bool,
    coverage: Optional[Sequence[ExposeCoverage]] = None,
    scored: Optional["_ScoredRun"] = None,
    register: Optional[RegisterSummary] = None,
) -> None:
    """Write ``EVIDENCE.md`` into the run directory, ahead of packing.

    Written under the run directory (never the bundle directory, which
    :func:`~flux_engine.evidence.pack.pack_evidence` only ever reads) so it
    is staged under ``run/`` and lands inside the packed archive alongside
    every other run artifact -- the mandatory "What Was Not Proven"
    section travels with the bundle it describes, not beside it.

    *coverage* is :func:`~flux_engine.stages.plan.read_column_provenance`'s
    answer: how each column was produced, read back through the plan's digest,
    or ``None`` when the run's plan binds no record.

    *register* is :func:`~flux_engine.stages.plan.read_parameter_register`'s
    summary: the sources of the run's numbers, read back through the plan's
    digest, or ``None`` when the run's plan binds no register, which leaves the
    report as it was.
    """
    report = render_evidence_report(
        bundle_name=bundle_name,
        kernel_version=kernel_version,
        run_digest=run_digest,
        generated_at=generated_at,
        conformance_summary=(
            "This run's `apply` stage wrote every declared emit stream and its "
            "receipt without a conformance refusal; see `fluxsim verify`'s "
            "row-conformance and RFC-04 legs for an independent re-check."
        ),
        # `ship` gates fidelity but never re-checks the golden baseline
        # (that is `verify`'s leg, independent per SEAM-FIDELITY-SPEC) --
        # NOT_CHECKED is the honest answer for a claim this stage never
        # made.
        reproducibility=Reproducibility.NOT_CHECKED,
        fidelity_summary=(
            _fidelity_summary_text(
                decisions,
                scored.profiled if scored else frozenset(),
                scored.earlier_records if scored else frozenset(),
            )
            + _signature_text(ephemeral_signing, self_attested, scored)
            + (_scorecard_summary_text(scored.scorecard) if scored else "")
        ),
        coverage=coverage,
        scorecard_note=_scorecard_not_proven(scored.scorecard) if scored else None,
        register_note=render_register_note(register) if register is not None else None,
    )
    write_text_atomic(layout.run_dir / "EVIDENCE.md", report)


def ship(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    out_dir: Union[str, Path],
    state_dir: Union[str, Path, None] = None,
    committed_slice: Optional[Dict[str, str]] = None,
    signing_key: Optional[bytes] = None,
    trust_key: Optional[bytes] = None,
    committed_at: Optional[str] = None,
    profile: Union[str, Path, None] = None,
) -> StageResult:
    """Gate, then pack. Writes nothing to *out_dir* when the gate withholds.

    *signing_key* is a 32-byte ed25519 seed. When omitted, a
    per-invocation key is generated and the record says so (see this
    module's docstring).

    *trust_key* is the 32-byte raw ed25519 **public** key the gate checks
    the record's signature against. When omitted it falls back to the
    public half of *signing_key*, and every decision and the evidence
    report say the signature is self-attested rather than implying an
    attribution the check does not make.

    *committed_at* is the owner's own RFC 3339 statement of when the
    committed slice was fixed. Given one, ``budget.maxStateAge`` is
    measured against the host clock and can refuse; without one, ship
    stamps the commitment itself and the age check reports that it was not
    checked.

    *profile* is the profile document the run's plan pins
    (docs/spec/PROFILE-SPEC.md), paired with the pin as ``plan`` pairs it: a
    pinned run handed no profile, a profile for a run that pins none, and
    another profile are refused before anything is written (``FLX-3109``,
    ``FLX-3110``). Every emit's properties are compared with it, the run is
    scored (docs/spec/SCORECARD-SPEC.md), and a missed threshold or a measured
    score below an emit's ``minCredibility`` withholds. The scorecard travels
    in the pack as ``run/scorecard.json``.
    """
    directory = Path(bundle_directory)
    # Resolved before the last component is taken: `Path(".").name` is empty,
    # which is how `fluxsim run . --ship` printed "Evidence: " and "Bundle: ``"
    # at the top of the first evidence report a newcomer ever opens.
    bundle_name = directory.resolve().name
    state = Path(state_dir) if state_dir is not None else Path(".flux")
    layout = RunLayout.for_state_dir(state, run_id).require_real_dirs()
    # Before the first file is written into the run directory, not at the pack: a
    # linked output directory (FLX-2118) must leave the run exactly as it was.
    require_real_directory(Path(out_dir))
    slices = committed_slice or {}

    # Both artifacts, not just the receipt: this stage re-derives every
    # component of `run-digest.json` before it packs anything, so a run
    # directory without one has nothing for it to check against, and
    # "there is no applied run here" is the honest answer rather than a
    # `FileNotFoundError` from three functions further down.
    if not is_regular_file(layout.receipt_path) or not is_regular_file(layout.run_digest_path):
        missing = NoAppliedRun(run_dir=layout.run_dir)
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={"code": missing.code.code},
            stderr=str(missing),
            error_code=missing.code.code,
        )
    try:
        receipt = _read_json(layout.receipt_path)
    except ValueError:
        # `receipt.json` is the run directory's commit marker, and `apply`
        # now writes it through one `os.replace` -- so a receipt that is
        # present but does not parse was written by an older build, or by
        # a stage killed before the atomic write landed. Either way this
        # directory holds no completed run, which is the catalogued answer
        # rather than a `JSONDecodeError` out of a function whose whole
        # contract is to return a result.
        missing = NoAppliedRun(run_dir=layout.run_dir)
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={"code": missing.code.code},
            stderr=f"{missing} (the receipt at {layout.receipt_path} is not readable JSON)",
            error_code=missing.code.code,
        )
    simulation_id = str(receipt.get("simulationId") or bundle_name)

    # A mistyped --committed-slice id is refused by name, not silently
    # dropped into a withholding that tells the caller to pass the flag
    # they just passed.
    try:
        require_known_slice_exposes(receipt, slices)
    except FluxError as error:
        return StageResult(
            stage="ship",
            exit_code=2,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    # The run's own artifacts, re-derived, before any of them is packed.
    # An emit with no fidelity block has no gate to answer to, so without
    # this nothing at all stood between an edited stream and a signed
    # archive whose in-toto Statement names the pre-edit runDigest.
    run_digest_document = _read_json(layout.run_digest_path)
    artifact_mismatches = component_mismatches(
        layout, receipt, run_digest_document.get("components", {})
    )
    if artifact_mismatches:
        moved_artifacts = RunArtifactsMoved(components=artifact_mismatches)
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={
                "code": FLX_8001.code,
                "reason": moved_artifacts.code.code,
                "artifactComponents": {"ok": False, "movedComponents": list(artifact_mismatches)},
            },
            stdout="",
            stderr=f"{FLX_8001.code}: nothing packed -- {moved_artifacts}",
            error_code=FLX_8001.code,
        )

    try:
        plan_document = load_recorded_plan(layout)
        # The kernel the report and the pack name is the plan's, whatever kernel a
        # new plan would take.
        kernel = plan_kernel(plan_document)
        # Each link of the chain the statement stands on, checked before anything is
        # written: plan.json, its planDigest, run-digest.json, and the column record.
        require_run_digest_binds_plan(layout, plan_document, run_digest_document)
        bound = read_column_provenance(layout, plan_document)
        register = read_parameter_register(layout, plan_document)
    except FluxError as error:
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )
    coverage = None if bound is None else bound.coverages
    # The record the report is written from is the record that is packed.
    verified_files = {} if bound is None else {layout.column_provenance_path.name: bound.data}
    summary = register.summary if register is not None else None
    verified_files.update(_register_files(layout, register))
    planned_emits = plan_document.get("emits", {})
    now_ms = _now_ms() if committed_at is not None else None

    # The profile the plan pins, paired with the one handed over before any
    # emit is decided: a pinned run is gated and scored against exactly the
    # document it was planned against, or not shipped at all.
    from flux_engine.stages._scorecard import pair_run_profile

    try:
        paired = pair_run_profile(plan_document, profile, where=simulation_id)
        if paired is None:
            _refuse_unformed_scorecard(layout)
    except FluxError as error:
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    # A catalogued refusal from the gate's own inputs -- a malformed
    # `--committed-at`, an unreadable slice -- comes back as this stage's
    # result, not as an exception out of a function whose whole contract
    # is to return one. `raise_for_status` is the in-process caller's way
    # of asking for the raise.
    try:
        outcomes: Dict[str, EmitOutcome] = {
            expose_id: _decide_emit(
                layout,
                directory,
                expose_id,
                entry,
                slices.get(expose_id),
                dict(planned_emits.get(expose_id, {})),
                signing_key=signing_key,
                trust_key=trust_key,
                committed_at=committed_at,
                now_ms=now_ms,
                pinned=paired,
            )
            for expose_id, entry in sorted(receipt.get("emits", {}).items())
        }
        scorecard, decisions = _score_and_gate(
            plan_document, run_id, run_digest_document, paired, outcomes
        )
        if scorecard is not None and paired is not None:
            # The shape flags join the scorecard after it has gated: they
            # inform, and nothing here can withhold on them.
            scorecard = replace(scorecard, shape=shape_report(layout, receipt, paired))
    except FluxError as error:
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )
    withheld = {
        expose_id: decision for expose_id, decision in decisions.items() if decision.withheld
    }

    if withheld:
        # NDJSON, one CloudEvent per withheld emit -- SEAM-FIDELITY-SPEC
        # section 7's "configured sink (stdout NDJSON by default)". Built
        # into StageResult.stdout, never printed directly here: an
        # in-process caller (the engine facade, a future host process)
        # gets it back as data, and `fluxsim --json ship` stays parseable
        # JSON on its own stdout rather than NDJSON events followed by a
        # JSON envelope on the same stream.
        events = [
            decision.withheld_event(
                event_id=f"{run_id}-{expose_id}",
                time=datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                # The Simulation's id, never the directory basename: a
                # governance record of a withheld ship attributed to
                # whatever the operator happened to call a folder is
                # neither stable across machines nor meaningful to an
                # auditor, and it publishes the local layout.
                source=f"flux://{simulation_id}/{run_id}",
            )
            for expose_id, decision in withheld.items()
        ]
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={
                "code": FLX_8001.code,
                "withheld": list(withheld),
                "decisions": {k: v.to_document() for k, v in decisions.items()},
                "events": events,
                **_scorecard_artifact(scorecard),
            },
            # A rendered summary, not the CloudEvent. The events are
            # machine format and reach a machine through `artifacts`
            # (`fluxsim --json ship`); a person reading a terminal gets the
            # expose, the checks that failed and what each one found,
            # which is the one thing the NDJSON blob never told them.
            stdout=_withheld_summary(withheld, scorecard),
            stderr=f"{FLX_8001.code}: withheld {', '.join(sorted(withheld))}",
            error_code=FLX_8001.code,
        )

    # Every emit passed, so every record earned its place on disk. Written
    # here rather than inside the decision loop: a multi-emit bundle whose
    # third emit is withheld used to leave the first two emits' signed,
    # gate-accepted records behind from a ship that produced no bundle at
    # all (SEAM-FIDELITY-SPEC section 7's "write no bundle" covers the
    # archive; this covers the run directory).
    for expose_id, outcome in outcomes.items():
        if outcome.record is not None:
            atomic_write_text(
                layout.fidelity_record_path(expose_id),
                json.dumps(outcome.record, indent=2, sort_keys=True) + "\n",
            )
    verified_files.update(_write_scorecard(layout, scorecard))

    run_digest = digest_of_document(run_digest_document, subject="runDigest")
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        _write_evidence_report(
            layout,
            bundle_name=bundle_name,
            kernel_version=kernel.version,
            run_digest=run_digest,
            generated_at=generated_at,
            decisions=decisions,
            ephemeral_signing=signing_key is None,
            self_attested=trust_key is None,
            coverage=coverage,
            scored=_scored_run(layout, scorecard, outcomes, slices),
            register=summary,
        )
    except FluxError as error:
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    # `spec.bundle.pack` (the FLUX bundler) prints its own "[ok]
    # packed ..." summary directly to stdout -- library code with no way
    # for a caller to redirect it (see this module's build report). This
    # stage captures that output rather than let it leak past
    # StageResult.stdout: a `--json ship` invocation stays valid JSON on
    # stdout, and an in-process caller gets the summary back as data.
    pack_output = io.StringIO()
    with contextlib.redirect_stdout(pack_output):
        result = pack_evidence(
            directory,
            layout.run_dir,
            out_dir,
            run_digest=run_digest,
            kernel_version=kernel.version,
            generated_at=generated_at,
            verified_run_files=verified_files,
        )
    if not result.ok:
        # `result.problems` on this path is deliberately generic ("see the
        # pack() output above") -- the detail (which schema rule failed,
        # on which document) is exactly what the captured stdout above
        # holds. Fold it into the diagnostic here rather than let the
        # capture of that output turn this failure message into a
        # reference to output nobody can see any more.
        captured = pack_output.getvalue().strip()
        detail = "; ".join(result.problems)
        return StageResult(
            stage="ship",
            exit_code=1,
            _artifacts={"problems": list(result.problems), "packOutput": captured},
            stderr=f"{detail}\n{captured}" if captured else detail,
        )

    return StageResult(
        stage="ship",
        exit_code=0,
        _artifacts={
            "archive": str(result.archive),
            "buildStatement": str(result.build_statement_path),
            "runtimeStatement": str(result.runtime_statement_path),
            "decisions": {k: v.to_document() for k, v in decisions.items()},
            **_register_artifact(summary),
        },
        stdout="\n".join(
            line
            for line in (
                pack_output.getvalue().rstrip("\n"),
                _register_line(summary),
                f"shipped {result.archive}",
            )
            if line
        ),
    )
