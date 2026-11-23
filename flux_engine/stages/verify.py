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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    FrozenSet,
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
    json_lines,
    read_bytes_nofollow,
    read_text_nofollow,
    require_real_directory,
    write_text_atomic,
)
from flux_engine.seam.emit import EMIT_LABEL
from flux_engine.spec import default_vectors_path
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages._parallel import map_in_pool
from flux_engine.stages.apply import (
    load_recorded_plan,
    plan_kernel,
    require_run_digest_binds_plan,
)
from flux_engine.stages.errors import (
    FLX_2109,
    FLX_2110,
    FLX_2114,
    InputDigestMoved,
    NoAppliedRun,
    RunArtifactsMoved,
    UnknownCommittedSliceExpose,
)
from flux_engine.stages.layout import (
    EVENTS_LEDGER_COMPONENTS,
    POPULATION_LEDGER_COMPONENTS,
    RUN_FILE_REMEDY,
    RunLayout,
    read_json_object,
)
from flux_engine.stages.plan import read_column_provenance, read_parameter_register

if TYPE_CHECKING:
    from flux_engine.concordance.calendar_lines import CalendarChecks
    from flux_engine.concordance.compiler import CompiledFidelity
    from flux_engine.concordance.exposure_lines import ExposureChecks
    from flux_engine.concordance.queries import QueriesDimension
    from flux_engine.concordance.sampling import SampledEvaluation, SampleRequest
    from flux_engine.concordance.scorecard import ContractShape, Scorecard, TableInput
    from flux_engine.concordance.shape import EmitSummary, ShapeReport
    from flux_engine.concordance.verifier import FidelityEvaluation, PropertyOutcome
    from flux_engine.seam.consumers import Ceilings
    from flux_engine.seam.profile import PinnedProfile
    from flux_engine.stages._module_checks import (
        EmitModule,
        ModuleChecks,
        ModuleJob,
        ModulePlan,
    )
    from flux_engine.stages._queries import QueriesLeg, QueriesPlan
    from flux_stats.profile import LinkGrouping

