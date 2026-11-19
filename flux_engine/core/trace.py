# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``flux-trace/1``: the closed op vocabulary and the run digest document.

Implements KERNEL-SPEC §10. The trace is the closed list of *observable
decisions* a run makes -- what a reader of the run could have watched
happen -- and nothing else. Kernel internals are never traced: a draw
count, a heap operation or a cache hit is not an observable decision, and
tracing one would make an implementation detail part of the golden.

Two consequences the code enforces rather than documents:

* the op vocabulary is closed (``FLX-2103``) and each op's field set is
  exact (``FLX-2104``). A new field is a ``kernelVersion`` bump, not a
  quiet addition -- which is what makes the wall-clock negative kernel of
  KERNEL-SPEC §14 detectable here rather than three layers downstream.
* component digests are taken over an envelope that names the component,
  so two empty components do not share one digest and a golden failure
  can name which component moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import canonicalise
from flux_engine.core.digest import (
    DigestDomain,
    DigestedDocument,
    DigestRef,
    digest_of_document,
    require_domain,
)
from flux_engine.core.errors import FluxError, register

__all__ = [
    "COMPONENT_OPS",
    "EXTERNAL_COMPONENTS",
    "FLX_2103",
    "FLX_2104",
    "FLX_2105",
    "GOLDEN_CHANGELOG_FILENAME",
    "GoldenUpdate",
    "OP_FIELDS",
    "RUN_FORMAT_VERSION",
    "RunComponents",
    "TRACE_VERSION",
    "TraceOp",
    "build_run_digest_document",
    "check_golden_update",
    "component_digest",
    "component_digests",
    "component_of",
    "golden_changelog_row",
    "moved_components",
    "require_golden_update",
    "trace_record",
]

FLX_2103 = register(
    "FLX-2103",
    "trace op outside the flux-trace/1 vocabulary",
    "The trace vocabulary is closed (KERNEL-SPEC §10). An op the golden "
    "does not know about would move every component digest under it, so a "
    "new op is a kernelVersion bump with a GOLDEN-CHANGELOG row, never an "
    "addition a caller can make at run time.",
)

FLX_2104 = register(
    "FLX-2104",
    "trace op field set does not match flux-trace/1",
    "Each op in KERNEL-SPEC §10 has an exact field set: a missing field "
    "leaves the trace ambiguous and an extra one puts something in the "
    "golden that the vocabulary never promised -- a wall-clock reading, "
    "for example, which would make two identical runs disagree.",
)

FLX_2105 = register(
    "FLX-2105",
    "golden update without a reason and a changelog row",
    "Moving a golden baseline is a deliberate act (KERNEL-SPEC §10): "
    "--update-golden requires --reason, and the new baseline must be "
    "recorded as a GOLDEN-CHANGELOG.md row naming the components that "
    "moved. Without both, a silent regression and an intended change look "
    "the same in the history.",
)

#: KERNEL-SPEC §1: the trace wire version, carried in the receipt and in
#: the run digest document.
TRACE_VERSION = "flux-trace/1"

#: KERNEL-SPEC §10: the shape version of ``run-digest.json``.
RUN_FORMAT_VERSION = 1

#: The file that records every deliberate golden move.
GOLDEN_CHANGELOG_FILENAME = "GOLDEN-CHANGELOG.md"


class TraceOp(str, Enum):
    """The eleven observable decisions of KERNEL-SPEC §10."""

    COHORT = "cohort"
    MARKET = "market"
    TRAIT = "trait"
    SIGNAL = "signal"
    DETECT = "detect"
    TRANSITION = "transition"
    DELIVER = "deliver"
    SUPPRESS = "suppress"
    ASSIGN = "assign"
    EMIT = "emit"
    DECISION = "decision"


