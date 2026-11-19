# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify``: artifact re-derivation, golden regression, rows, RFC-04, fidelity.

Five independent checks over an already-applied run directory, each
answerable without re-running the simulation:

0. **The bundle, and the artifacts.** Before anything else, these
   re-derivations decide whether the run directory still describes
   the bundle it was given and still holds the bytes its digest was taken
   over:

   * ``plan.json`` is read back and confirmed to belong to the directory
     holding it (:func:`~flux_engine.stages.apply.load_recorded_plan`,
     ``FLX-2113``; ``FLX-2107`` when there is none). ``apply`` and ``ship``
     both did this; ``verify`` read no plan at all, so ``cp -R runs/<real> runs/<other>`` and ``fluxsim
     verify --run-id <other>`` exited 0 over a directory whose plan names
     a different run -- every file in the copy agreeing with every other
     file in the copy is exactly why the internal checks passed.
   * ``run-digest.json`` names that same plan (``FLX-2113``),
     ``column-provenance.json`` hashes to the plan's
     ``columnProvenanceDigest`` (``FLX-2115``), and, for a Simulation that
     declares a parameter register, ``parameter-register.json`` hashes to its
     ``parameterRegisterDigest`` (``FLX-2116``; a register file in a run whose
     plan binds none is refused too). ``ship`` makes the same checks, so the
     two stages hold one opinion of one directory, and the shares of the
     register are reported on a line of their own.
   * the bundle's ``inputDigest`` is recomputed from the directory named
     on the command line and compared with the receipt's (``FLX-2101``).
     ``bundle`` is a required argument of this command, and until this
     check existed the stage never read it: any path at all -- a typo, a
     deleted directory, a different bundle -- produced the same verdict.
   * every entry of KERNEL-SPEC §10's ``components`` map is recomputed
     from the artifacts on disk (:func:`recompute_components`) and
     compared with ``run-digest.json`` (``FLX-2110``). The golden leg
     below hashes the *document*; this leg hashes what the document
     claims to describe. Without it, an emit stream edited after ``apply``
     wrote it -- the shipped data itself -- passed every leg of this
     stage and shipped inside a signed evidence archive.

1. **Golden regression** -- the run's ``runDigest`` against the recorded
   baseline for this Simulation. No baseline yet is
   :attr:`~flux_engine.api.verdict.Reproducibility.NOT_CHECKED`, not a
   silent pass; a moved digest names the components that moved
   (:func:`flux_engine.core.trace.moved_components`). A moved digest whose
   component map is nevertheless identical -- the edit was one no part of
   the simulation reads -- is reported as such (``outputsIdentical``,
   :data:`INPUT_ONLY_DETAIL`) rather than as an unexplained mismatch.
2. **Row conformance** -- every emitted stream's row count matches its
   receipt and every row's width matches the column count its seam
   attestation declares. Not merely that the rows agree with *each
   other*: a stream whose every row is uniformly the wrong width agrees
   with itself and matches nothing that was declared. A line that is not
   JSON at all is a named problem here (``<exposeId>: line N is not
   JSON``), never an uncaught ``JSONDecodeError`` that aborts the stage
   and suppresses the remaining legs. (The stronger per-value check
   already ran once, inside :class:`~flux_engine.seam.emit.EmitWriter`,
   at ``apply`` time -- a row that violated KERNEL-SPEC §9 was never
   written. This is a second, independent read of what actually landed on
   disk.)
3. **RFC-04 vector replay** -- :func:`flux_engine.governance.gate.run_conformance_vectors`
   against the bundled enforcement vectors.