__all__ = [
    "GOLDEN_REMEDY",
    "GOLDEN_SNAPSHOT_SCHEMA_VERSION",
    "INPUT_ONLY_DETAIL",
    "component_mismatches",
    "packed_emit",
    "recompute_components",
    "recompute_input_digest",
    "require_known_slice_exposes",
    "run_calendar_checks",
    "run_exposure_checks",
    "shaped_columns",
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


def _emit_component_digest(
    layout: RunLayout, expose_id: str, data: Optional[bytes] = None
) -> Optional[str]:
    """Recompute one emit stream's ``outputDigest`` from the bytes on disk (§8).

    ``outputDigest = sha256(b"flux-emit/1\\x00" + JCS(header) + b"\\x00" +
    rows)`` where *rows* is the framed row block -- which is exactly what
    ``emits/<exposeId>.ndjson`` holds, byte for byte, because ``apply``
    writes :meth:`~flux_engine.seam.emit.EmitWriter.stream_bytes` into it
    unchanged. The header is the one the emit's seam attestation records.

    ``None`` when either file is missing: the caller reports that as a
    component that does not reproduce, not as a component that agrees.
    A caller that has already read the stream passes its bytes as *data*, so the
    digest is taken over the bytes it holds and the file is not read a second time.
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
        EMIT_LABEL
        + b"\x00"
        + canonical_bytes(header)
        + b"\x00"
        + (read_bytes_nofollow(emit_path) if data is None else data)
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
    for line in json_lines(read_text_nofollow(path)):
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
    """Decode every row of an emit stream; a bad line is a problem, not an exception.

    The lines are decoded in one call: they are joined into one JSON array, which is the rows
    when every line is one JSON value. Anything else (a line that is not JSON, or that holds
    more than one value, or a split the lines make differently) is decoded a line at a time,
    which is what names the lines that are not JSON. A file that is not UTF-8 is one problem.
    """
    try:
        text = read_text_nofollow(emit_path)
    except UnicodeDecodeError:
        # Not text the engine wrote: a problem with the rows, as a line that is not JSON is.
        return [], [f"{expose_id}: {emit_path.name} is not UTF-8 text"]
    all_lines = json_lines(text)
    lines = [line for line in all_lines if line]
    try:
        decoded = json.loads("[" + ",".join(lines) + "]")
    except (json.JSONDecodeError, RecursionError):
        decoded = None
    if isinstance(decoded, list) and len(decoded) == len(lines):
        return decoded, []
    rows: List[List[Any]] = []
    problems: List[str] = []
    for index, line in enumerate(all_lines):
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            problems.append(f"{expose_id}: line {index + 1} is not JSON")
    return rows, problems


class RunRows:
    """The emits of one applied run, each decoded once for every leg that reads its rows.

    Row conformance, the properties compared with the pinned profile and the shape flags all
    read an emit's rows. They used to decode the file each; here the file is decoded the first
    time it is asked for, and what it decoded to (rows, or the problems) is handed to the rest.
    """

    def __init__(self, layout: RunLayout) -> None:
        self.layout = layout
        self._decoded: Dict[str, Tuple[List[List[Any]], List[str]]] = {}
        self._tables: Dict[str, Any] = {}

    def decoded(self, expose_id: str) -> Tuple[List[List[Any]], List[str]]:
        """The emit's decoded rows and the problems decoding met, as :func:`_decoded_rows` gives them."""
        if expose_id not in self._decoded:
            self._decoded[expose_id] = _decoded_rows(self.layout.emit_path(expose_id), expose_id)
        return self._decoded[expose_id]

    def table(self, expose_id: str) -> Any:
        """The emit's rows keyed by column (a :class:`DecodedRows`), or ``(code, reason)`` of a refusal."""
        if expose_id not in self._tables:
            self._tables[expose_id] = _synthetic_rows(self.layout, expose_id, self)
        return self._tables[expose_id]


def _declared_columns(layout: RunLayout, expose_id: str) -> Optional[List[str]]:
    """The column names the emit's seam attestation declares, or ``None``."""
    path = layout.seam_attestation_path(expose_id)
    if not is_regular_file(path):
        return None
    try:
        return [str(name) for name in _read_json(path)["columns"]]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class _EmitTask:
    """What one worker needs to read one emit: where it is, and what to check it against."""

    layout: RunLayout
    expose_id: str
    entry: Mapping[str, Any]
    #: The pinned profile when the emit is to be compared with it and counted for shape.
    pinned: Optional["PinnedProfile"] = None
    #: The emit's rules and key columns for the module checks (ADR 0018), when it has any.
    module: Optional["ModuleJob"] = None
    #: The emit's hidden columns, which its stream does not hold (ADR 0025 section 8).
    hidden_columns: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _EmitPass:
    """What one emit's single read found: its row problems, its profile check and its shape counts."""

    expose_id: str
    row_problems: List[str]
    check: Optional[ProfileCheck] = None
    summary: Optional["EmitSummary"] = None
    module: Optional["EmitModule"] = None
    #: The emit's calendar lines, when the pinned profile publishes a calendar for it.
    calendar: Optional["CalendarChecks"] = None


def _emit_pass(task: _EmitTask) -> _EmitPass:
    """Read one emit once: decode it, check its rows, compare it with the profile, count its shape.

    The unit of parallel work. Every step reads the emit through one :class:`RunRows`, so the
    file is decoded once however many steps there are, and nothing here touches another emit.
    """
    reader = RunRows(task.layout)
    problems = _conformance_problems(task.layout, task.expose_id, task.entry, reader)
    module = _module_pass(reader, task.expose_id, task.module) if task.module is not None else None
    if task.pinned is None:
        return _EmitPass(task.expose_id, problems, module=module)
    return _EmitPass(
        task.expose_id,
        problems,
        _profile_check(task.layout, task.expose_id, task.entry, task.pinned, reader),
        _shape_summary(task.layout, task.expose_id, task.pinned, reader),
        module,
        _calendar_pass(task.layout, task.expose_id, task.pinned, reader, task.hidden_columns),
    )


def _calendar_pass(
    layout: RunLayout,
    expose_id: str,
    pinned: "PinnedProfile",
    reader: "RunRows",
    hidden_columns: Tuple[str, ...],
) -> Optional["CalendarChecks"]:
    """One emit's calendar lines (ADR 0025 section 8), on the columns this pass has already decoded.

    ``None`` when the pinned profile publishes no calendar for the emit, so a run whose profile
    holds none is read, and reported, exactly as it was.
    """
    from flux_engine.concordance.calendar_lines import calendar_for_emit

    table = pinned.profile.tables.get(expose_id)
    if table is None or not table.calendar:
        return None
    rows, problem = reader.table(expose_id)
    types: Dict[str, str] = {}
    try:
        attestation = _read_json(layout.seam_attestation_path(expose_id))
        types = dict(
            zip(
                (str(name) for name in attestation["columns"]),
                (str(name) for name in attestation["types"]),
                strict=True,
            )
        )
    except (*_UNREADABLE, FluxError):
        pass
    return calendar_for_emit(
        expose_id,
        table.calendar,
        None if problem is not None else rows,
        types,
        hidden_columns,
        unreadable=None if problem is None else problem[1],
    )


def _module_pass(reader: "RunRows", expose_id: str, job: "ModuleJob") -> "EmitModule":
    """One emit's share of the module checks, on the columns this pass has already decoded."""
    from flux_engine.stages._module_checks import module_pass

    return module_pass(reader, expose_id, job)


def _read_only_mapping(items: Mapping[Any, Any]) -> Any:
    from types import MappingProxyType

    return MappingProxyType(dict(items))


def _reduce_read_only_mapping(proxy: Any) -> Any:
    return _read_only_mapping, (dict(proxy),)


def _pickle_read_only_mappings() -> None:
    """Let the read-only mappings a profile and a compiled block hold cross to a worker and back.

    A ``MappingProxyType`` does not pickle by default. It is registered here, in this process
    and (as the pool's initializer) in each worker, as the dictionary it shows.
    """
    from multiprocessing.reduction import ForkingPickler
    from types import MappingProxyType

    ForkingPickler.register(MappingProxyType, _reduce_read_only_mapping)


def _map_in_pool(
    function: Callable[[_EmitTask], _EmitPass], tasks: Sequence[_EmitTask], jobs: int
) -> List[_EmitPass]:
    """Map *function* over *tasks*: in worker processes when *jobs* allows, else here, in order.

    The one place ``verify`` makes a pool, so that a pool shared with the rest of the run can
    replace it in this one function. The results are in the order of *tasks*, so a verdict never
    depends on which worker finished first.
    """
    _pickle_read_only_mappings()
    return map_in_pool(function, tasks, jobs, initializer=_pickle_read_only_mappings)


@dataclass(frozen=True)
class _EmitReads:
    """Every emit of the run, each read once: by :func:`_read_emits`."""

    passes: Tuple[_EmitPass, ...]
    #: Whether the emits were also compared with the pinned profile and counted for shape.
    profiled: bool

    def row_problems(self) -> List[str]:
        return [problem for each in self.passes for problem in each.row_problems]

    def checks(self) -> Dict[str, ProfileCheck]:
        return {each.expose_id: each.check for each in self.passes if each.check is not None}

    def summaries(self) -> List["EmitSummary"]:
        return [each.summary for each in self.passes if each.summary is not None]

    def modules(self) -> Dict[str, "EmitModule"]:
        return {each.expose_id: each.module for each in self.passes if each.module is not None}

    def calendars(self) -> "CalendarChecks":
        """Every emit's calendar lines, in the order of the emits."""
        from flux_engine.concordance.calendar_lines import CalendarChecks

        found = CalendarChecks()
        for each in self.passes:
            if each.calendar is not None:
                found = found.merged(each.calendar)
        return found


def _read_emits(
    layout: RunLayout,
    receipt: Mapping[str, Any],
    pinned: Optional["PinnedProfile"],
    *,
    jobs: int,
    module: Optional["ModulePlan"] = None,
    hidden_columns: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> _EmitReads:
    """Read every emit once, in parallel when *jobs* allows (see :func:`_emit_pass`)."""
    tasks = [
        _EmitTask(
            layout,
            expose_id,
            entry,
            pinned,
            module.jobs.get(expose_id) if module else None,
            (hidden_columns or {}).get(expose_id, ()),
        )
        for expose_id, entry in sorted(receipt.get("emits", {}).items())
    ]
    return _EmitReads(tuple(_map_in_pool(_emit_pass, tasks, jobs)), pinned is not None)


def _conformance_problems(
    layout: RunLayout, expose_id: str, entry: Mapping[str, Any], reader: RunRows
) -> List[str]:
    """One emit's row conformance: its file, its row count and the width of every row."""
    problems: List[str] = []
    emit_path = layout.emit_path(expose_id)
    if not is_regular_file(emit_path):
        problems.append(f"{expose_id}: no emit file at {emit_path}")
        return problems
    rows, decode_problems = reader.decoded(expose_id)
    problems.extend(decode_problems)
    if decode_problems:
        return problems
    if len(rows) != entry["rows"]:
        problems.append(f"{expose_id}: receipt declares {entry['rows']} rows, file has {len(rows)}")
        return problems
    # The declared width, not merely a shared one. Rows that all carry
    # the same wrong number of values agree with each other and match
    # nothing the contract declared, which is exactly the corruption
    # this leg exists to catch.
    columns = _declared_columns(layout, expose_id)
    if columns is None:
        problems.append(f"{expose_id}: no readable seam attestation to check row width against")
        return problems
    widths = {len(row) for row in rows}
    wrong = sorted(width for width in widths if width != len(columns))
    if wrong:
        problems.append(
            f"{expose_id}: rows carry {wrong} values where the attestation "
            f"declares {len(columns)} columns"
        )
    return problems


def _row_conformance(
    layout: RunLayout, receipt: Dict[str, Any], reader: Optional[RunRows] = None
) -> List[str]:
    reader = reader or RunRows(layout)
    problems: List[str] = []
    for expose_id, entry in sorted(receipt.get("emits", {}).items()):
        problems.extend(_conformance_problems(layout, expose_id, entry, reader))
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
    #: The emit's contract columns and declared relationships, for coverage.
    contract: Optional["ContractShape"] = None
    #: The groupings of the properties grouped by a link, by their ``<relationship>.<column>`` key.
    links: Mapping[str, "LinkGrouping"] = field(default_factory=dict)
    #: Notes that do not fail the check: what the profile publishes that nothing declared reads.
    notes: Tuple[str, ...] = ()

    def table_input(self) -> "TableInput":
        """This emit as the scorecard reads it."""
        from flux_engine.concordance.scorecard import TableInput

        return TableInput(
            expose_id=self.expose_id,
            compiled=self.compiled,
            evaluation=self.evaluation,
            min_credibility=self.min_credibility,
            declares_fidelity=self.compiled is not None or self.evaluation is not None,
            contract=self.contract,
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
            "status": _fidelity_status(evaluation),
            "reference": "profile",
            "detail": profile_check_detail(evaluation),
            "refusals": [refusal.to_document() for refusal in evaluation.refusals],
            "outside": [
                _outside_document(outcome, refused=outcome.property_id in refused)
                for outcome in evaluation.outside
            ],
            "censored": [outcome.property_id for outcome in evaluation.censored],
            **({"notes": list(self.notes)} if self.notes else {}),
        }


def _profile_check(
    layout: RunLayout,
    expose_id: str,
    entry: Mapping[str, Any],
    pinned: "PinnedProfile",
    reader: Optional[RunRows] = None,
) -> ProfileCheck:
    """One emit compared with the pinned profile (the body of :func:`_profile_checks`)."""
    from flux_engine.concordance.compiler import compile_fidelity
    from flux_engine.concordance.errors import FLX_8001
    from flux_engine.concordance.linked import join_linked_columns, link_groupings
    from flux_engine.concordance.predicate import expose_from_contract
    from flux_engine.stages._scorecard import (
        contract_shape,
        evaluate_against_profile,
        min_credibility,
        refused_evaluation,
        unscored_note,
        unused_statistics,
    )

    fidelity_input_path = layout.fidelity_input_path(expose_id)
    if not entry.get("declaresFidelity"):
        return ProfileCheck(expose_id=expose_id)
    if not is_regular_file(fidelity_input_path):
        # No compiled block to score: the scorecard says the table was
        # refused, and why, rather than leaving it out.
        reason = f"no {fidelity_input_path.name} recorded for this run"
        return ProfileCheck(
            expose_id=expose_id,
            evaluation=refused_evaluation(expose_id, FLX_8001.code, reason),
        )
    shape: Optional["ContractShape"] = None
    try:
        fidelity_input = _read_json(fidelity_input_path)
        contract_document = fidelity_input["contract"]
        shape = contract_shape(contract_document, expose_id)
        expose = expose_from_contract(contract_document, expose_id)
        compiled = compile_fidelity(
            contract=contract_document, emit=fidelity_input["emit"], expose=expose
        )
        floor = min_credibility(fidelity_input)
        synthetic_rows, problem = _table_of(layout, expose_id, reader)
        links = link_groupings(contract_document, expose_id, compiled.block, expose.column_names)
        if problem is None:
            synthetic_rows = join_linked_columns(
                synthetic_rows, links, lambda parent: _table_of(layout, parent, reader)
            )
    except FluxError as error:
        return ProfileCheck(
            expose_id=expose_id,
            evaluation=refused_evaluation(expose_id, error.code.code, error.detail),
            contract=shape,
        )
    except _UNREADABLE as error:
        # A record nothing could parse: named by its kind, never quoted.
        reason = f"{fidelity_input_path.name} cannot be read ({type(error).__name__})"
        return ProfileCheck(
            expose_id=expose_id,
            evaluation=refused_evaluation(expose_id, FLX_8001.code, reason),
            contract=shape,
        )
    unread = unused_statistics(expose_id, pinned) if not compiled.properties else ()
    return ProfileCheck(
        expose_id=expose_id,
        compiled=compiled,
        evaluation=(
            refused_evaluation(expose_id, *problem)
            if problem is not None
            else evaluate_against_profile(compiled, pinned, synthetic_rows, links)
        ),
        min_credibility=floor,
        contract=shape,
        links=links,
        notes=(unscored_note(unread),) if unread else (),
    )


def evaluate_profile_rows(
    layout: RunLayout,
    compiled: "CompiledFidelity",
    pinned: "PinnedProfile",
    rows: Any,
    links: Mapping[str, "LinkGrouping"],
) -> "FidelityEvaluation":
    """*rows* of an emit compared with the pinned profile, the linked columns of *links* joined in.

    A property grouped by a link reads the linked expose's rows of the same run, so the
    comparison ``ship`` repeats over the emit's rows reads them as :func:`_profile_check` does.
    """
    from flux_engine.concordance.linked import join_linked_columns
    from flux_engine.stages._scorecard import evaluate_against_profile, refused_evaluation

    try:
        rows = join_linked_columns(rows, links, lambda parent: _synthetic_rows(layout, parent))
    except FluxError as error:
        return refused_evaluation(compiled.block.expose_id, error.code.code, error.detail)
    return evaluate_against_profile(compiled, pinned, rows, links)


def _profile_checks(
    layout: RunLayout,
    receipt: Mapping[str, Any],
    pinned: "PinnedProfile",
    reader: Optional[RunRows] = None,
) -> Dict[str, ProfileCheck]:
    """Compare every emit that declares a fidelity block with the pinned profile.

    The synthetic side is read as the slice path reads it, through the
    emit's seam attestation and the catalogued row guards, so a stream that
    cannot be read is a refusal of that emit (``FLX-8001``), never an
    exception that would cost the other emits their answer.
    """
    return {
        expose_id: _profile_check(layout, expose_id, entry, pinned, reader)
        for expose_id, entry in sorted(receipt.get("emits", {}).items())
    }


#: What reading one emit's run records can raise besides a catalogued refusal:
#: a file gone or unreadable, text that is not JSON, or JSON of the wrong shape.
_UNREADABLE = (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError)


def _table_of(
    layout: RunLayout, expose_id: str, reader: Optional[RunRows]
) -> Tuple[Any, Optional[Tuple[str, str]]]:
    """:func:`_synthetic_rows` through the run's reader, which decodes each emit once."""
    if reader is None:
        return _synthetic_rows(layout, expose_id)
    return reader.table(expose_id)


def _synthetic_rows(
    layout: RunLayout, expose_id: str, reader: Optional[RunRows] = None
) -> Tuple[Any, Optional[Tuple[str, str]]]:
    """An emit's rows keyed by column, or ``(code, reason)`` when they cannot be read.

    Through the catalogued guards the slice path reads them with: a line that
    is not JSON, or a row whose width is not the header's, is a refusal of
    this emit (``FLX-8001``), never an exception. The rows are a
    :class:`~flux_engine.concordance.decoded.DecodedRows`: a sequence of the
    mappings the slice path reads, whose columns decode once for the readers
    that take them.
    """
    from flux_engine.concordance.decoded import DecodedRows
    from flux_engine.concordance.errors import FLX_8001

    emit_path = layout.emit_path(expose_id)
    if not is_regular_file(emit_path):
        return [], (FLX_8001.code, f"no emit file {emit_path.name} for this run")
    emitted_rows, decode_problems = (
        reader.decoded(expose_id) if reader is not None else _decoded_rows(emit_path, expose_id)
    )
    if decode_problems:
        return [], (FLX_8001.code, "; ".join(decode_problems))
    attestation_path = layout.seam_attestation_path(expose_id)
    try:
        attestation = _read_json(attestation_path)
        names = [str(name) for name in attestation["columns"]]
        try:
            # Every row as wide as the header, which is what packed_emit's refusal checks.
            return DecodedRows(names, emitted_rows), None
        except (ValueError, TypeError):
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

    :func:`_decoded_rows` reads the text whole and splits it at ``"\\n"`` only
    (:func:`flux_engine.safeio.json_lines`), as a JSON-lines file is split.
    Opened with ``newline="\\n"``, the file ends a line there and nowhere else and
    keeps every other character, so both readers count the same records.
    """
    with emit_path.open(encoding="utf-8", newline="\n") as stream:
        for line in stream:
            yield line[:-1] if line.endswith("\n") else line


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
    from flux_engine.concordance.errors import FLX_5031, FLX_8001
    from flux_engine.concordance.linked import link_groupings
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
            if link_groupings(contract_document, expose_id, compiled.block, expose.column_names):
                reason = (
                    "a property grouped by a link reads another emit's rows, which a sample of "
                    "this emit's blocks does not hold"
                )
                evaluations[expose_id] = _refused_sample(expose_id, FLX_5031.code, reason)
                continue
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


def _shape_summary(
    layout: RunLayout, expose_id: str, pinned: "PinnedProfile", reader: Optional[RunRows] = None
) -> "EmitSummary":
    """One emit's shape counts against the pinned profile (the body of :func:`shape_report`)."""
    from flux_engine.concordance.shape import refused_emit, summarise_emit

    if expose_id not in pinned.profile.tables:
        return summarise_emit(pinned.profile, expose_id, ())
    rows, problem = _table_of(layout, expose_id, reader)
    if problem is not None:
        return refused_emit(expose_id, *problem)
    return summarise_emit(pinned.profile, expose_id, rows)


def shape_report(
    layout: RunLayout,
    receipt: Mapping[str, Any],
    pinned: "PinnedProfile",
    reader: Optional[RunRows] = None,
) -> "ShapeReport":
    """The shape flags for every emit of the run, against the pinned profile.

    SCORECARD-SPEC.md §6. Every emit whose table the profile describes is
    read, one at a time and as the profile path reads it
    (:func:`_synthetic_rows`), whether or not it declares a fidelity block.
    An emit whose rows cannot be read is refused on the report, with its
    reason, and the others are still checked. ``ship`` takes the same
    report into its scorecard.
    """
    from flux_engine.concordance.shape import decide_shape

    return decide_shape(
        [
            _shape_summary(layout, expose_id, pinned, reader)
            for expose_id in sorted(receipt.get("emits", {}))
        ]
    )


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
    #: The rules checked on the rows (ADR 0018); empty for a bundle that declares none.
    module: Optional["ModuleChecks"] = None
    #: The calendar lines of the profile's published calendars (ADR 0025 section 8); ``None`` when it publishes none.
    calendar: Optional["CalendarChecks"] = None
    #: The exposure lines of the profile's published exposures (ADR 0025 section 8); ``None`` when it publishes none.
    exposure: Optional["ExposureChecks"] = None

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

    def settled(self) -> "ProfileLegs":
        """These legs with the scorecard's ``met`` read from every line that fails the run.

        The lines are formed after the scorecard is (the exposure lines read the bundle, and a
        calendar is gated by it), so the scorecard and its ``scorecard`` leg are set here, once all
        of them are known. A run no line fails is returned as it is.
        """
        from flux_engine.stages._scorecard import line_failures, with_line_failures

        failed = line_failures(self.module, self.calendar, self.exposure)
        if self.scorecard is None or not failed:
            return self
        scorecard = with_line_failures(self.scorecard, failed)
        leg = self.leg
        if leg is not None and leg.get("status") == "computed":
            leg = {**leg, "met": scorecard.met, "document": scorecard.to_document()}
        return replace(self, scorecard=scorecard, leg=leg)

    def failures(self) -> List[str]:
        """A missed threshold or ``minCredibility``, or a rule off its band, one line each: each fails the stage."""
        found = self.scorecard.failure_lines() if self.scorecard is not None else []
        found += self.module.failures() if self.module is not None else []
        found = found + (self.calendar.failures() if self.calendar is not None else [])
        return found + (self.exposure.failures() if self.exposure is not None else [])

    def artifact(self) -> Dict[str, Any]:
        """The ``scorecard`` artifact key, or nothing for a run that pins no profile."""
        extra: Dict[str, Any] = {"scorecard": self.leg} if self.leg is not None else {}
        if self.module is not None:
            extra.update(self.module.artifact())
        if self.calendar is not None:
            extra.update(self.calendar.artifact())
        if self.exposure is not None:
            extra.update(self.exposure.artifact())
        if self.sample is not None and self.sampled is not None:
            extra["sample"] = {
                **self.sample.to_document(),
                "emits": {name: found.to_document() for name, found in self.sampled.items()},
            }
        return extra

    def lines(self) -> List[str]:
        """The rules' and the scorecard's lines under the legs: nothing for a run with neither."""
        found = self.module.lines() if self.module is not None else []
        if self.calendar is not None:
            found = found + self.calendar.text()
        if self.exposure is not None:
            found = found + self.exposure.text()
        if self.scorecard is not None:
            return found + [f"  {line}" for line in self.scorecard.summary_lines()]
        if self.leg is not None:
            return found + [f"  scorecard: {self.leg['status']} -- {self.leg['detail']}"]
        return found


#: Why a pinned run handed its profile is not scored: its bytes are not the run's.
_ARTIFACTS_MOVED_DETAIL = (
    "the run's artifacts do not reproduce run-digest.json (FLX-2110), so nothing was "
    "compared with the profile; re-run apply"
)


def _count_queries(
    plan: Optional["QueriesPlan"],
    loaded: _Loaded,
    paired: Optional["PinnedProfile"],
    *,
    artifacts_ok: bool,
) -> Optional["QueriesLeg"]:
    """Leg 4a: count the consumers' queries on the run, or ``None`` for a bundle that declares none."""
    if plan is None:
        return None
    from flux_engine.stages._queries import count_queries

    return count_queries(
        plan,
        loaded.layout,
        loaded.receipt,
        loaded.recorded_plan,
        pinned=paired,
        artifacts_ok=artifacts_ok,
    )


def _plan_module_checks(
    bundle_directory: Union[str, Path],
    layout: RunLayout,
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    *,
    artifacts_ok: bool,
) -> "ModulePlan":
    """What the module checks need of the run (:mod:`flux_engine.stages._module_checks`), imported when used."""
    from flux_engine.stages._module_checks import plan_module_checks

    return plan_module_checks(
        bundle_directory, layout, receipt, plan_document, artifacts_ok=artifacts_ok
    )


def _finish_module_checks(
    plan: "ModulePlan", results: Mapping[str, "EmitModule"], layout: RunLayout
) -> "ModuleChecks":
    """Join the emits' module checks, in the order of the emits."""
    from flux_engine.stages._module_checks import finish_module_checks

    return finish_module_checks(plan, results, layout)


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
    emits: Optional[_EmitReads] = None,
    module: Optional["ModuleChecks"] = None,
    queries: Optional["QueriesDimension"] = None,
) -> ProfileLegs:
    """Leg 4b: compare every emit with the paired profile and score the run.

    The pairing itself (:func:`_paired_profile`) runs before any leg and
    refuses there. A pinned run handed no profile reports its scorecard as
    not checked, with the flag that runs it; one whose artifacts do not
    reproduce its digest is not scored at all, since its rows are not the
    run's. With a *sample*, each emit is compared on a sample of its blocks
    instead and no scorecard is formed from it.
    """
    from flux_engine.concordance.shape import decide_shape
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
    read = emits if emits is not None and emits.profiled else None
    checks = read.checks() if read is not None else _profile_checks(layout, receipt, paired)
    calendar = _calendar_checks(read, paired, plan_document)
    scorecard = build_run_scorecard(
        plan_document=plan_document,
        run_id=run_id,
        run_digest=run_digest,
        pinned=paired,
        tables=[check.table_input() for check in checks.values()],
        module=module,
        calendar=calendar,
        queries=queries,
    )
    # The shape flags inform; nothing in the leg reads them (SCORECARD-SPEC.md §6).
    shape = (
        decide_shape(read.summaries())
        if read is not None
        else shape_report(layout, receipt, paired)
    )
    scorecard = replace(scorecard, shape=shape)
    return ProfileLegs(
        pinned=pinned,
        checks=checks,
        scorecard=scorecard,
        leg={"status": "computed", "met": scorecard.met, "document": scorecard.to_document()},
        calendar=calendar,
    )


def _calendar_checks(
    read: Optional[_EmitReads], paired: "PinnedProfile", plan_document: Mapping[str, Any]
) -> Optional["CalendarChecks"]:
    """The calendar lines of every published calendar (ADR 0025 section 8), or ``None`` when there is none.

    The written emits' lines were formed as each was read; the calendars of hidden tables, which no
    stream holds, are listed as not scored here from the recorded plan.
    """
    from flux_engine.concordance.calendar_lines import hidden_not_scored

    if read is None:
        return None
    found = read.calendars().merged(hidden_not_scored(paired.profile, plan_document))
    return None if found.empty else found


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
        "status": _fidelity_status(evaluation),
        "refusals": [refusal.to_document() for refusal in evaluation.refusals],
        "outside": [
            _outside_document(outcome, refused=outcome.property_id in refused)
            for outcome in evaluation.outside
        ],
        **_slice_detail(evaluation),
    }