#: The exact field set of each op, in the order KERNEL-SPEC §10's table
#: lists them. The order is documentation only -- JCS sorts object keys --
#: but it keeps this table readable next to the specification.
OP_FIELDS: Mapping[TraceOp, Tuple[str, ...]] = {
    TraceOp.COHORT: ("worldId", "lifecycle", "count"),
    TraceOp.MARKET: ("ordinal", "market"),
    TraceOp.TRAIT: ("ordinal", "name", "value"),
    TraceOp.SIGNAL: ("t", "signalId", "ordinal", "occurrence", "payloadDigest"),
    TraceOp.DETECT: ("t", "detectorId", "ordinal", "firstT", "lastT", "eventCount"),
    TraceOp.TRANSITION: (
        "t",
        "journeyId",
        "ordinal",
        "fromState",
        "toState",
        "outcome",
        "success",
    ),
    TraceOp.DELIVER: ("t", "campaignId", "ordinal", "channelId"),
    TraceOp.SUPPRESS: ("t", "campaignId", "ordinal", "reasonCode"),
    TraceOp.ASSIGN: ("experimentId", "ordinal", "variant"),
    TraceOp.EMIT: ("exposeId", "rowIndex"),
    TraceOp.DECISION: ("seq", "policyDigest", "reasonCode", "allow"),
}

#: KERNEL-SPEC §10's component grouping. The five components whose digest
#: is taken over trace ops; ``emits`` and ``decisions`` are digested
#: elsewhere (see :data:`EXTERNAL_COMPONENTS`).
COMPONENT_OPS: Mapping[str, Tuple[TraceOp, ...]] = {
    "population": (TraceOp.COHORT, TraceOp.MARKET, TraceOp.TRAIT),
    "events": (TraceOp.SIGNAL, TraceOp.DETECT),
    "journeys": (TraceOp.TRANSITION,),
    "delivery": (TraceOp.DELIVER, TraceOp.SUPPRESS),
    "assignment": (TraceOp.ASSIGN,),
}

#: The two components of the run digest that are **not** digests of trace
#: ops: ``emits`` carries the per-expose ``outputDigest`` of KERNEL-SPEC §8
#: (domain ``jcs-ndjson-stream``, computed above the sink) and
#: ``decisions`` is the raw bytes of ``decisions.ndjson``. The ``emit``
#: and ``decision`` trace ops still exist -- they are what a reader
#: watches -- but their digest comes from the artifact, not from the
#: trace, so the artifact and the trace cannot disagree unnoticed.
EXTERNAL_COMPONENTS: Tuple[str, ...] = ("emits", "decisions")

_OP_COMPONENT: Mapping[TraceOp, str] = {
    op: component for component, ops in COMPONENT_OPS.items() for op in ops
}


def _as_op(op: Any) -> TraceOp:
    if isinstance(op, TraceOp):
        return op
    try:
        return TraceOp(op)
    except ValueError:
        raise FluxError(FLX_2103, f"op {op!r}") from None


def component_of(op: Any) -> Optional[str]:
    """Return the component *op* belongs to, or ``None`` for emit/decision.

    ``emit`` and ``decision`` are traced but their component digests come
    from the emit stream and ``decisions.ndjson`` respectively -- see
    :data:`EXTERNAL_COMPONENTS`.
    """
    return _OP_COMPONENT.get(_as_op(op))


def trace_record(op: Any, **fields: Any) -> Dict[str, Any]:
    """Return one canonical ``flux-trace/1`` record.

    Validates the op against the closed vocabulary (``FLX-2103``) and the
    field set against KERNEL-SPEC §10's table (``FLX-2104``), then
    canonicalises the values -- so a float, a ``datetime``, or any other
    class with no canonical spelling is refused here rather than at the
    digest, where the diagnostic would name a byte offset instead of a
    field.
    """
    trace_op = _as_op(op)
    expected = set(OP_FIELDS[trace_op])
    given = set(fields)
    if given != expected:
        missing = sorted(expected - given)
        unexpected = sorted(given - expected)
        detail = f"op {trace_op.value!r}"
        if missing:
            detail += f", missing {missing}"
        if unexpected:
            detail += f", unexpected {unexpected}"
        raise FluxError(FLX_2104, detail)
    record: Dict[str, Any] = {"op": trace_op.value}
    for name in OP_FIELDS[trace_op]:
        record[name] = canonicalise(fields[name])
    return record