4. **Fidelity** -- SEAM-FIDELITY-SPEC §2-§8, per emit. An emit with no
   declared ``fidelity`` block in its ``extensions`` entry reports
   ``"not declared"``.
   One that declares a block but was given no ``--committed-slice`` on the
   command line reports ``"declared, not checked"`` -- this build has no
   pipeline that produces a committed real-data slice on its own (see the
   REQUEST in this stage's build report), so the boundary state a check
   needs has to come from the caller. Given a slice, the full commit ->
   verify pipeline of :mod:`flux_engine.concordance` runs and the result is
   ``"passed"``/``"failed"``.

   When a prior ``ship`` left a ``fidelity-record-<exposeId>.json`` in the
   run directory, this leg also checks that record's signature against the
   anchor ``--trust-key``/``--signing-key`` supplied
   (:func:`trust_anchor_for`, :func:`_record_signature_leg`). ``ship``
   prints ``fluxsim verify`` as the command a reader outside the process runs
   to reach the same conclusion from the pack, and ``verify`` could not be
   given a trust anchor at all -- so the one signature state that says
   *who* signed was reachable from ``ship`` and from nowhere else. With no
   anchor the check reports itself ``"not checked"``: a record's own key is
   never promoted to a trust anchor.

The headline (:meth:`~flux_engine.api.verdict.VerifyVerdict.headline`)
never conflates any of this with **validity**: nothing in this engine
measures a predicted outcome against the world, so ``validity`` is always
:attr:`~flux_engine.api.verdict.Validity.NOT_ASSERTED`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from flux_engine.api.stages import StageResult
from flux_engine.api.verdict import Reproducibility, Validity, VerifyVerdict
from flux_engine.concordance.gate import PackedEmit
from flux_engine.core.artifact import write_artifact
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.digest import digest_of_bytes, digest_of_document, digest_of_stream
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import Kernel
from flux_engine.core.trace import (
    COMPONENT_OPS,
    GoldenUpdate,
    check_golden_update,
    component_digest,
    golden_changelog_row,
    moved_components,
)
from flux_engine.evidence.register import render_register_line
from flux_engine.governance.gate import run_conformance_vectors
from flux_engine.safeio import (
    is_regular_file,
    read_bytes_nofollow,
    read_text_nofollow,
    require_real_directory,
    write_text_atomic,
)
from flux_engine.seam.emit import EMIT_LABEL
from flux_engine.spec import default_vectors_path
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages.apply import (
    load_recorded_plan,
    plan_kernel,
    require_run_digest_binds_plan,
)
from flux_engine.stages.errors import (
    FLX_2114,
    InputDigestMoved,
    NoAppliedRun,
    RunArtifactsMoved,
    UnknownCommittedSliceExpose,
)
from flux_engine.stages.layout import (
    EVENTS_LEDGER_COMPONENTS,
    POPULATION_LEDGER_COMPONENTS,
    RunLayout,
)
from flux_engine.stages.plan import read_column_provenance, read_parameter_register

if TYPE_CHECKING:
    from flux_engine.concordance.compiler import CompiledFidelity
    from flux_engine.concordance.sampling import SampledEvaluation, SampleRequest
    from flux_engine.concordance.scorecard import Scorecard, TableInput
    from flux_engine.concordance.shape import ShapeReport
    from flux_engine.concordance.verifier import FidelityEvaluation, PropertyOutcome
    from flux_engine.seam.profile import PinnedProfile

__all__ = [
    "GOLDEN_REMEDY",
    "GOLDEN_SNAPSHOT_SCHEMA_VERSION",
    "INPUT_ONLY_DETAIL",
    "component_mismatches",
    "packed_emit",
    "recompute_components",
    "recompute_input_digest",
    "require_known_slice_exposes",
    "shape_report",
    "trust_anchor_for",
    "verify",
]

#: Said when the golden regression moved but every entry of the run
#: digest's own ``components`` map is identical (KERNEL-SPEC §10's
#: "a golden failure names the component that moved" -- here it names
#: none). KERNEL-SPEC §11 builds ``inputDigest`` from the *raw bytes* of
#: every bundle document, with no exemption for a comment or a metadata
#: annotation, and ``inputDigest`` is a field of the run digest document
#: (§10), so an edit that no part of the simulation reads still moves the
#: top-level ``runDigest``. So does ``planDigest``, the field beside it: a plan
#: that gains a key moves every run digest with every component identical.
#: Distinguishing that case from a real drift is
#: the difference between "someone retitled a document" and "the
#: generator changed", and a reader must never have to guess which.
#: Envelope version of the ``<state_dir>/golden/<simulationId>.json``
#: snapshot ``--update-golden`` writes.
GOLDEN_SNAPSHOT_SCHEMA_VERSION = "flux-golden/1"

INPUT_ONLY_DETAIL = (
    "moved: no component -- every entry of run-digest.json's `components` map "
    "is identical to the baseline's, so what moved is one the simulation never "
    "reads: a bundle edit (KERNEL-SPEC §11 digests the raw bytes of every bundle "
    "document, so a comment, a metadata annotation or a reordered key still "
    "moves inputDigest and with it the run digest), or the plan, whose own "
    "digest is part of the run digest and moves when an engine release adds to "
    "the plan"
)

#: The remedy, said once. It used to be the closing sentence of BOTH the
#: regression line and :data:`INPUT_ONLY_DETAIL`, so the one failure printed
#: it twice, in two spellings, inside one run-on line.
GOLDEN_REMEDY = (
    're-record the baseline with `--update-golden --reason "<why>"` if the ' "move was intended"
)


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(read_text_nofollow(path))


def _now() -> str:
    """The wall clock, read here at the stage layer.

    ``core.artifact.write_artifact`` takes ``generated_at`` as a required
    argument precisely so the clock read happens outside the kernel; this
    is the stage that supplies it.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Re-derivation: the bundle's inputDigest, and every run-digest component
# ---------------------------------------------------------------------------


def recompute_input_digest(bundle_directory: Union[str, Path], *, kernel: Kernel) -> str:
    """Recompute KERNEL-SPEC §11's ``inputDigest`` for the bundle at *bundle_directory*.

    Loads and validates the bundle and digests its declared document
    bytes: the cheap half of the pipeline, with no population
    materialised and no row emitted, because ``inputDigest`` is a fact
    about the bundle's documents and its lockfile, and about nothing else
    in the directory. A file a pack wrote beside those documents is a
    product of packing the bundle rather than an input to running it, and
    does not move this value.

    The selection is not restated here.
    :func:`flux_engine.stages.pipeline.input_digest_of` makes it, and
    :func:`flux_engine.stages.pipeline.execute` calls that same function
    while running, so verify time and apply time cannot drift into a
    second opinion. ``tests/stages`` pins them to one code path.

    *kernel* is the kernel of the run's plan (:func:`~flux_engine.stages.apply.plan_kernel`),
    whose versions are inside the digest, never the kernel a new plan would take.
    """
    from flux_engine.seam.resolve import load_bundle

    return _pipeline.input_digest_of(load_bundle(Path(bundle_directory)), kernel=kernel).ref.value


def _emit_component_digest(layout: RunLayout, expose_id: str) -> Optional[str]:
    """Recompute one emit stream's ``outputDigest`` from the bytes on disk (§8).

    ``outputDigest = sha256(b"flux-emit/1\\x00" + JCS(header) + b"\\x00" +
    rows)`` where *rows* is the framed row block -- which is exactly what
    ``emits/<exposeId>.ndjson`` holds, byte for byte, because ``apply``
    writes :meth:`~flux_engine.seam.emit.EmitWriter.stream_bytes` into it
    unchanged. The header is the one the emit's seam attestation records.

    ``None`` when either file is missing: the caller reports that as a
    component that does not reproduce, not as a component that agrees.
    """
    attestation_path = layout.seam_attestation_path(expose_id)
    emit_path = layout.emit_path(expose_id)
    if not is_regular_file(attestation_path) or not is_regular_file(emit_path):
        return None
    try:
        attestation = _read_json(attestation_path)
        header = {
            "exposeId": expose_id,
            "columns": list(attestation["columns"]),
            "types": list(attestation["types"]),
            "contractDigest": attestation["reference"]["contractDigest"],
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    material = (
        EMIT_LABEL + b"\x00" + canonical_bytes(header) + b"\x00" + read_bytes_nofollow(emit_path)
    )
    return digest_of_stream(material, subject=f"outputDigest:{expose_id}").value


def _ledger_records(
    path: Path, components: Sequence[str]
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """One ledger's ops, grouped by component, in file order.

    ``None`` -- which the caller reads as "no component of this ledger
    reproduces" -- when the file is missing, holds a line that is not
    JSON, or carries an op that does not belong in it. That last case is
    the reason the grouping happens here rather than per component: a
    record filtered out for being the wrong op would let anything at all
    be appended to a ledger without moving a digest.
    """
    if not is_regular_file(path):
        return None
    grouped: Dict[str, List[Dict[str, Any]]] = {name: [] for name in components}
    of_op = {op.value: name for name in components for op in COMPONENT_OPS[name]}
    for line in read_text_nofollow(path).splitlines():
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(record, dict):
            return None
        op = record.get("op")
        component = of_op.get(op) if isinstance(op, str) else None
        if component is None:
            return None
        grouped[component].append(record)
    return grouped


def recompute_components(layout: RunLayout, receipt: Mapping[str, Any]) -> Dict[str, Any]:
    """Recompute KERNEL-SPEC §10's whole ``components`` map from the run directory.

    One entry per component, derived from the artifact that component's
    digest was taken over and from nothing else:

    * ``emits.<exposeId>`` -- the framed stream digest over
      ``emits/<exposeId>.ndjson`` (§8);
    * ``decisions`` -- the raw bytes of ``decisions.ndjson``;
    * the five trace components -- :func:`~flux_engine.core.trace.component_digest`
      over their ops as the ledgers hold them (:data:`~flux_engine.stages.layout.COMPONENT_LEDGER`),
      which ``apply`` writes in the emission order the digest was computed in.

    A component whose artifact is missing or unreadable is ``None``: the
    comparison below treats that as "does not reproduce", never as a pass.
    """
    components: Dict[str, Any] = {}
    for ledger in (POPULATION_LEDGER_COMPONENTS, EVENTS_LEDGER_COMPONENTS):
        grouped = _ledger_records(layout.ledger_path(ledger[0]), ledger)
        for name in ledger:
            components[name] = (
                None if grouped is None else component_digest(name, grouped[name]).value
            )
    decisions_path = layout.decisions_path
    components["decisions"] = (
        digest_of_bytes(read_bytes_nofollow(decisions_path), subject="decisionsDigest").value
        if is_regular_file(decisions_path)
        else None
    )
    components["emits"] = {
        expose_id: _emit_component_digest(layout, expose_id)
        for expose_id in sorted(receipt.get("emits", {}))
    }
    return components


def component_mismatches(
    layout: RunLayout, receipt: Mapping[str, Any], recorded: Mapping[str, Any]
) -> Tuple[str, ...]:
    """Name every component of *recorded* the run directory no longer reproduces.

    ``recorded`` is ``run-digest.json``'s own ``components`` block. Names
    are the ones KERNEL-SPEC §10 uses, with a per-expose stream reported
    as ``emits.<exposeId>`` -- the same spelling
    :func:`~flux_engine.core.trace.moved_components` gives a golden
    failure, so one vocabulary covers both.
    """
    recomputed = recompute_components(layout, receipt)
    return moved_components(dict(recorded), recomputed)


def require_known_slice_exposes(
    receipt: Mapping[str, Any], committed_slice: Mapping[str, str]
) -> None:
    """Refuse (``FLX-2111``) a ``--committed-slice`` naming an expose the run has no emit for.

    Slices are consumed by lookup (``slices.get(expose_id)``), so a
    mistyped id was never looked up, never warned about and never
    reported: the flag silently vanished and the emit it was meant for was
    reported as having no slice, telling the caller to pass an argument
    they had just passed.
    """
    known = sorted(receipt.get("emits", {}))
    unknown = sorted(set(committed_slice) - set(known))
    if unknown:
        raise UnknownCommittedSliceExpose(unknown=unknown, known=known)


def _decoded_rows(emit_path: Path, expose_id: str) -> Tuple[List[List[Any]], List[str]]:
    """Decode every row of an emit stream; a bad line is a problem, not an exception."""
    rows: List[List[Any]] = []
    problems: List[str] = []
    for index, line in enumerate(read_text_nofollow(emit_path).splitlines()):
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            problems.append(f"{expose_id}: line {index + 1} is not JSON")
    return rows, problems


def _declared_columns(layout: RunLayout, expose_id: str) -> Optional[List[str]]:
    """The column names the emit's seam attestation declares, or ``None``."""
    path = layout.seam_attestation_path(expose_id)
    if not is_regular_file(path):
        return None
    try:
        return [str(name) for name in _read_json(path)["columns"]]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _row_conformance(layout: RunLayout, receipt: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    for expose_id, entry in sorted(receipt.get("emits", {}).items()):
        emit_path = layout.emit_path(expose_id)
        if not is_regular_file(emit_path):
            problems.append(f"{expose_id}: no emit file at {emit_path}")
            continue
        rows, decode_problems = _decoded_rows(emit_path, expose_id)
        problems.extend(decode_problems)
        if decode_problems:
            continue
        if len(rows) != entry["rows"]:
            problems.append(
                f"{expose_id}: receipt declares {entry['rows']} rows, file has {len(rows)}"
            )
            continue
        # The declared width, not merely a shared one. Rows that all carry
        # the same wrong number of values agree with each other and match
        # nothing the contract declared, which is exactly the corruption
        # this leg exists to catch.
        columns = _declared_columns(layout, expose_id)
        if columns is None:
            problems.append(f"{expose_id}: no readable seam attestation to check row width against")
            continue
        widths = {len(row) for row in rows}
        wrong = sorted(width for width in widths if width != len(columns))
        if wrong:
            problems.append(
                f"{expose_id}: rows carry {wrong} values where the attestation "
                f"declares {len(columns)} columns"
            )
    return problems


def _golden(
    layout: RunLayout,
    simulation_id: str,
    run_digest: str,
    run_components: Dict[str, Any],
    *,
    update: bool,
    reason: Optional[str],
    declared_baseline: Optional[str],
    generated_at: str,
    kernel_version: str,
) -> Dict[str, Any]:
    """Compare against ``<state_dir>/golden/<simulationId>.json``, the record
    ``--update-golden`` maintains (bundle directories are frequently read-only
    checkouts -- the FLUX examples repo included -- so this build never writes
    into the bundle itself).

    The Simulation's own ``spec.golden.baselineDigest`` -- the schema's
    golden-regression anchor ($defs/golden) -- is reported alongside as
    ``specGoldenMatch`` for a reader's information, but it is not this
    function's pass/fail signal: an example or a fixture ships whatever
    placeholder its author typed (the FLUX spec repo's own telco example
    ships one no run has ever actually produced), and gating the whole loop
    on that would make "the bundle happens to carry an old string" and "the
    simulation is not reproducible" the same failure. Once an owner has run
    ``--update-golden`` and committed the resulting digest into the bundle's
    ``spec.golden`` block, this and that become the same check.
    """
    require_real_directory(layout.golden_dir)
    golden_path = layout.golden_path(simulation_id)
    changelog_path = layout.golden_changelog_path
    existing = _read_json(golden_path) if is_regular_file(golden_path) else None
    previous_digest = existing["baselineDigest"] if existing else None
    spec_golden_match = None if declared_baseline is None else declared_baseline == run_digest
    # KERNEL-SPEC section 10: `golden.baselineDigest === runDigest`. This
    # function's pass/fail signal is deliberately the out-of-band
    # `<state_dir>/golden/` record, not the bundle's own declared value
    # (see this function's docstring) -- but a reader must still be told
    # when the two disagree, never left to notice a silent gap on their
    # own.
    spec_golden_mismatch = declared_baseline is not None and spec_golden_match is False

    if update:
        changed = tuple(
            moved_components(existing.get("components", {}), run_components) if existing else ()
        )
        golden_update = GoldenUpdate(
            kernel_version=kernel_version,
            previous_digest=previous_digest,
            new_digest=run_digest,
            reason=reason or "",
            components=changed,
        )
        row = golden_changelog_row(golden_update)
        layout.ensure_golden_dir()
        changelog_text = (
            read_text_nofollow(changelog_path)
            if is_regular_file(changelog_path)
            else (
                "| kernelVersion | previous | new | components | reason |\n|---|---|---|---|---|\n"
            )
        )
        changelog_text += row + "\n"
        problems = check_golden_update(golden_update, changelog_text)
        if problems:
            return {"updated": False, "problems": problems}
        write_text_atomic(changelog_path, changelog_text)
        # Through the one artifact writer, not a hand-rolled `json.dumps`
        # beside it: `GoldenSnapshot` is a declared envelope kind, and a
        # kind with no producer is a choke point in the docstring only.
        write_artifact(
            golden_path,
            {
                "kernelVersion": kernel_version,
                "baselineDigest": run_digest,
                "components": run_components,
            },
            kind="GoldenSnapshot",
            schema_version=GOLDEN_SNAPSHOT_SCHEMA_VERSION,
            generated_at=generated_at,
            write_text=write_text_atomic,
        )
        return {
            "updated": True,
            "baselineDigest": run_digest,
            "movedComponents": list(changed),
            "specGoldenMatch": spec_golden_match,
            "specGoldenMismatch": spec_golden_mismatch,
        }

    if existing is None:
        return {
            "reproducibility": Reproducibility.NOT_CHECKED,
            "reason": "no golden baseline recorded yet",
            "outputsIdentical": None,
            "detail": None,
            "specGoldenMatch": spec_golden_match,
            "specGoldenMismatch": spec_golden_mismatch,
        }
    if existing["baselineDigest"] == run_digest:
        return {
            "reproducibility": Reproducibility.REPRODUCIBLE,
            "movedComponents": [],
            "outputsIdentical": True,
            "detail": None,
            "specGoldenMatch": spec_golden_match,
            "specGoldenMismatch": spec_golden_mismatch,
        }
    moved = moved_components(existing.get("components", {}), run_components)
    return {
        "reproducibility": Reproducibility.NOT_REPRODUCIBLE,
        "baselineDigest": existing["baselineDigest"],
        "runDigest": run_digest,
        "movedComponents": list(moved),
        "outputsIdentical": not moved,
        "detail": None if moved else INPUT_ONLY_DETAIL,
        "specGoldenMatch": spec_golden_match,
        "specGoldenMismatch": spec_golden_mismatch,
    }


def trust_anchor_for(
    signing_key: Optional[bytes], trust_key: Optional[bytes]
) -> Tuple[Optional[bytes], bool]:
    """The public key a record's signature is checked against, and whether it is self-attested.

    One derivation, called by both stages -- ``ship`` passes the key
    material it is about to sign with, ``verify`` passes whatever
    ``--signing-key`` it was given -- so ``fluxsim ship --signing-key k`` and
    ``fluxsim verify --signing-key k`` cannot reach different verdicts about
    one record:

    * ``--trust-key`` is the anchor. It came from outside this process, so
      a signature that verifies against it is a statement about *who*
      signed, and ``self_attested`` is false.
    * A signing key alone yields the public half of that seed. The
      signature check is then a statement about internal consistency and
      nothing else, which is what ``self_attested`` records.
    * Neither leaves the anchor ``None``. A record's own key is never
      promoted to a trust anchor, so a caller with no key at all gets no
      anchor and the check reports itself unchecked rather than green.
      (``ship`` never reaches that case: it always has key material, an
      ephemeral one when the caller supplied none.)
    """
    if trust_key is not None:
        return trust_key, False
    if signing_key is None:
        return None, False
    from flux_engine.concordance.record import Ed25519Signer

    return Ed25519Signer(private_key=signing_key, key_id="_").public_key, True


def _record_signature_leg(
    layout: RunLayout,
    expose_id: str,
    *,
    trust_anchor: Optional[bytes],
    self_attested: bool,
) -> Optional[Dict[str, Any]]:
    """Check the ``fidelity-record-<expose>.json`` a prior ``ship`` left, if any.

    ``ship`` prints ``fluxsim verify`` as the command a reader outside this
    process runs to reach the same conclusion from the pack alone, and
    ``verify`` had no way to be given a trust anchor at all -- so the one
    signature state that is a statement about *who* signed was reachable
    from ``ship`` and from nowhere else. Returns ``None`` when this run
    holds no record for the expose: there is nothing to say, and an
    outcome key that always appeared would say "not checked" about a file
    that does not exist.
    """
    record_path = layout.fidelity_record_path(expose_id)
    if not is_regular_file(record_path):
        return None
    if trust_anchor is None:
        return {
            "status": "not checked",
            "detail": (
                f"{record_path.name} is present; pass --trust-key <file> (the "
                "`publicKey` the record prints) to check its signature"
            ),
        }
    from flux_engine.concordance.record import verify_record

    reasons = verify_record(json.loads(read_text_nofollow(record_path)), trusted_keys=trust_anchor)
    if reasons:
        return {"status": "not trusted", "detail": "; ".join(reasons)}
    return {
        "status": "self-attested" if self_attested else "trusted",
        "detail": (
            "the signature verifies against the public half of the key this "
            "invocation was given, which says the record is internally "
            "consistent and nothing about who signed it"
            if self_attested
            else None
        ),
    }


@dataclass(frozen=True)
class ProfileCheck:
    """One emit compared with the run's pinned profile (PROFILE-SPEC.md §7).

    ``compiled`` and ``evaluation`` are ``None`` for an emit with no fidelity
    block. An emit whose rows could not be read before any property was
    compared carries an evaluation holding that one refusal, so its table is
    ``refused`` on the scorecard rather than silently empty.
    ``min_credibility`` is the emit's declared ``minCredibility``.
    """

    expose_id: str
    compiled: Optional["CompiledFidelity"] = None
    evaluation: Optional["FidelityEvaluation"] = None
    min_credibility: Optional[str] = None

    def table_input(self) -> "TableInput":
        """This emit as the scorecard reads it."""
        from flux_engine.concordance.scorecard import TableInput

        return TableInput(
            expose_id=self.expose_id,
            compiled=self.compiled,
            evaluation=self.evaluation,
            min_credibility=self.min_credibility,
            declares_fidelity=self.compiled is not None or self.evaluation is not None,
        )

    def leg_outcome(self) -> Dict[str, Any]:
        """The fidelity leg's entry for this emit, in the slice path's shape.

        ``reference`` says the comparison was with the pinned profile;
        ``censored`` lists the properties the profile left nothing to score.
        """
        from flux_engine.stages._scorecard import profile_check_detail

        evaluation = self.evaluation
        assert evaluation is not None
        refused = {refusal.property_id for refusal in evaluation.refusals}
        return {
            "status": "passed" if evaluation.within else "failed",
            "reference": "profile",
            "detail": profile_check_detail(evaluation),
            "refusals": [refusal.to_document() for refusal in evaluation.refusals],
            "outside": [
                _outside_document(outcome, refused=outcome.property_id in refused)
                for outcome in evaluation.outside
            ],
            "censored": [outcome.property_id for outcome in evaluation.censored],
        }


def _profile_checks(
    layout: RunLayout, receipt: Mapping[str, Any], pinned: "PinnedProfile"
) -> Dict[str, ProfileCheck]:
    """Compare every emit that declares a fidelity block with the pinned profile.

    The synthetic side is read as the slice path reads it, through the
    emit's seam attestation and the catalogued row guards, so a stream that
    cannot be read is a refusal of that emit (``FLX-8001``), never an
    exception that would cost the other emits their answer.
    """
    from flux_engine.concordance.compiler import compile_fidelity
    from flux_engine.concordance.errors import FLX_8001
    from flux_engine.concordance.predicate import expose_from_contract
    from flux_engine.stages._scorecard import (
        evaluate_against_profile,
        min_credibility,
        refused_evaluation,
    )

    checks: Dict[str, ProfileCheck] = {}
    for expose_id, entry in sorted(receipt.get("emits", {}).items()):
        fidelity_input_path = layout.fidelity_input_path(expose_id)
        if not entry.get("declaresFidelity"):
            checks[expose_id] = ProfileCheck(expose_id=expose_id)
            continue
        if not is_regular_file(fidelity_input_path):
            # No compiled block to score: the scorecard says the table was
            # refused, and why, rather than leaving it out.
            reason = f"no {fidelity_input_path.name} recorded for this run"
            checks[expose_id] = ProfileCheck(
                expose_id=expose_id,
                evaluation=refused_evaluation(expose_id, FLX_8001.code, reason),
            )
            continue
        try:
            fidelity_input = _read_json(fidelity_input_path)
            contract_document = fidelity_input["contract"]
            expose = expose_from_contract(contract_document, expose_id)
            compiled = compile_fidelity(
                contract=contract_document, emit=fidelity_input["emit"], expose=expose
            )
            floor = min_credibility(fidelity_input)
            synthetic_rows, problem = _synthetic_rows(layout, expose_id)
        except FluxError as error:
            checks[expose_id] = ProfileCheck(
                expose_id=expose_id,
                evaluation=refused_evaluation(expose_id, error.code.code, error.detail),
            )
            continue
        except _UNREADABLE as error:
            # A record nothing could parse: named by its kind, never quoted.
            reason = f"{fidelity_input_path.name} cannot be read ({type(error).__name__})"
            checks[expose_id] = ProfileCheck(
                expose_id=expose_id,
                evaluation=refused_evaluation(expose_id, FLX_8001.code, reason),
            )
            continue
        checks[expose_id] = ProfileCheck(
            expose_id=expose_id,
            compiled=compiled,
            evaluation=(
                refused_evaluation(expose_id, *problem)
                if problem is not None
                else evaluate_against_profile(compiled, pinned, synthetic_rows)
            ),
            min_credibility=floor,
        )
    return checks


#: What reading one emit's run records can raise besides a catalogued refusal:
#: a file gone or unreadable, text that is not JSON, or JSON of the wrong shape.
_UNREADABLE = (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError)


def _synthetic_rows(
    layout: RunLayout, expose_id: str
) -> Tuple[List[Any], Optional[Tuple[str, str]]]:
    """An emit's rows keyed by column, or ``(code, reason)`` when they cannot be read.

    Through the catalogued guards the slice path reads them with: a line that
    is not JSON, or a row whose width is not the header's, is a refusal of
    this emit (``FLX-8001``), never an exception.
    """
    from flux_engine.concordance.errors import FLX_8001

    emit_path = layout.emit_path(expose_id)
    if not is_regular_file(emit_path):
        return [], (FLX_8001.code, f"no emit file {emit_path.name} for this run")
    emitted_rows, decode_problems = _decoded_rows(emit_path, expose_id)
    if decode_problems:
        return [], (FLX_8001.code, "; ".join(decode_problems))
    attestation_path = layout.seam_attestation_path(expose_id)
    try:
        attestation = _read_json(attestation_path)
        return packed_emit(attestation, expose_id, emitted_rows).as_mappings(), None
    except FluxError as error:
        return [], (error.code.code, error.detail)
    except _UNREADABLE as error:
        return [], (
            FLX_8001.code,
            f"{emit_path.name} or {attestation_path.name} cannot be read as rows "
            f"({type(error).__name__})",
        )


def _emit_lines(emit_path: Path) -> Iterator[str]:
    """An emit's lines, one at a time, split as :func:`_decoded_rows` splits them.

    :func:`_decoded_rows` reads the text whole and splits it with
    :meth:`str.splitlines`, which ends a line at more characters than a
    file's universal newlines do (``\\v``, ``\\f``, ``\\x1c`` to ``\\x1e``,
    ``\\x85``, ``\\u2028``, ``\\u2029``). Read with ``newline=""``, every
    character stays, and splitting each line read again gives the same
    lines in the same order, so both readers count the same records.
    """
    with emit_path.open(encoding="utf-8", newline="") as stream:
        for line in stream:
            yield from line.splitlines()


def _sampled_blocks(
    layout: RunLayout, expose_id: str, sample: "SampleRequest", key: str
) -> Tuple[int, int, Dict[int, List[Any]], Optional[Tuple[str, str]]]:
    """An emit's record count, its block count, and the rows of the blocks a sample chose.

    Counts the records first, chooses the blocks from the count, then decodes
    only the lines of those blocks. The rows are keyed by each block's first
    record, so a refusal names a row by its place in the emit. The refusals
    are :func:`_synthetic_rows`'s: a line that is not JSON, a missing emit
    file or seam attestation, or a row the header's width refuses is an
    ``FLX-8001`` refusal of this emit, never an exception.
    """
    from flux_engine.concordance.errors import FLX_8001
    from flux_engine.concordance.sampling import choose_blocks

    emit_path = layout.emit_path(expose_id)
    if not emit_path.is_file():
        return 0, 0, {}, (FLX_8001.code, f"no emit file {emit_path.name} for this run")
    attestation_path = layout.seam_attestation_path(expose_id)
    size = sample.block_records
    try:
        records = sum(1 for line in _emit_lines(emit_path) if line)
        total, chosen = choose_blocks(records, sample, key)
        wanted = set(chosen)
        decoded: Dict[int, List[List[Any]]] = {index: [] for index in chosen}
        problems: List[str] = []
        index = 0
        for number, line in enumerate(_emit_lines(emit_path), start=1):
            if not line:
                continue
            block = index // size
            index += 1
            if block not in wanted:
                continue
            try:
                decoded[block].append(json.loads(line))
            except json.JSONDecodeError:
                problems.append(f"{expose_id}: line {number} is not JSON")
        if problems:
            return records, total, {}, (FLX_8001.code, "; ".join(problems))
        attestation = _read_json(attestation_path)
        blocks = {
            block * size: packed_emit(attestation, expose_id, rows).as_mappings(block * size)
            for block, rows in decoded.items()
        }
        return records, total, blocks, None
    except FluxError as error:
        return 0, 0, {}, (error.code.code, error.detail)
    except _UNREADABLE as error:
        return (
            0,
            0,
            {},
            (
                FLX_8001.code,
                f"{emit_path.name} or {attestation_path.name} cannot be read as rows "
                f"({type(error).__name__})",
            ),
        )


def _refused_sample(expose_id: str, code: str, reason: str) -> "SampledEvaluation":
    """A sampled evaluation that read nothing: one refusal, so the emit fails with its reason."""
    from flux_engine.concordance.sampling import SampledEvaluation
    from flux_engine.concordance.verifier import Refusal

    return SampledEvaluation(
        expose_id=expose_id,
        blocks=0,
        blocks_read=0,
        records=0,
        records_read=0,
        refusals=(Refusal(code=code, reason=reason),),
    )


def _sampled_evaluations(
    layout: RunLayout,
    receipt: Mapping[str, Any],
    run_id: str,
    paired: "PinnedProfile",
    sample: "SampleRequest",
) -> Dict[str, "SampledEvaluation"]:
    """Compare each emit :func:`_profile_checks` would compare, on a sample of its blocks.

    The emit's fidelity block is compiled as :func:`_profile_checks` compiles
    it. An emit whose input cannot be read gets a refusal in place of an
    evaluation, so the other emits still answer. An emit with a committed
    slice is compared too: its leg holds the slice's result and this one
    (:meth:`ProfileLegs.also`), as without a sample.
    """
    from flux_engine.concordance.compiler import compile_fidelity
    from flux_engine.concordance.errors import FLX_8001
    from flux_engine.concordance.predicate import expose_from_contract
    from flux_engine.concordance.sampling import evaluate_sample

    evaluations: Dict[str, "SampledEvaluation"] = {}
    for expose_id, entry in sorted(receipt.get("emits", {}).items()):
        if not entry.get("declaresFidelity"):
            continue
        fidelity_input_path = layout.fidelity_input_path(expose_id)
        if not fidelity_input_path.is_file():
            reason = f"no {fidelity_input_path.name} recorded for this run"
            evaluations[expose_id] = _refused_sample(expose_id, FLX_8001.code, reason)
            continue
        try:
            fidelity_input = _read_json(fidelity_input_path)
            contract_document = fidelity_input["contract"]
            expose = expose_from_contract(contract_document, expose_id)
            compiled = compile_fidelity(
                contract=contract_document, emit=fidelity_input["emit"], expose=expose
            )
            records, total, blocks, problem = _sampled_blocks(
                layout, expose_id, sample, f"{run_id}/{expose_id}"
            )
            if problem is not None:
                evaluations[expose_id] = _refused_sample(expose_id, *problem)
                continue
            evaluations[expose_id] = evaluate_sample(
                compiled, paired.profile, blocks, population=total, records=records
            )
        except FluxError as error:
            evaluations[expose_id] = _refused_sample(expose_id, error.code.code, error.detail)
        except _UNREADABLE as error:
            reason = f"{fidelity_input_path.name} cannot be read ({type(error).__name__})"
            evaluations[expose_id] = _refused_sample(expose_id, FLX_8001.code, reason)
    return evaluations


def _sampled_outcome(evaluation: "SampledEvaluation") -> Dict[str, Any]:
    """The fidelity leg's entry for an emit compared on a sample."""
    return {
        "status": evaluation.status,
        "reference": "profile",
        "sampled": True,
        "detail": evaluation.detail(),
        **evaluation.to_document(),
    }


def _unpaired_sample(recorded_plan: Mapping[str, Any]) -> StageResult:
    """A sample asked for with no profile to compare it with: ``FLX-3109``, as the pairing is.

    Refused before any leg runs, not reported as a leg that was not checked.
    """
    from flux_engine.seam.profile import FLX_3109
    from flux_engine.stages._scorecard import plan_pin

    pin = plan_pin(recorded_plan)
    reason = (
        "this run's plan pins no profile, so there is nothing to compare a sample with"
        if pin is None
        else f"this run's plan pins profile {pin}; pass that profile with --profile"
    )
    refusal = FluxError(FLX_3109, f"--sample compares with the pinned profile: {reason}")
    return StageResult(
        stage="verify",
        exit_code=1,
        _artifacts={"code": refusal.code.code},
        stderr=str(refusal),
        error_code=refusal.code.code,
    )


#: The scorecard leg of a sampled verify: no scorecard is formed from a sample.
_SAMPLED_SCORECARD_DETAIL = "a sampled verify forms no scorecard; verify without --sample for it"


def shape_report(
    layout: RunLayout, receipt: Mapping[str, Any], pinned: "PinnedProfile"
) -> "ShapeReport":
    """The shape flags for every emit of the run, against the pinned profile.

    SCORECARD-SPEC.md §6. Every emit whose table the profile describes is
    read, one at a time and as the profile path reads it
    (:func:`_synthetic_rows`), whether or not it declares a fidelity block.
    An emit whose rows cannot be read is refused on the report, with its
    reason, and the others are still checked. ``ship`` takes the same
    report into its scorecard; the flags inform and gate nothing.
    """
    from flux_engine.concordance.shape import (
        EmitSummary,
        decide_shape,
        refused_emit,
        summarise_emit,
    )

    summaries: List[EmitSummary] = []
    for expose_id in sorted(receipt.get("emits", {})):
        if expose_id not in pinned.profile.tables:
            summaries.append(summarise_emit(pinned.profile, expose_id, ()))
            continue
        rows, problem = _synthetic_rows(layout, expose_id)
        if problem is not None:
            summaries.append(refused_emit(expose_id, *problem))
        else:
            summaries.append(summarise_emit(pinned.profile, expose_id, rows))
    return decide_shape(summaries)


def _not_checked(expose_id: str, pinned: Optional[str]) -> Dict[str, Any]:
    """An emit's fidelity leg when nothing was handed over to check it against.

    The detail names the flag that turns it into a result: ``--committed-slice``,
    and ``--profile`` too when the run's plan pins one.
    """
    if pinned is None:
        return {
            "status": "declared, not checked",
            "detail": "declares a fidelity block; pass --committed-slice "
            f"{expose_id}=file://<path> to run the check",
        }
    return {
        "status": "declared, not checked",
        "detail": (
            f"declares a fidelity block, and this run's plan pins profile {pinned}; "
            f"pass that profile with --profile, or --committed-slice "
            f"{expose_id}=file://<path>, to run the check"
        ),
    }


@dataclass(frozen=True)
class ProfileLegs:
    """What a run whose plan pins a profile adds to ``verify``.

    ``pinned`` is the digest the plan pins (``None``: nothing is added).
    ``checks`` holds every emit compared with the profile and ``scorecard``
    the run's scores, both ``None`` when no profile was handed over; ``leg``
    is the ``scorecard`` artifact either way.
    """

    pinned: Optional[str] = None
    checks: Optional[Mapping[str, ProfileCheck]] = None
    scorecard: Optional["Scorecard"] = None
    leg: Optional[Dict[str, Any]] = None
    #: Why a profile that was handed over compared nothing, when it did not.
    skipped: Optional[str] = None
    #: A sampled verify: the request and each emit's evaluation on its sample.
    sample: Optional["SampleRequest"] = None
    sampled: Optional[Mapping[str, "SampledEvaluation"]] = None

    def unsliced(self, expose_id: str) -> Dict[str, Any]:
        """The fidelity leg of an emit handed no committed slice."""
        if self.sampled is not None and expose_id in self.sampled:
            return _sampled_outcome(self.sampled[expose_id])
        check = (self.checks or {}).get(expose_id)
        if check is not None and check.evaluation is not None:
            return check.leg_outcome()
        if self.skipped is not None:
            return {"status": "declared, not checked", "detail": self.skipped}
        return _not_checked(expose_id, self.pinned)

    def also(self, expose_id: str, sliced: Dict[str, Any]) -> Dict[str, Any]:
        """An emit checked against its committed slice, and against the pinned profile too.

        ``ship`` ships such an emit only when both references find it within,
        so the leg fails when either does not; the profile's own result is
        under ``profile``, compared on the sample when ``verify`` samples.
        With no profile handed over, *sliced* is returned as it is.
        """
        if self.sampled is not None and expose_id in self.sampled:
            profiled = _sampled_outcome(self.sampled[expose_id])
        else:
            check = (self.checks or {}).get(expose_id)
            if check is None or check.evaluation is None:
                return sliced
            profiled = check.leg_outcome()
        outcome = dict(sliced, profile=profiled)
        if profiled["status"] == "failed":
            outcome["status"] = "failed"
        return outcome

    def failures(self) -> List[str]:
        """A missed threshold or ``minCredibility``, one line each: each fails the stage."""
        return self.scorecard.failure_lines() if self.scorecard is not None else []

    def artifact(self) -> Dict[str, Any]:
        """The ``scorecard`` artifact key, or nothing for a run that pins no profile."""
        extra: Dict[str, Any] = {"scorecard": self.leg} if self.leg is not None else {}
        if self.sample is not None and self.sampled is not None:
            extra["sample"] = {
                **self.sample.to_document(),
                "emits": {name: found.to_document() for name, found in self.sampled.items()},
            }
        return extra

    def lines(self) -> List[str]:
        """The scorecard's lines under the legs: nothing for a run that pins no profile."""
        if self.scorecard is not None:
            return [f"  {line}" for line in self.scorecard.summary_lines()]
        if self.leg is not None:
            return [f"  scorecard: {self.leg['status']} -- {self.leg['detail']}"]
        return []


#: Why a pinned run handed its profile is not scored: its bytes are not the run's.
_ARTIFACTS_MOVED_DETAIL = (
    "the run's artifacts do not reproduce run-digest.json (FLX-2110), so nothing was "
    "compared with the profile; re-run apply"
)


def _profile_legs(
    layout: RunLayout,
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    run_id: str,
    run_digest: str,
    paired: Optional["PinnedProfile"],
    *,
    artifacts_ok: bool,
    sample: Optional["SampleRequest"] = None,
) -> ProfileLegs:
    """Leg 4b: compare every emit with the paired profile and score the run.

    The pairing itself (:func:`_paired_profile`) runs before any leg and
    refuses there. A pinned run handed no profile reports its scorecard as
    not checked, with the flag that runs it; one whose artifacts do not
    reproduce its digest is not scored at all, since its rows are not the
    run's. With a *sample*, each emit is compared on a sample of its blocks
    instead and no scorecard is formed from it.
    """
    from flux_engine.stages._scorecard import build_run_scorecard, plan_pin, unpaired_detail

    pinned = plan_pin(plan_document)
    if pinned is None:
        return ProfileLegs()
    if paired is None:
        return ProfileLegs(
            pinned=pinned, leg={"status": "not checked", "detail": unpaired_detail(pinned)}
        )
    if not artifacts_ok:
        return ProfileLegs(
            pinned=pinned,
            skipped=_ARTIFACTS_MOVED_DETAIL,
            leg={"status": "not checked", "detail": _ARTIFACTS_MOVED_DETAIL},
        )
    if sample is not None:
        return ProfileLegs(
            pinned=pinned,
            leg={"status": "not checked", "detail": _SAMPLED_SCORECARD_DETAIL},
            sample=sample,
            sampled=_sampled_evaluations(layout, receipt, run_id, paired, sample),
        )
    checks = _profile_checks(layout, receipt, paired)
    scorecard = build_run_scorecard(
        plan_document=plan_document,
        run_id=run_id,
        run_digest=run_digest,
        pinned=paired,
        tables=[check.table_input() for check in checks.values()],
    )
    # The shape flags inform; nothing in the leg reads them (SCORECARD-SPEC.md §6).
    scorecard = replace(scorecard, shape=shape_report(layout, receipt, paired))
    return ProfileLegs(
        pinned=pinned,
        checks=checks,
        scorecard=scorecard,
        leg={"status": "computed", "met": scorecard.met, "document": scorecard.to_document()},
    )


def _fidelity_leg(
    layout: RunLayout,
    receipt: Dict[str, Any],
    committed_slice: Dict[str, str],
    *,
    trust_anchor: Optional[bytes] = None,
    self_attested: bool = False,
    against: Optional["ProfileLegs"] = None,
) -> Dict[str, Any]:
    """Each emit's fidelity check: against its committed slice, else against the pinned profile.

    *against* holds what the run's pinned profile found (:func:`_profile_legs`),
    so an emit with no slice reports that comparison, or, when nothing was
    handed over to compare it with, the flag that runs one.
    """
    outcomes: Dict[str, Any] = {}
    for expose_id, entry in sorted(receipt.get("emits", {}).items()):
        if not entry.get("declaresFidelity"):
            outcomes[expose_id] = {"status": "not declared"}
            continue
        slice_uri = committed_slice.get(expose_id)
        if slice_uri is not None:
            outcome = (against or ProfileLegs()).also(
                expose_id, _run_fidelity_check(layout, expose_id, slice_uri)
            )
        else:
            outcome = (against or ProfileLegs()).unsliced(expose_id)
        signature = _record_signature_leg(
            layout, expose_id, trust_anchor=trust_anchor, self_attested=self_attested
        )
        if signature is not None:
            outcome["record"] = signature
            # A record on disk that does not verify against the anchor the
            # caller supplied is a failure of this leg, not a footnote to
            # it: the artifact a ship published is not the one the reader
            # trusts. The detail moves with the status -- leaving "pass
            # --committed-slice to run the check" beside the word `failed`
            # would name a check that did not run as the reason.
            if signature["status"] == "not trusted":
                was_failure = outcome["status"] == "failed"
                outcome["status"] = "failed"
                details = [f"{layout.fidelity_record_path(expose_id).name}: {signature['detail']}"]
                if was_failure and outcome.get("detail"):
                    details.append(str(outcome["detail"]))
                outcome["detail"] = "; ".join(details)
        outcomes[expose_id] = outcome
    return outcomes


#: What dates a committed state this leg forms itself. `verify` takes no
#: `--committed-at`, and stamping the host clock would date one record set
#: differently on every call; the owner's own declaration reaches a state
#: through `fluxsim ship --committed-at`.
_UNDECLARED_COMMITMENT_TIME = "2026-01-01T00:00:00.000Z"


def _run_fidelity_check(layout: RunLayout, expose_id: str, slice_uri: str) -> Dict[str, Any]:
    from flux_engine.concordance.boundary.slice import read_slice
    from flux_engine.concordance.boundary.store import BoundaryStore
    from flux_engine.concordance.compiler import compile_fidelity
    from flux_engine.concordance.predicate import expose_from_contract
    from flux_engine.concordance.verifier import ClearStatistic

    attestation = _read_json(layout.seam_attestation_path(expose_id))
    fidelity_input_path = layout.fidelity_input_path(expose_id)
    if not is_regular_file(fidelity_input_path):
        return {
            "status": "declared, not checked",
            "detail": f"no {fidelity_input_path.name} recorded for this run",
        }
    fidelity_input = _read_json(fidelity_input_path)
    contract_document = fidelity_input["contract"]
    emit_entry = fidelity_input["emit"]
    expose = expose_from_contract(contract_document, expose_id)
    compiled = compile_fidelity(contract=contract_document, emit=emit_entry, expose=expose)

    rows, reference = read_slice(slice_uri, maximum=compiled.block.records.maximum)
    # The same boundary store `ship` commits through, so a slice checked
    # here and then shipped is one committed state with one blinding value
    # and one C2 -- not two states that happen to hold the same records.
    # This leg declares no commitment time of its own: `stamped_at` dates
    # only a state being formed for the first time, and it is a fixed
    # instant rather than the host clock because this leg reports a
    # comparison of two datasets and never measures a state's age (that is
    # `ship`'s gate, against the owner's own `--committed-at`).
    store = BoundaryStore.for_state_dir(
        layout.state_dir, max_checks_per_state=compiled.block.budget.max_checks_per_state
    )
    state = store.commit(
        rows=rows,
        compiled=compiled,
        slice_reference=reference,
        stamped_at=_UNDECLARED_COMMITMENT_TIME,
    )

    emit_path = layout.emit_path(expose_id)
    emitted_rows, decode_problems = _decoded_rows(emit_path, expose_id)
    if decode_problems:
        return _failed_before_comparing("; ".join(decode_problems))
    # Through the catalogued guard, never a bare `zip(..., strict=True)`:
    # a row whose width does not match the header is `FLX-8001` naming the
    # row, the width and the declared column count, not a ValueError with
    # no code that aborts the stage and suppresses every other leg.
    try:
        synthetic_rows = packed_emit(attestation, expose_id, emitted_rows).as_mappings()
    except FluxError as error:
        return _failed_before_comparing(str(error))

    evaluation = ClearStatistic(
        compiled=compiled, committed=state, synthetic_rows=synthetic_rows
    ).evaluate()
    refused = {refusal.property_id for refusal in evaluation.refusals}
    return {
        "status": "passed" if evaluation.within else "failed",
        "refusals": [refusal.to_document() for refusal in evaluation.refusals],
        "outside": [
            _outside_document(outcome, refused=outcome.property_id in refused)
            for outcome in evaluation.outside
        ],
    }


def _failed_before_comparing(detail: str) -> Dict[str, Any]:
    """A failed leg that never compared a property: nothing outside, nothing refused."""
    return {"status": "failed", "refusals": [], "outside": [], "detail": detail}


def _outside_document(outcome: "PropertyOutcome", *, refused: bool) -> Dict[str, Any]:
    """One property that is not within its margin, as ``--json`` and the failed line name it.

    Fields, never values (ADR 0003, KERNEL-SPEC section 9): the property's id,
    kind and columns, the column a grouped property groups by, and the groups
    that landed outside by ORDINAL in the NFC-sorted group order. That is the
    ``group #N`` an ``FLX-5005`` refusal already says, for the same reason: a
    group's key is a value read out of the committed slice. A property the
    check refused is listed too, because §6's record row calls it ``outside``;
    ``refused`` says the check declined to answer for it, as a whole or for
    one group, and its ``refusals`` entry says why.
    """
    document: Dict[str, Any] = {
        "propertyId": outcome.property_id,
        "property": outcome.kind.value,
        "columns": list(outcome.columns),
        "refused": refused,
    }
    if outcome.group_by is not None:
        document["groupBy"] = outcome.group_by
        document["groupOrdinals"] = [
            # Outside, not merely not within: a group the profile withheld is
            # censored, never a failure. On the slice path the two agree.
            ordinal
            for ordinal, group in enumerate(outcome.groups)
            if group.outcome == "outside"
        ]
    return document


def _failure_lines(outcome: Mapping[str, Any]) -> List[str]:
    """What a failed fidelity check says about its properties, one line each.

    The wording is ``ship``'s (``property <id> (<kind> of <columns>)``), with
    the column a grouped property groups by and the groups that failed added.
    A refusal follows with its code and reason, which already name columns,
    rows and counts and never a value. A leg that passed, was not checked, or
    failed before any property was compared has neither, and gets no line.
    """
    lines = _sampled_failure_lines(outcome) if outcome.get("sampled") else []
    for entry in outcome.get("outside", ()):
        subject = f"{entry['property']} of {', '.join(entry['columns'])}"
        where = ""
        if "groupBy" in entry:
            subject += f", grouped by {entry['groupBy']}"
            ordinals = entry["groupOrdinals"]
            if ordinals and not entry["refused"]:
                noun = "groups" if len(ordinals) > 1 else "group"
                where = f" in {noun} " + ", ".join(f"#{ordinal}" for ordinal in ordinals)
        verdict = (
            "refused, not within the declared margin"
            if entry["refused"]
            else "outside the declared margin"
        )
        lines.append(f"property {entry['propertyId']} ({subject}): {verdict}{where}")
    for refusal in outcome.get("refusals", ()):
        names = f" on property {refusal['propertyId']}" if refusal.get("propertyId") else ""
        lines.append(f"refusal {refusal['code']}{names}: {refusal['reason']}")
    profiled = outcome.get("profile")
    if profiled is not None and profiled.get("status") == "failed":
        lines.append(f"against the pinned profile: {profiled.get('detail')}")
        lines.extend(f"  {line}" for line in _failure_lines(profiled))
    return lines


def _sampled_failure_lines(outcome: Mapping[str, Any]) -> List[str]:
    """The means a sampled check found outside the band, or could not place, one line each."""
    return [
        f"property {found['propertyId']} (mean of {', '.join(found['columns'])}): "
        f"{found['outcome']} at the 95 % interval"
        for found in outcome.get("properties", ())
        if found["outcome"] in ("outside", "undecided")
    ]


def _failed_legs(outcomes: Mapping[str, Any], failed: Sequence[str]) -> List[str]:
    """The failure message for each expose whose fidelity leg failed, on stderr."""
    lines: List[str] = []
    for expose_id in failed:
        lines.append(f"{expose_id}: fidelity check failed")
        lines.extend(f"  {line}" for line in _failure_lines(outcomes[expose_id]))
    return lines


def _failure_summary(expose_id: str, outcome: Mapping[str, Any]) -> str:
    """The same facts on one line, for the ``--update-golden`` refusal that joins its problems."""
    named = _failure_lines(outcome)
    return f"{expose_id}: fidelity check failed" + (": " + ", ".join(named) if named else "")


def packed_emit(
    attestation: Mapping[str, Any], expose_id: str, rows: Sequence[Sequence[Any]]
) -> "PackedEmit":
    """Build the §8 header and rows one emit is read back as.

    Shared by ``verify``'s fidelity leg and ``ship``'s gate so both read
    the pack the same way and both get
    :meth:`~flux_engine.concordance.gate.PackedEmit.as_mappings`' width
    refusal (``FLX-8001``) instead of an uncatalogued ``ValueError`` from
    a bare ``zip(..., strict=True)``.
    """
    header = {
        "exposeId": expose_id,
        "columns": list(attestation["columns"]),
        "types": list(attestation["types"]),
        "contractDigest": attestation["reference"]["contractDigest"],
    }
    return PackedEmit(header=header, rows=tuple(tuple(row) for row in rows))


def _leg_lines(
    golden: Mapping[str, Any],
    *,
    artifact_mismatches: Sequence[str],
    row_problems: Sequence[str],
    vector_failures: int,
    fidelity_outcomes: Mapping[str, Any],
) -> List[str]:
    """One human line per leg, under the headline.

    This command runs five independent checks and used to tell a person
    six words about them, while the same invocation's ``--json`` envelope
    carried the reason each leg said what it said and the exact flag that
    turns "not checked" into a result. The human was given strictly less
    than the machine, and no next step. Both now come from the same
    values, so neither can quietly say more than the other.
    """
    lines: List[str] = []

    reproducibility = golden.get("reproducibility", Reproducibility.NOT_CHECKED)
    if reproducibility is Reproducibility.NOT_CHECKED:
        lines.append(
            f"  golden: not checked ({golden.get('reason', 'no baseline recorded yet')} "
            '-- record one with `--update-golden --reason "<why>"`)'
        )
    elif reproducibility is Reproducibility.REPRODUCIBLE:
        lines.append("  golden: reproducible (this run matches the recorded baseline)")
    else:
        moved = ", ".join(golden.get("movedComponents") or ()) or "no component"
        lines.append(f"  golden: NOT reproducible (moved: {moved})")

    lines.append(
        "  artifacts: every run-digest component reproduces"
        if not artifact_mismatches
        else f"  artifacts: {len(artifact_mismatches)} component(s) do not reproduce: "
        + ", ".join(artifact_mismatches)
    )
    lines.append(
        "  rows: ok"
        if not row_problems
        else f"  rows: {len(row_problems)} problem(s) -- {row_problems[0]}"
    )
    lines.append(f"  rfc-04 vectors: {vector_failures} failure(s)")
    for expose_id, outcome in sorted(fidelity_outcomes.items()):
        status = outcome.get("status", "unknown")
        detail = outcome.get("detail")
        lines.append(f"  fidelity {expose_id}: {status}" + (f" -- {detail}" if detail else ""))
        lines.extend(f"    {line}" for line in _failure_lines(outcome))
        record = outcome.get("record")
        if record:
            record_detail = record.get("detail")
            lines.append(
                f"  fidelity {expose_id} record signature: {record.get('status', 'unknown')}"
                + (f" -- {record_detail}" if record_detail else "")
            )
    return lines


def _paired_profile(
    plan_document: Mapping[str, Any],
    receipt: Mapping[str, Any],
    profile: Union[str, Path, None],
) -> Optional["PinnedProfile"]:
    """Leg 0c: the profile the run's plan pins, paired with the one handed over, or ``None``.

    Paired exactly as ``plan`` pairs them: a profile with no pin, another
    profile, and a document that is not a profile are refused here, before
    any leg, since scoring the run against a document it was not planned
    against would be a comparison of nothing. A pin with no profile is
    ``None``: the legs report it as not checked.
    """
    from flux_engine.stages._scorecard import pair_run_profile, plan_pin

    if profile is None and plan_pin(plan_document) is not None:
        return None
    simulation_id = str(receipt.get("simulationId") or plan_document.get("simulationId"))
    return pair_run_profile(plan_document, profile, where=simulation_id)


def verify(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    state_dir: Union[str, Path, None] = None,
    update_golden: bool = False,
    reason: Optional[str] = None,
    committed_slice: Optional[Dict[str, str]] = None,
    vectors_path: Optional[Union[str, Path]] = None,
    allow_declared_baseline_drift: bool = False,
    signing_key: Optional[bytes] = None,
    trust_key: Optional[bytes] = None,
    profile: Union[str, Path, None] = None,
    sample: Optional["SampleRequest"] = None,
) -> StageResult:
    """Verify the run recorded at ``<state_dir>/runs/<run_id>/``.

    *trust_key* is the 32-byte raw ed25519 **public** key any fidelity
    record this run already holds is checked against, and *signing_key* is
    the seed whose public half stands in for it when the caller has only
    that -- the same derivation ``ship`` performs, shared through
    :func:`trust_anchor_for` so the two stages cannot disagree about one
    record.

    *profile* is the profile document the run's plan pins
    (docs/spec/PROFILE-SPEC.md), paired with the pin as ``plan`` pairs it
    (``FLX-3109``, ``FLX-3110``). Given one, every emit that declares a
    fidelity block and has no committed slice is checked against it, and
    the run is scored (docs/spec/SCORECARD-SPEC.md): ``artifacts["scorecard"]``
    holds the document, and a missed threshold or a measured score below an
    emit's ``minCredibility`` fails the stage. A run whose plan pins a
    profile and is handed none reports its scorecard as not checked. A run
    whose plan pins none has no ``scorecard`` key, and nothing else differs.

    With a *sample* (``--sample``) each of those emits is compared with the
    profile on a sample of whole blocks of its records, each mean decided by
    its 95 % interval (docs/spec/SAMPLE-SPEC.md), and no scorecard is formed.
    It needs the profile, so a run that pins none, or a call handed none, is
    refused (``FLX-3109``) before any leg runs.
    """
    state = Path(state_dir) if state_dir is not None else Path(".flux")
    layout = RunLayout.for_state_dir(state, run_id).require_real_dirs()

    if not is_regular_file(layout.receipt_path) or not is_regular_file(layout.run_digest_path):
        missing = NoAppliedRun(run_dir=layout.run_dir)
        return StageResult(
            stage="verify",
            exit_code=1,
            _artifacts={"code": missing.code.code},
            stderr=str(missing),
            error_code=missing.code.code,
        )

    receipt = _read_json(layout.receipt_path)
    # The plan comes first, and a run directory without one is refused (FLX-2107):
    # `apply` reads one before it writes anything, and going on without it would skip
    # every check that runs through it. A directory named for another plan is a copy
    # (FLX-2113). run-digest.json is read after the plan, so that holds whatever the
    # file contains, and `ship` makes the same checks, so one directory gets one verdict.
    try:
        recorded_plan = load_recorded_plan(layout)
        kernel = plan_kernel(recorded_plan)
        run_digest_document = _read_json(layout.run_digest_path)
        require_run_digest_binds_plan(layout, recorded_plan, run_digest_document)
        read_column_provenance(layout, recorded_plan)
        # Parameter register: the sources of the run's numbers, read only through the plan.
        register = read_parameter_register(layout, recorded_plan)
    except FluxError as error:
        return StageResult(
            stage="verify",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )
    run_digest = digest_of_document(run_digest_document, subject="runDigest").value

    # Leg 0a. A mistyped --committed-slice key is a refusal, not a
    # silently dropped flag (see `require_known_slice_exposes`). Checked
    # before anything expensive: the caller's next command is the same one
    # with the id spelled right.
    try:
        require_known_slice_exposes(receipt, committed_slice or {})
    except FluxError as error:
        return StageResult(
            stage="verify",
            exit_code=2,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    # Leg 0b. The bundle argument. `verify`'s own signature took a bundle
    # directory and never read it, so `fluxsim verify /etc --run-id <id>`
    # reported the same verdict as the real bundle would. KERNEL-SPEC §11's
    # inputDigest is what binds a run to the documents it was given, and
    # recomputing it here is the whole content of "verify this run against
    # this bundle".
    try:
        current_input_digest = recompute_input_digest(bundle_directory, kernel=kernel)
    except FluxError as error:
        return StageResult(
            stage="verify",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )
    except OSError as error:
        moved = InputDigestMoved(planned=str(receipt.get("inputDigest")), current="unreadable")
        return StageResult(
            stage="verify",
            exit_code=1,
            _artifacts={"code": moved.code.code},
            stderr=f"{moved.code.code}: {bundle_directory} could not be read as a bundle: {error}",
            error_code=moved.code.code,
        )
    recorded_input_digest = str(receipt.get("inputDigest"))
    if current_input_digest != recorded_input_digest:
        moved = InputDigestMoved(planned=recorded_input_digest, current=current_input_digest)
        return StageResult(
            stage="verify",
            exit_code=1,
            _artifacts={
                "code": moved.code.code,
                "inputDigest": {
                    "recorded": recorded_input_digest,
                    "recomputed": current_input_digest,
                },
            },
            stderr=f"{moved.code.code}: {moved.detail} -- this run was not produced from "
            f"{Path(bundle_directory)}",
            error_code=moved.code.code,
        )

    # Leg 0c. The profile the run's plan pins, paired with the one handed
    # over exactly as `plan` pairs them. A profile with no pin, or another
    # profile, is refused here, before any leg: scoring the run against a
    # document it was not planned against would be a comparison of nothing.
    # A pin with no profile is reported as not checked by the legs below.
    try:
        paired = _paired_profile(recorded_plan, receipt, profile)
    except FluxError as error:
        return StageResult(
            stage="verify",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )
    if sample is not None and paired is None:
        return _unpaired_sample(recorded_plan)

    # The legs that do not touch the golden record run first, all of them,
    # unconditionally -- including when `--update-golden` was passed. A
    # `--update-golden` invocation that never evaluated row conformance,
    # RFC-04 or fidelity would let `fluxsim run --verify --update-golden`
    # report success having verified nothing but the golden write itself.
    # None of them returns early either: one invocation says everything it
    # found, so an operator fixing a corrupted run directory is not told
    # about it one leg per command.
    artifact_mismatches = component_mismatches(
        layout, receipt, run_digest_document.get("components", {})
    )
    artifact_detail = (
        str(RunArtifactsMoved(components=artifact_mismatches)) if artifact_mismatches else ""
    )

    row_problems = _row_conformance(layout, receipt)

    vectors = vectors_path or default_vectors_path()
    vector_failures, vector_messages = run_conformance_vectors(vectors)

    trust_anchor, self_attested = trust_anchor_for(signing_key, trust_key)
    against = _profile_legs(
        layout,
        receipt,
        recorded_plan,
        run_id,
        run_digest,
        paired,
        artifacts_ok=not artifact_mismatches,
        sample=sample,
    )
    fidelity_outcomes = _fidelity_leg(
        layout,
        receipt,
        committed_slice or {},
        trust_anchor=trust_anchor,
        self_attested=self_attested,
        against=against,
    )
    fidelity_failed = [
        expose_id
        for expose_id, outcome in fidelity_outcomes.items()
        if outcome.get("status") == "failed"
    ]
    artifacts_leg = {
        "ok": not artifact_mismatches,
        "movedComponents": list(artifact_mismatches),
    }

    # Leg 4b. The scorecard, for a run whose plan pins a profile: every emit
    # scored against it, with the plan's thresholds and each emit's
    # minCredibility read against the computed scores.
    scorecard_failures = against.failures()
    scorecard_extra = against.artifact()

    legs_ok = (
        not artifact_mismatches
        and not row_problems
        and vector_failures == 0
        and not fidelity_failed
        and not scorecard_failures
    )
    # FLX-2110 outranks the rest as the reported code: a run directory
    # that no longer reproduces its own digest makes every other leg a
    # statement about bytes nothing in the run produced.
    leg_error_code = RunArtifactsMoved(components=()).code.code if artifact_mismatches else None

    if update_golden:
        if not legs_ok:
            problems = [
                *([artifact_detail] if artifact_detail else []),
                *row_problems,
                *vector_messages[:5],
                *(_failure_summary(name, fidelity_outcomes[name]) for name in fidelity_failed),
                *scorecard_failures,
            ]
            return StageResult(
                stage="verify",
                exit_code=1,
                _artifacts={
                    "golden": {"updated": False, "problems": problems},
                    "artifactComponents": artifacts_leg,
                    "rowConformance": {"ok": not row_problems, "problems": row_problems},
                    "rfc04": {"failures": vector_failures, "messages": vector_messages},
                    "fidelity": fidelity_outcomes,
                    **scorecard_extra,
                },
                stderr="refusing to update the golden baseline -- a leg failed: "
                + "; ".join(problems),
                error_code=leg_error_code,
            )
        golden = _golden(
            layout,
            receipt["simulationId"],
            run_digest,
            run_digest_document["components"],
            update=True,
            reason=reason,
            declared_baseline=receipt.get("declaredGoldenBaseline"),
            generated_at=_now(),
            kernel_version=kernel.version,
        )
        exit_code = 0 if golden.get("updated") else 1
        if golden.get("updated"):
            golden["pasteIntoBundle"] = f'spec:\n  golden:\n    baselineDigest: "{run_digest}"'
        return StageResult(
            stage="verify",
            exit_code=exit_code,
            _artifacts={
                "golden": golden,
                "declaredBaselineDrift": bool(golden.get("specGoldenMismatch")),
                "artifactComponents": artifacts_leg,
                "rowConformance": {"ok": True, "problems": []},
                "rfc04": {"failures": 0, "messages": []},
                "fidelity": fidelity_outcomes,
                **scorecard_extra,
            },
            stdout="golden baseline updated" if golden.get("updated") else "",
            stderr="; ".join(golden.get("problems", [])) if not golden.get("updated") else "",
        )

    golden = _golden(
        layout,
        receipt["simulationId"],
        run_digest,
        run_digest_document["components"],
        update=False,
        reason=reason,
        declared_baseline=receipt.get("declaredGoldenBaseline"),
        generated_at=_now(),
        kernel_version=kernel.version,
    )

    reproducibility = golden.get("reproducibility", Reproducibility.NOT_CHECKED)
    verdict = VerifyVerdict(reproducibility=reproducibility, validity=Validity.NOT_ASSERTED)

    # KERNEL-SPEC §10 makes `golden.baselineDigest === runDigest` a
    # normative equality. A stage that prints the mismatch and still exits
    # 0 hands a CI gate -- and the --json envelope's own `"ok": true` --
    # a green light over a violated declaration, with the one fact that
    # matters buried three levels down in `artifacts.golden`. The verdict
    # is the exit code, so the mismatch belongs in it. A caller that has
    # decided a stale declared baseline is acceptable says so explicitly
    # with `--allow-declared-baseline-drift`.
    declared_baseline_drift = bool(golden.get("specGoldenMismatch"))
    ok = (
        reproducibility != Reproducibility.NOT_REPRODUCIBLE
        and legs_ok
        and (allow_declared_baseline_drift or not declared_baseline_drift)
    )

    headline = verdict.headline()
    stderr_parts: List[str] = []
    if not ok:
        if artifact_detail:
            stderr_parts.append(artifact_detail)
        stderr_parts.extend(row_problems)
        stderr_parts.extend(vector_messages[:5])
        stderr_parts.extend(_failed_legs(fidelity_outcomes, fidelity_failed))
        stderr_parts.extend(scorecard_failures)
    # **The regression names what moved, to a human.** `movedComponents`
    # is the answer to the only question a red `reproducible: no` raises,
    # and it used to reach `--json` callers alone: the human output was
    # the bare headline, with no baseline digest, no run digest and no
    # component name anywhere. This stage exists to say "this stopped
    # reproducing, and here is what moved"; saying it only in JSON is not
    # saying it.
    if reproducibility is Reproducibility.NOT_REPRODUCIBLE:
        moved_names = tuple(golden.get("movedComponents") or ())
        stderr_parts.append(
            f"golden regression: baseline {golden.get('baselineDigest')} -> this run "
            f"{golden.get('runDigest')}"
        )
        # One line for what moved -- the component list, or the explanation
        # of how a digest moves with no component behind it (see
        # INPUT_ONLY_DETAIL). Never both, and never the remedy twice.
        stderr_parts.append(
            f"moved: {', '.join(moved_names)}" if moved_names else INPUT_ONLY_DETAIL
        )
        stderr_parts.append(GOLDEN_REMEDY)
    # A moved digest over an unmoved run: say so in the headline, not only
    # in the artifacts. See INPUT_ONLY_DETAIL.
    if reproducibility is Reproducibility.NOT_REPRODUCIBLE and golden.get("outputsIdentical"):
        headline += " | inputs moved, every component identical"
    # KERNEL-SPEC section 10 makes `golden.baselineDigest === runDigest` a
    # normative equality; this stage's own pass/fail signal is the
    # out-of-band <state_dir>/golden/ record (see `_golden`'s docstring),
    # but a declared-vs-actual mismatch must still reach the reader,
    # regardless of exit code -- silence here would let "the bundle
    # carries a stale placeholder" and "this run is not reproducible"
    # look identical.
    if declared_baseline_drift:
        headline += " | declared baseline: does not match"
        stderr_parts.append(
            "declared baseline: does not match -- the bundle's Simulation "
            "spec.golden.baselineDigest is "
            f"{receipt.get('declaredGoldenBaseline')}, this run's runDigest is "
            f"{run_digest}"
            # The recovery hint used to be appended only when the flag was
            # already passed -- so the one invocation that actually failed
            # over this was the one told nothing about how to proceed.
            + (
                " (allowed by --allow-declared-baseline-drift)"
                if allow_declared_baseline_drift
                else ' -- re-record it with `--update-golden --reason "<why>"` and paste the '
                "printed digest into the bundle's spec.golden, or accept the drift for this "
                "run with `--allow-declared-baseline-drift`"
            )
        )

    # A golden regression is a catalogued failure like any other, and it
    # used to report `"code": null` -- the one leg of this stage whose
    # `--json` envelope told a caller nothing it could branch on, and the
    # one whose remedy (`--update-golden --reason`) is a specific command.
    # FLX-2110 still outranks it: a run directory that no longer
    # reproduces its own digest makes the baseline comparison a statement
    # about bytes nothing in the run produced.
    error_code = leg_error_code
    if error_code is None and reproducibility is Reproducibility.NOT_REPRODUCIBLE:
        error_code = FLX_2114.code

    return StageResult(
        stage="verify",
        exit_code=0 if ok else 1,
        _artifacts={
            "headline": headline,
            "reproducibility": reproducibility.value,
            "validity": verdict.validity.value,
            "verdict": {
                "reproducibility": reproducibility.value,
                "validity": verdict.validity.value,
            },
            "golden": golden,
            "declaredBaselineDrift": declared_baseline_drift,
            "artifactComponents": artifacts_leg,
            "rowConformance": {"ok": not row_problems, "problems": row_problems},
            "rfc04": {"failures": vector_failures, "messages": vector_messages},
            "fidelity": fidelity_outcomes,
            **scorecard_extra,
            **(
                {"parameterRegister": register.summary.as_document()}
                if register is not None
                else {}
            ),
        },
        stdout="\n".join(
            [
                headline,
                *_leg_lines(
                    golden,
                    artifact_mismatches=artifact_mismatches,
                    row_problems=row_problems,
                    vector_failures=vector_failures,
                    fidelity_outcomes=fidelity_outcomes,
                ),
                *([f"  {render_register_line(register.summary)}"] if register is not None else []),
                *against.lines(),
            ]
        ),
        stderr="\n".join(stderr_parts),
        error_code=error_code,
    )