def _slice_detail(evaluation: "FidelityEvaluation") -> Dict[str, Any]:
    """What a committed slice's leg says beyond its status: a block with no property, or the
    properties it named and did not compare (a rank correlation, whose ``S`` and ``A4`` do not add
    up across blocks, ADR 0035 decision 7)."""
    from flux_engine.stages._scorecard import NOT_SCORED_DETAIL

    found: Dict[str, Any] = {}
    if evaluation.not_compared:
        found["notCompared"] = list(evaluation.not_compared)
        if evaluation.unscored:
            found["detail"] = "not compared with a committed slice: " + ", ".join(
                evaluation.not_compared
            )
    elif evaluation.unscored:
        found["detail"] = NOT_SCORED_DETAIL
    return found


def _fidelity_status(evaluation: "FidelityEvaluation") -> str:
    """``failed``, ``not scored`` for a block with no property to compare, or ``passed``."""
    if evaluation.unscored:
        return "not scored"
    return "passed" if evaluation.within else "failed"


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
    refusal_codes = {
        refusal["propertyId"]: refusal["code"]
        for refusal in reversed(outcome.get("refusals", ()))
        if refusal.get("propertyId")
    }
    for entry in outcome.get("outside", ()):
        subject = f"{entry['property']} of {', '.join(entry['columns'])}"
        where = ""
        if "groupBy" in entry:
            subject += f", grouped by {entry['groupBy']}"
            ordinals = entry["groupOrdinals"]
            if ordinals and not entry["refused"]:
                noun = "groups" if len(ordinals) > 1 else "group"
                where = f" in {noun} " + ", ".join(f"#{ordinal}" for ordinal in ordinals)
        # A refused property was never compared, so it has no margin to be within or outside of.
        code = refusal_codes.get(entry["propertyId"])
        verdict = (
            f"refused, not compared ({code})"
            if code and entry["refused"]
            else "refused, not compared" if entry["refused"] else "outside the declared margin"
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
    """The estimates a sampled check found outside the band, or could not place, one line each."""
    labels = {"histogram": "histogram", "quantile": "quantile bins"}
    return [
        f"property {found['propertyId']} ({labels.get(found.get('kind'), 'mean')} of "
        f"{', '.join(found['columns'])}): "
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
        lines.extend(f"    note: {note}" for note in outcome.get("notes", ()))
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
    jobs: int = 1,
    held_out: Union[str, Path, None] = None,
    max_query_seconds: Optional[int] = None,
    max_query_memory: Optional[int] = None,
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
    profile on a sample of whole blocks of its records, each mean, histogram and
    quantile with bins decided by its 95 % interval (docs/spec/SAMPLE-SPEC.md), and no scorecard is formed.
    It needs the profile, so a run that pins none, or a call handed none, is
    refused (``FLX-3109``) before any leg runs.

    *jobs* is how many processes may read the run's emits at once (each emit is decoded, its
    rows checked and its shape counted in one worker); it never changes a verdict or a byte.

    A bundle that declares ``consumers`` has every query counted on the run, and the scorecard's
    ``queries`` dimension holds the rates, the calibration queries apart from the held-out ones
    (ADR 0028 section 7, :mod:`flux_engine.stages._queries`). *held_out* is the held-out profile,
    read with the profile reader, whose entries are the held-out queries' targets; the pinned
    profile may not name a held-out query nor the held-out profile a calibration one (``FLX-3701``,
    ``FLX-3702``). *max_query_seconds* and *max_query_memory* raise the ceilings a bundle's
    ``limits`` may not pass (``FLX-3700`` outside 30 to 600 seconds and 1 to 16 GiB).

    ``verify`` is a short sequence of named steps, each a function below: :func:`_load_run`,
    :func:`_require_known_slices`, :func:`_require_bundle_unmoved`, :func:`_pair_profile`,
    :func:`_run_legs` (the legs that do not touch the golden record), then
    :func:`_update_golden_baseline` or :func:`_verdict`. A new leg is a new step of
    :func:`_run_legs`; a new dimension of the scorecard joins it in :func:`_profile_legs`,
    reading each emit's decoded columns through :class:`RunRows`.
    """
    from flux_engine.stages._queries import query_ceilings

    # The flags raise the ceilings of the command that runs the queries and nothing else: a
    # bundle's own limits are read against the engine's, here as in every stage.
    try:
        ceilings = query_ceilings(max_query_seconds, max_query_memory)
    except FluxError as error:
        return _refused(error, exit_code=2)
    state = Path(state_dir) if state_dir is not None else Path(".flux")
    layout = RunLayout.for_state_dir(state, run_id).require_real_dirs()
    loaded = _load_run(layout)
    if isinstance(loaded, StageResult):
        return loaded
    # Leg 0a. A mistyped --committed-slice key is a refusal, not a silently dropped flag.
    # Checked before anything expensive: the caller's next command is the same one with the
    # id spelled right.
    refused = _require_known_slices(loaded.receipt, committed_slice or {})
    if refused is not None:
        return refused
    # Leg 0b. The bundle argument is the one the run was produced from.
    refused = _require_bundle_unmoved(bundle_directory, loaded)
    if refused is not None:
        return refused
    # Leg 0c. The profile the plan pins, paired as `plan` pairs it.
    paired, refused = _pair_profile(loaded, profile, sample)
    if refused is not None:
        return refused
    # Leg 0d. The consumers' queries: the registry, and the profile the held-out targets come from.
    queries_plan, refused = _prepare_queries(bundle_directory, loaded, paired, held_out, ceilings)
    if refused is not None:
        return refused
    legs = _run_legs(
        loaded,
        paired,
        queries_plan,
        bundle_directory=bundle_directory,
        run_id=run_id,
        committed_slice=committed_slice or {},
        vectors_path=vectors_path,
        signing_key=signing_key,
        trust_key=trust_key,
        sample=sample,
        jobs=jobs,
    )
    if update_golden:
        return _update_golden_baseline(loaded, legs, reason=reason)
    return _verdict(
        loaded,
        legs,
        reason=reason,
        allow_declared_baseline_drift=allow_declared_baseline_drift,
    )


def _refused(error: FluxError, *, exit_code: int = 1) -> StageResult:
    """The stage's result for a catalogued refusal."""
    return StageResult(
        stage="verify",
        exit_code=exit_code,
        _artifacts={"code": error.code.code},
        stderr=str(error),
        error_code=error.code.code,
    )


@dataclass(frozen=True)
class _Loaded:
    """What step one reads of the applied run: its records, its plan and the digest they bind."""

    layout: RunLayout
    receipt: Dict[str, Any]
    recorded_plan: Dict[str, Any]
    kernel: Kernel
    run_digest_document: Dict[str, Any]
    run_digest: str
    register: Any


def _load_run(layout: RunLayout) -> Union[_Loaded, StageResult]:
    """Step 1: read the run's receipt, its plan and its run digest, or refuse it."""
    if not is_regular_file(layout.receipt_path) or not is_regular_file(layout.run_digest_path):
        missing = NoAppliedRun(run_dir=layout.run_dir)
        return StageResult(
            stage="verify",
            exit_code=1,
            _artifacts={"code": missing.code.code},
            stderr=str(missing),
            error_code=missing.code.code,
        )
    # The plan comes first, and a run directory without one is refused (FLX-2107):
    # `apply` reads one before it writes anything, and going on without it would skip
    # every check that runs through it. A directory named for another plan is a copy
    # (FLX-2113). run-digest.json is read after the plan, so that holds whatever the
    # file contains, and `ship` makes the same checks, so one directory gets one verdict.
    try:
        receipt = read_json_object(layout.receipt_path, code=FLX_2109, remedy=RUN_FILE_REMEDY)
        recorded_plan = load_recorded_plan(layout)
        kernel = plan_kernel(recorded_plan)
        run_digest_document = read_json_object(
            layout.run_digest_path, code=FLX_2110, remedy=RUN_FILE_REMEDY
        )
        require_run_digest_binds_plan(layout, recorded_plan, run_digest_document)
        read_column_provenance(layout, recorded_plan)
        # Parameter register: the sources of the run's numbers, read only through the plan.
        register = read_parameter_register(layout, recorded_plan)
    except FluxError as error:
        return _refused(error)
    run_digest = digest_of_document(run_digest_document, subject="runDigest").value
    return _Loaded(
        layout, receipt, recorded_plan, kernel, run_digest_document, run_digest, register
    )


def _require_known_slices(
    receipt: Mapping[str, Any], committed_slice: Dict[str, str]
) -> Optional[StageResult]:
    """Step 2: every ``--committed-slice`` key names an emit of the run (see ``require_known_slice_exposes``)."""
    try:
        require_known_slice_exposes(receipt, committed_slice)
    except FluxError as error:
        return _refused(error, exit_code=2)
    return None


def _require_bundle_unmoved(
    bundle_directory: Union[str, Path], loaded: _Loaded
) -> Optional[StageResult]:
    """Step 3: the bundle argument is the one the run was made from.

    ``verify``'s own signature took a bundle directory and never read it, so
    ``fluxsim verify /etc --run-id <id>`` reported the same verdict as the real bundle would.
    KERNEL-SPEC §11's inputDigest is what binds a run to the documents it was given, and
    recomputing it here is the whole content of "verify this run against this bundle".
    """
    receipt = loaded.receipt
    try:
        current_input_digest = recompute_input_digest(bundle_directory, kernel=loaded.kernel)
    except FluxError as error:
        return _refused(error)
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
            f"{Path(bundle_directory).resolve()}",
            error_code=moved.code.code,
        )
    return None