def _records_for(component: str, records: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    allowed = {op.value for op in COMPONENT_OPS[component]}
    checked: List[Dict[str, Any]] = []
    for index, record in enumerate(records):
        op = record.get("op")
        if op not in allowed:
            raise FluxError(
                FLX_2104,
                f"record {index} carries op {op!r}, which is not part of the "
                f"{component!r} component",
            )
        checked.append(dict(record))
    return checked


def component_digest(component: str, records: Iterable[Mapping[str, Any]]) -> DigestRef:
    """Digest one trace component (KERNEL-SPEC §10).

    The digest is taken over an envelope that names the component and the
    trace version, not over the bare op list: two components that are both
    empty on a given run must not share one digest, or a golden failure
    could not name which one moved.
    """
    if component not in COMPONENT_OPS:
        raise FluxError(
            FLX_2103,
            f"component {component!r} is not one of {sorted(COMPONENT_OPS)}",
        )
    envelope = {
        "traceVersion": TRACE_VERSION,
        "component": component,
        "ops": _records_for(component, records),
    }
    return digest_of_document(envelope, subject=f"{component}Digest")


def component_digests(records: Iterable[Mapping[str, Any]]) -> Dict[str, DigestRef]:
    """Digest all five trace components of one run's *records*.

    Records are partitioned by op into the components of
    :data:`COMPONENT_OPS`, preserving their emission order within each
    component -- the scheduler's order is part of what the golden pins.
    ``emit`` and ``decision`` records are ignored here; their digests come
    from the artifacts (see :data:`EXTERNAL_COMPONENTS`).
    """
    grouped: Dict[str, List[Mapping[str, Any]]] = {name: [] for name in COMPONENT_OPS}
    for index, record in enumerate(records):
        op = record.get("op")
        if op is None:
            raise FluxError(FLX_2104, f"record {index} has no 'op' field")
        trace_op = _as_op(op)
        component = _OP_COMPONENT.get(trace_op)
        if component is not None:
            grouped[component].append(record)
    return {name: component_digest(name, grouped[name]) for name in sorted(grouped)}


@dataclass(frozen=True)
class RunComponents:
    """The ``components`` block of the run digest document (KERNEL-SPEC §10)."""

    population: DigestRef
    events: DigestRef
    journeys: DigestRef
    delivery: DigestRef
    assignment: DigestRef
    emits: Mapping[str, DigestRef]
    decisions: DigestRef

    def as_document(self) -> Dict[str, Any]:
        """Render as the canonical block, checking each reference's domain.

        The domains are part of the claim: the five trace components are
        ``jcs-document``, a per-expose ``outputDigest`` is the
        ``jcs-ndjson-stream`` of KERNEL-SPEC §8, and ``decisions`` is the
        raw bytes of ``decisions.ndjson``. A reference in the wrong domain
        is ``FLX-1002`` here rather than a mismatch nobody can explain
        later.
        """
        block: Dict[str, Any] = {
            "population": require_domain(self.population, DigestDomain.JCS_DOCUMENT).value,
            "events": require_domain(self.events, DigestDomain.JCS_DOCUMENT).value,
            "journeys": require_domain(self.journeys, DigestDomain.JCS_DOCUMENT).value,
            "delivery": require_domain(self.delivery, DigestDomain.JCS_DOCUMENT).value,
            "assignment": require_domain(self.assignment, DigestDomain.JCS_DOCUMENT).value,
            "emits": {
                expose_id: require_domain(
                    self.emits[expose_id], DigestDomain.JCS_NDJSON_STREAM
                ).value
                for expose_id in sorted(self.emits)
            },
            "decisions": require_domain(self.decisions, DigestDomain.RAW_BYTES).value,
        }
        return block


def build_run_digest_document(
    *,
    kernel_version: str,
    input_digest: DigestRef,
    plan_digest: DigestRef,
    components: RunComponents,
) -> DigestedDocument:
    """Build the run digest document and its digest (KERNEL-SPEC §10).

    ``golden.baselineDigest`` is exactly the returned reference's value:
    a golden comparison is one string comparison, and the component block
    beneath it is what :func:`moved_components` reads to name what moved.
    """
    document: Dict[str, Any] = {
        "runFormatVersion": RUN_FORMAT_VERSION,
        "traceVersion": TRACE_VERSION,
        "kernelVersion": kernel_version,
        "inputDigest": require_domain(input_digest, DigestDomain.JCS_DOCUMENT).value,
        "planDigest": require_domain(plan_digest, DigestDomain.JCS_DOCUMENT).value,
        "components": components.as_document(),
    }
    return DigestedDocument(
        document=document,
        ref=digest_of_document(document, subject="runDigest"),
    )


def moved_components(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> Tuple[str, ...]:
    """Name the components that differ between two ``components`` blocks.

    KERNEL-SPEC §10: "A golden failure names the component that moved."
    Nested ``emits`` entries are reported as ``emits.<exposeId>``, and an
    expose that appears on only one side is reported too -- an emit that
    stopped being produced has moved as surely as one whose rows changed.
    """
    moved: List[str] = []
    for name in sorted(set(previous) | set(current)):
        before = previous.get(name)
        after = current.get(name)
        if isinstance(before, Mapping) or isinstance(after, Mapping):
            before_map: Mapping[str, Any] = before if isinstance(before, Mapping) else {}
            after_map: Mapping[str, Any] = after if isinstance(after, Mapping) else {}
            for key in sorted(set(before_map) | set(after_map)):
                if before_map.get(key) != after_map.get(key):
                    moved.append(f"{name}.{key}")
        elif before != after:
            moved.append(name)
    return tuple(moved)


@dataclass(frozen=True)
class GoldenUpdate:
    """One deliberate move of a golden baseline.

    ``components`` names what moved (from :func:`moved_components`), so
    the changelog row says which part of the run changed rather than only
    that the digest did.
    """

    kernel_version: str
    previous_digest: Optional[str]
    new_digest: str
    reason: str
    components: Sequence[str] = ()


def golden_changelog_row(update: GoldenUpdate) -> str:
    """Render *update* as the ``GOLDEN-CHANGELOG.md`` table row.

    One row, one deliberate move: kernel version, the digest before and
    after, the components that moved, and the reason the author gave.
    """
    components = ", ".join(update.components) if update.components else "-"
    previous = update.previous_digest or "-"
    reason = update.reason.strip().replace("|", "/").replace("\n", " ")
    return (
        f"| {update.kernel_version} | {previous} | {update.new_digest} | {components} | {reason} |"
    )


def check_golden_update(update: GoldenUpdate, changelog: str) -> Tuple[str, ...]:
    """Return the reasons *update* is not admissible, or an empty tuple.

    Checks, in order: a non-empty reason of real content; a new digest
    that differs from the previous one (a "move" that moves nothing is a
    mistake, not a golden update); and a ``GOLDEN-CHANGELOG.md`` body that
    already carries a row for the new digest.
    """
    problems: List[str] = []
    reason = update.reason.strip()
    if not reason:
        problems.append("--reason is required and must say why the baseline moved")
    elif len(reason) < 8:
        problems.append(f"--reason {reason!r} is too short to explain a baseline move")
    if update.previous_digest is not None and update.previous_digest == update.new_digest:
        problems.append("the new baseline equals the previous one: nothing moved")
    if update.new_digest not in changelog:
        problems.append(
            f"{GOLDEN_CHANGELOG_FILENAME} has no row for {update.new_digest}; "
            "add the row emitted by golden_changelog_row()"
        )
    return tuple(problems)


def require_golden_update(update: GoldenUpdate, changelog: str) -> None:
    """Raise ``FLX-2105`` unless *update* passes :func:`check_golden_update`."""
    problems = check_golden_update(update, changelog)
    if problems:
        raise FluxError(FLX_2105, "; ".join(problems))