def _pair_profile(
    loaded: _Loaded, profile: Union[str, Path, None], sample: Optional["SampleRequest"]
) -> Tuple[Optional["PinnedProfile"], Optional[StageResult]]:
    """Step 4: the profile the run's plan pins, paired with the one handed over.

    Paired exactly as `plan` pairs them. A profile with no pin, or another profile, is refused
    here, before any leg: scoring the run against a document it was not planned against would
    be a comparison of nothing. A pin with no profile is reported as not checked by the legs
    below.
    """
    try:
        paired = _paired_profile(loaded.recorded_plan, loaded.receipt, profile)
    except FluxError as error:
        return None, _refused(error)
    if sample is not None and paired is None:
        return None, _unpaired_sample(loaded.recorded_plan)
    return paired, None


def _prepare_queries(
    bundle_directory: Union[str, Path],
    loaded: _Loaded,
    paired: Optional["PinnedProfile"],
    held_out: Union[str, Path, None],
    ceilings: "Ceilings",
) -> Tuple[Optional["QueriesPlan"], Optional[StageResult]]:
    """Step 4b: the consumers' registry, and the refusals that need no run.

    The held-out profile is read with the profile reader, and the pinned profile may not name a
    held-out query nor the held-out profile a calibration one. A bundle that declares no consumers
    has nothing to prepare, and is refused only a held-out profile it has no use for.
    """
    from flux_engine.stages._queries import prepare_queries

    try:
        return (
            prepare_queries(
                bundle_directory,
                loaded.receipt,
                loaded.recorded_plan,
                pinned=paired,
                held_out=held_out,
                ceilings=ceilings,
            ),
            None,
        )
    except FluxError as error:
        return None, _refused(error)


@dataclass(frozen=True)
class _Legs:
    """What the legs that do not touch the golden record found.

    One field per leg, so a new leg is one new field and one new step in :func:`_run_legs`.
    """

    artifact_mismatches: Tuple[str, ...]
    artifact_detail: str
    artifacts_leg: Dict[str, Any]
    row_problems: List[str]
    vector_failures: int
    vector_messages: List[str]
    against: "ProfileLegs"
    #: The rules and relationships checked on the rows (ADR 0018); empty when there is nothing to check.
    module: "ModuleChecks"
    fidelity_outcomes: Dict[str, Any]
    fidelity_failed: List[str]
    scorecard_failures: List[str]
    #: The consumers' queries as counted on the run (:mod:`flux_engine.stages._queries`); ``None``
    #: for a bundle that declares none.
    queries: Optional["QueriesLeg"]
    legs_ok: bool
    #: FLX-2110 outranks the rest as the reported code: a run directory that no longer
    #: reproduces its own digest makes every other leg a statement about bytes nothing in the
    #: run produced.
    error_code: Optional[str]
    #: The first refusal's code when refusals are the only failure of every leg here
    #: (:func:`_refusal_code`); :func:`_verdict_code` ranks it below a golden regression.
    refusal_code: Optional[str]


def _run_legs(
    loaded: _Loaded,
    paired: Optional["PinnedProfile"],
    queries_plan: Optional["QueriesPlan"],
    *,
    bundle_directory: Union[str, Path],
    run_id: str,
    committed_slice: Dict[str, str],
    vectors_path: Optional[Union[str, Path]],
    signing_key: Optional[bytes],
    trust_key: Optional[bytes],
    sample: Optional["SampleRequest"],
    jobs: int,
) -> _Legs:
    """Step 5: every leg that does not touch the golden record, all of them, unconditionally.

    Including when `--update-golden` was passed: an `--update-golden` invocation that never
    evaluated row conformance, RFC-04 or fidelity would let `fluxsim run --verify
    --update-golden` report success having verified nothing but the golden write itself. None
    of them returns early either: one invocation says everything it found, so an operator
    fixing a corrupted run directory is not told about it one leg per command.
    """
    layout, receipt = loaded.layout, loaded.receipt
    artifact_mismatches = component_mismatches(
        layout, receipt, loaded.run_digest_document.get("components", {})
    )
    artifact_detail = (
        str(RunArtifactsMoved(components=artifact_mismatches)) if artifact_mismatches else ""
    )

    # The emits' rows: each is decoded once, by one worker, for every leg that reads it.
    scored = paired if sample is None and not artifact_mismatches else None
    module_plan = _plan_module_checks(
        bundle_directory,
        layout,
        receipt,
        loaded.recorded_plan,
        artifacts_ok=not artifact_mismatches,
    )
    emits = _read_emits(
        layout,
        receipt,
        scored,
        jobs=jobs,
        module=module_plan,
        hidden_columns=_hidden_columns_of(loaded.recorded_plan) if scored is not None else None,
    )
    row_problems = emits.row_problems()
    module = _finish_module_checks(module_plan, emits.modules(), layout)

    vectors = vectors_path or default_vectors_path()
    vector_failures, vector_messages = run_conformance_vectors(vectors)

    queries = _count_queries(queries_plan, loaded, paired, artifacts_ok=not artifact_mismatches)

    trust_anchor, self_attested = trust_anchor_for(signing_key, trust_key)
    against = _profile_legs(
        layout,
        receipt,
        loaded.recorded_plan,
        run_id,
        loaded.run_digest,
        paired,
        artifacts_ok=not artifact_mismatches,
        sample=sample,
        emits=emits,
        module=module,
        queries=queries.dimension if queries is not None else None,
    )
    against = replace(against, module=module)
    if scored is not None and against.pinned is not None:
        shaped = shaped_columns(bundle_directory, receipt, loaded.recorded_plan)
        against = replace(
            against,
            calendar=_gated(against.calendar, shaped),
            exposure=run_exposure_checks(
                bundle_directory, layout, receipt, loaded.recorded_plan, scored
            ),
        )
    against = against.settled()
    fidelity_outcomes = _fidelity_leg(
        layout,
        receipt,
        committed_slice,
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
    # Leg 4b. The scorecard, for a run whose plan pins a profile: every emit scored against
    # it, with the plan's thresholds and each emit's minCredibility read against the
    # computed scores.
    scorecard_failures = against.failures()
    if queries is not None and queries.failure:
        scorecard_failures = [*scorecard_failures, queries.failure]
    legs_ok = (
        not artifact_mismatches
        and not row_problems
        and vector_failures == 0
        and not fidelity_failed
        and not scorecard_failures
    )
    return _Legs(
        artifact_mismatches=tuple(artifact_mismatches),
        artifact_detail=artifact_detail,
        artifacts_leg=artifacts_leg,
        row_problems=row_problems,
        vector_failures=vector_failures,
        vector_messages=vector_messages,
        against=against,
        module=module,
        fidelity_outcomes=fidelity_outcomes,
        fidelity_failed=fidelity_failed,
        scorecard_failures=scorecard_failures,
        queries=queries,
        legs_ok=legs_ok,
        error_code=RunArtifactsMoved(components=()).code.code if artifact_mismatches else None,
        refusal_code=(
            _refusal_code(fidelity_outcomes, fidelity_failed)
            if not (artifact_mismatches or row_problems or vector_failures or scorecard_failures)
            else (queries.error_code if queries is not None and queries.failure else None)
        ),
    )


def run_calendar_checks(
    layout: RunLayout,
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    paired: "PinnedProfile",
    shaped: Optional[Mapping[str, FrozenSet[str]]] = None,
) -> Optional["CalendarChecks"]:
    """The calendar lines of a run, for a stage that reads no emit for them itself (``ship``).

    Each emit is read as :func:`_emit_pass` reads it, so the lines are the ones ``verify`` forms,
    gated by *shaped* (:func:`shaped_columns`) as ``verify`` gates them.
    ``None`` when the pinned profile publishes no calendar.
    """
    from flux_engine.concordance.calendar_lines import CalendarChecks

    if not any(table.calendar for table in paired.profile.tables.values()):
        return None
    reader = RunRows(layout)
    hidden = _hidden_columns_of(plan_document)
    found = CalendarChecks()
    for expose_id in sorted(receipt.get("emits", {})):
        each = _calendar_pass(layout, expose_id, paired, reader, hidden.get(expose_id, ()))
        if each is not None:
            found = found.merged(each)
    return _gated(found, shaped) if found.lines else None


def _gated(
    checks: Optional["CalendarChecks"], shaped: Optional[Mapping[str, FrozenSet[str]]]
) -> Optional["CalendarChecks"]:
    """*checks* with each line gated by the columns *shaped* names; unchanged when the bundle could not be read."""
    if checks is None or shaped is None:
        return checks
    return checks.gating(shaped)


def shaped_columns(
    bundle_directory: Union[str, Path],
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
) -> Optional[Mapping[str, FrozenSet[str]]]:
    """The date columns each emit's ``rates`` entries shape (ADR 0025 section 2), or ``None`` when the bundle is not the run's.

    A calendar line fails a run only for a column named here; for any other column no entry of the
    bundle can move its calendar, so the line is printed and does not gate. The bundle is read as
    the exposure lines read it: it must be the run's (the same ``inputDigest``). ``None`` leaves
    every line gated.
    """
    from flux_engine.seam.resolve import declared_rates, load_bundle

    try:
        loaded = load_bundle(Path(bundle_directory))
        if _pipeline.input_digest_of(loaded, kernel=plan_kernel(plan_document)).ref.value != str(
            receipt.get("inputDigest")
        ):
            return None
        simulation_id = str(receipt.get("simulationId") or plan_document.get("simulationId"))
        entries = declared_rates(loaded.simulation(simulation_id), where=simulation_id)
    except FluxError:
        return None
    found: Dict[str, set[str]] = {}
    for entry in entries:
        found.setdefault(entry.emit, set()).add(entry.column)
    return {expose_id: frozenset(columns) for expose_id, columns in found.items()}


def run_exposure_checks(
    bundle_directory: Union[str, Path],
    layout: RunLayout,
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    paired: "PinnedProfile",
) -> Optional["ExposureChecks"]:
    """The exposure lines of the ``within`` entries with ``exposure: true`` (ADR 0025 section 8), or ``None`` when there is none to score.

    ``verify`` and ``ship`` both call this, so the lines that fail a run are the same in both.

    A line is made only for an entry whose relationship the pinned profile publishes an ``exposure``
    for, so a run whose profile publishes none is read, and reported, exactly as it was and the
    bundle is not read again. Otherwise the bundle is read as the module checks read it: it must be
    the run's (the same ``inputDigest``), since the entries, the relationships and the facts'
    tolerances are read from it.
    """
    if not any(entry.exposure for entry in paired.profile.relationships):
        return None
    from flux_engine.concordance.exposure_lines import exposure_checks, exposure_sources
    from flux_engine.core.clock import declared_time_bounds
    from flux_engine.seam.resolve import declared_facts, load_bundle, resolve_all_emits
    from flux_stats.reading import parse_rfc3339_ms

    try:
        loaded = load_bundle(Path(bundle_directory))
        if _pipeline.input_digest_of(loaded, kernel=plan_kernel(plan_document)).ref.value != str(
            receipt.get("inputDigest")
        ):
            return None
        simulation_id = str(receipt.get("simulationId") or plan_document.get("simulationId"))
        simulation = loaded.simulation(simulation_id)
        start, end = declared_time_bounds(simulation)
        window = (parse_rfc3339_ms(start), parse_rfc3339_ms(end))
        resolved = resolve_all_emits(loaded, simulation_id=simulation_id)
        facts = declared_facts(simulation, where=simulation_id)
    except FluxError:
        return None
    sources = exposure_sources(resolved, facts)
    reader = RunRows(layout)
    types: Dict[str, Dict[str, str]] = {}

    def rows_of(expose_id: str) -> Tuple[Any, Optional[str]]:
        rows, problem = reader.table(expose_id)
        return (None, problem[1]) if problem is not None else (rows, None)

    def types_of(expose_id: str) -> Dict[str, str]:
        if expose_id not in types:
            try:
                attestation = _read_json(layout.seam_attestation_path(expose_id))
                types[expose_id] = dict(
                    zip(
                        (str(name) for name in attestation["columns"]),
                        (str(name) for name in attestation["types"]),
                        strict=True,
                    )
                )
            except (*_UNREADABLE, FluxError):
                types[expose_id] = {}
        return types[expose_id]

    found = exposure_checks(sources, paired.profile, rows_of, types_of, window)
    return None if found.empty else found


def _hidden_columns_of(plan_document: Mapping[str, Any]) -> Mapping[str, Tuple[str, ...]]:
    """Each emit's hidden columns, as the recorded plan lists them (ADR 0024 section 2)."""
    from flux_engine.concordance.calendar_lines import hidden_in_plan

    return hidden_in_plan(plan_document)[1]


def _verdict_code(moved: Optional[str], regressed: bool, refused: Optional[str]) -> Optional[str]:
    """The one code a failing verify reports: moved artifacts, then a golden regression, then a refusal.

    FLX-2110 outranks the rest, as :class:`_Legs` says. A golden regression comes next: it is a
    catalogued failure with its own remedy (``--update-golden --reason``), whatever else failed. A
    refusal's code is reported only when nothing else is, since it names what the profile lacks
    and nothing about the run.
    """
    if moved is not None:
        return moved
    if regressed:
        return FLX_2114.code
    return refused


def _refusal_code(outcomes: Mapping[str, Any], failed: Sequence[str]) -> Optional[str]:
    """The code of the first refusal, when refusals are the only thing the fidelity leg failed on.

    A property the profile cannot answer is refused, not compared (``FLX-5037`` and the codes
    beside it), and a verify that fails on nothing else is a catalogued failure like the others:
    its envelope carries the code and the terminal points at ``fluxsim explain``. A leg that also
    holds a property outside its margin, an estimate outside its band, or a failure with no
    refusal to name has no single code, and stays uncoded.
    """
    first: Optional[str] = None
    for expose_id in failed:
        outcome = outcomes[expose_id]
        parts = [outcome]
        profiled = outcome.get("profile")
        if profiled is not None and profiled.get("status") == "failed":
            parts.append(profiled)
        for part in parts:
            if part.get("status") != "failed":
                continue
            refusals = part.get("refusals", ())
            if (
                not refusals
                or any(not entry["refused"] for entry in part.get("outside", ()))
                or any(
                    found["outcome"] in ("outside", "undecided")
                    for found in part.get("properties", ())
                    if part.get("sampled")
                )
            ):
                return None
            first = first or str(refusals[0]["code"])
    return first


def _update_golden_baseline(loaded: _Loaded, legs: _Legs, *, reason: Optional[str]) -> StageResult:
    """Step 6a (``--update-golden``): record this run as the baseline, if every leg held."""
    receipt = loaded.receipt
    scorecard_extra = legs.against.artifact()
    if not legs.legs_ok:
        problems = [
            *([legs.artifact_detail] if legs.artifact_detail else []),
            *legs.row_problems,
            *legs.vector_messages[:5],
            *(
                _failure_summary(name, legs.fidelity_outcomes[name])
                for name in legs.fidelity_failed
            ),
            *legs.scorecard_failures,
        ]
        return StageResult(
            stage="verify",
            exit_code=1,
            _artifacts={
                "golden": {"updated": False, "problems": problems},
                "artifactComponents": legs.artifacts_leg,
                "rowConformance": {"ok": not legs.row_problems, "problems": legs.row_problems},
                "rfc04": {"failures": legs.vector_failures, "messages": legs.vector_messages},
                "fidelity": legs.fidelity_outcomes,
                **scorecard_extra,
            },
            stderr="refusing to update the golden baseline -- a leg failed: " + "; ".join(problems),
            error_code=legs.error_code,
        )
    golden = _golden(
        loaded.layout,
        receipt["simulationId"],
        loaded.run_digest,
        loaded.run_digest_document["components"],
        update=True,
        reason=reason,
        declared_baseline=receipt.get("declaredGoldenBaseline"),
        generated_at=_now(),
        kernel_version=loaded.kernel.version,
    )
    exit_code = 0 if golden.get("updated") else 1
    if golden.get("updated"):
        golden["pasteIntoBundle"] = f'spec:\n  golden:\n    baselineDigest: "{loaded.run_digest}"'
    return StageResult(
        stage="verify",
        exit_code=exit_code,
        _artifacts={
            "golden": golden,
            "declaredBaselineDrift": bool(golden.get("specGoldenMismatch")),
            "artifactComponents": legs.artifacts_leg,
            "rowConformance": {"ok": True, "problems": []},
            "rfc04": {"failures": 0, "messages": []},
            "fidelity": legs.fidelity_outcomes,
            **scorecard_extra,
        },
        stdout="golden baseline updated" if golden.get("updated") else "",
        stderr="; ".join(golden.get("problems", [])) if not golden.get("updated") else "",
    )


def _queries_artifact(queries: Optional["QueriesLeg"]) -> Dict[str, Any]:
    """The ``queries`` artifact key: the dimension's member, or nothing for a bundle with no consumers."""
    return {"queries": queries.dimension.to_document()} if queries is not None else {}


def _queries_lines(queries: Optional["QueriesLeg"], against: ProfileLegs) -> List[str]:
    """The queries' lines under the legs, unless the scorecard's own lines already hold them."""
    if queries is None or against.scorecard is not None:
        return []
    return [f"  {line}" for line in queries.dimension.lines()]


def _verdict(
    loaded: _Loaded,
    legs: _Legs,
    *,
    reason: Optional[str],
    allow_declared_baseline_drift: bool,
) -> StageResult:
    """Step 6b: the golden regression, then the verdict over every leg."""
    receipt, run_digest = loaded.receipt, loaded.run_digest
    artifact_mismatches, artifacts_leg = legs.artifact_mismatches, legs.artifacts_leg
    row_problems = legs.row_problems
    vector_failures, vector_messages = legs.vector_failures, legs.vector_messages
    fidelity_outcomes, against = legs.fidelity_outcomes, legs.against
    scorecard_extra = against.artifact()
    register, leg_error_code = loaded.register, legs.error_code
    golden = _golden(
        loaded.layout,
        receipt["simulationId"],
        run_digest,
        loaded.run_digest_document["components"],
        update=False,
        reason=reason,
        declared_baseline=receipt.get("declaredGoldenBaseline"),
        generated_at=_now(),
        kernel_version=loaded.kernel.version,
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
        and legs.legs_ok
        and (allow_declared_baseline_drift or not declared_baseline_drift)
    )

    headline = verdict.headline()
    stderr_parts: List[str] = []
    if not ok:
        if legs.artifact_detail:
            stderr_parts.append(legs.artifact_detail)
        stderr_parts.extend(legs.row_problems)
        stderr_parts.extend(legs.vector_messages[:5])
        stderr_parts.extend(_failed_legs(legs.fidelity_outcomes, legs.fidelity_failed))
        stderr_parts.extend(legs.scorecard_failures)
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
    error_code = _verdict_code(
        leg_error_code, reproducibility is Reproducibility.NOT_REPRODUCIBLE, legs.refusal_code
    )

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
            **_queries_artifact(legs.queries),
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
                *_queries_lines(legs.queries, against),
            ]
        ),
        stderr="\n".join(stderr_parts),
        error_code=error_code,
    )
