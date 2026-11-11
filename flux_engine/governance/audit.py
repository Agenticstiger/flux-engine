# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The decision audit trail: ``decisions.ndjson`` and its meta sidecar.

Implements KERNEL-SPEC section 10's ``decisionsDigest`` ("raw bytes of
``decisions.ndjson``") and section 13's governance band together with the
RFC-04 decision contract (``flux-enforcement-0.5.0.json#/$defs/decision``).

Two files, one accumulator (:class:`DecisionAuditLog`):

* ``decisions.ndjson`` -- one line per call, and each line is the
  RFC-04 decision object **verbatim**: exactly what
  :func:`flux_engine.governance.gate.decide` returned, schema-validated
  before it is written (``FLX-4003`` on a shape that does not conform).
  Nothing about *when* or *in what run* is added to this line -- that
  would make the file something other than a decision log.
* ``decisions.meta.ndjson`` -- the run-scoped context for the same line,
  in the same order: ``{seq, runId, documentId, skillRef, tickMs,
  phase}``. Kept in a parallel file rather than folded into the decision
  object so ``decisions.ndjson`` stays exactly what the schema describes
  and nothing else -- a decision produced once, replayed against a second
  run, is still schema-valid on its own.

**Why not KERNEL-SPEC's own canonicaliser.** ``flux_engine.core.canonical``
rejects any ``float`` (``FLX-1001``): every hashed value in the kernel is
an ``int`` or a ``Decimal``. RFC-04's own numeric discipline is different
on purpose -- ``as_int()`` in the reference gate treats ``200000.0`` as
the JSON integer 200000, so a schema-valid ``tokenBudget`` (and therefore
a schema-valid decision's echoed ``request.tokens``) can legally carry a
float-spelled integer. Running such a decision through the kernel
canonicaliser would raise on conformant RFC-04 output. So each line here
is ``json.dumps(obj, sort_keys=True)`` instead: deterministic (same
object, same bytes, every time -- ``sort_keys`` makes the byte sequence
independent of the dict's insertion order) without adopting a numeric
discipline the decision contract does not share.
``decisionsDigest`` is then a **raw-bytes** digest (KERNEL-SPEC section
2.3's ``raw-bytes`` domain, not ``jcs-document``) over exactly the bytes
written -- the domain the run digest table names for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from jsonschema import Draft202012Validator

from flux_engine.core.digest import DigestRef, digest_of_bytes
from flux_engine.core.errors import FluxError, register
from flux_engine.core.trace import TraceOp, trace_record

__all__ = [
    "AuditedDecision",
    "DecisionAuditLog",
    "DecisionMeta",
    "DecisionAuditWrite",
    "FLX_4003",
    "META_FIELDS",
    "decision_schema_validator",
]

FLX_4003 = register(
    "FLX-4003",
    "decision record does not satisfy the RFC-04 decision contract",
    "A decision was about to be recorded to decisions.ndjson but it does not "
    "validate against flux-enforcement-0.5.0.json#/$defs/decision -- the schema "
    "every conformant gate's output must satisfy. This should be unreachable for "
    "a decision produced by flux_engine.spec.enforce.decide; seeing it means a "
    "caller constructed or mutated a decision object by hand before recording it.",
)

#: The parallel meta file's field set, in the order KERNEL-SPEC's task
#: description lists them. Order is documentation only (plain JSON does
#: not sort keys the way JCS does); :func:`json.dumps` writes them with
#: ``sort_keys=True`` regardless, for byte-stability.
META_FIELDS: Tuple[str, ...] = ("seq", "runId", "documentId", "skillRef", "tickMs", "phase")


@lru_cache(maxsize=1)
def _decision_schema() -> Mapping[str, Any]:
    """Load ``flux-enforcement-0.5.0.json`` once, from whichever copy is live.

    Prefers the public ``flux_spec`` distribution's own resource resolver
    (``flux_spec._paths.schema_dir``, which knows the difference between a
    source checkout and an installed wheel); falls back to the vendored
    copy bundled with flux_engine. Mirrors
    :func:`flux_engine.spec.default_vectors_path`'s two-source fallback,
    for the schema instead of the vectors file.
    """
    try:
        from flux_spec._paths import schema_dir

        candidate = schema_dir() / "flux-enforcement-0.5.0.json"
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    except ImportError:
        pass

    import flux_engine._vendor.flux_spec as _vendored

    vendored_path = (
        Path(_vendored.__file__).resolve().parent / "schema" / "flux-enforcement-0.5.0.json"
    )
    return json.loads(vendored_path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def decision_schema_validator() -> Draft202012Validator:
    """A validator for ``#/$defs/decision`` of the RFC-04 enforcement contract."""
    contract = _decision_schema()
    return Draft202012Validator({"$ref": "#/$defs/decision", "$defs": contract["$defs"]})


def _validate_decision(decision: Mapping[str, Any]) -> None:
    validator = decision_schema_validator()
    error = next(iter(validator.iter_errors(decision)), None)
    if error is not None:
        raise FluxError(FLX_4003, error.message)


@dataclass(frozen=True)
class DecisionMeta:
    """The run-scoped context for one recorded decision.

    ``phase`` is caller-supplied and carried as given -- typically
    ``flux_engine.core.clock.Phase(...).name``, but this module does not
    import :mod:`flux_engine.core.clock` to stay decoupled from the
    scheduler; a plain string or int naming the phase is exactly as
    usable here.
    """

    seq: int
    run_id: str
    document_id: str
    skill_ref: Optional[str]
    tick_ms: int
    phase: Union[str, int]

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ValueError(f"seq must be a non-negative int, got {self.seq!r}")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("runId must be a non-empty string")
        if not isinstance(self.document_id, str) or not self.document_id.strip():
            raise ValueError("documentId must be a non-empty string")
        if self.skill_ref is not None and (
            not isinstance(self.skill_ref, str) or not self.skill_ref.strip()
        ):
            raise ValueError("skillRef must be a non-empty string when present")
        if isinstance(self.tick_ms, bool) or not isinstance(self.tick_ms, int) or self.tick_ms < 0:
            raise ValueError(f"tickMs must be a non-negative int, got {self.tick_ms!r}")
        if isinstance(self.phase, bool) or not isinstance(self.phase, (str, int)):
            raise ValueError(f"phase must be a str or int, got {type(self.phase).__name__}")
        if isinstance(self.phase, str) and not self.phase.strip():
            raise ValueError("phase must be a non-empty string when given as one")

    def as_dict(self) -> Dict[str, Any]:
        """This meta record as the JSON object written to ``decisions.meta.ndjson``."""
        return {
            "seq": self.seq,
            "runId": self.run_id,
            "documentId": self.document_id,
            "skillRef": self.skill_ref,
            "tickMs": self.tick_ms,
            "phase": self.phase,
        }


@dataclass(frozen=True)
class AuditedDecision:
    """One recorded decision, paired with its meta record."""

    decision: Mapping[str, Any]
    meta: DecisionMeta


@dataclass(frozen=True)
class DecisionAuditWrite:
    """What :meth:`DecisionAuditLog.write` produced."""

    decisions_path: Path
    meta_path: Path
    decisions_digest: DigestRef


def _line_bytes(obj: Mapping[str, Any]) -> bytes:
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


class DecisionAuditLog:
    """Accumulates decisions in memory; :meth:`write` flushes both files.

    Every :meth:`record` call schema-validates the decision (``FLX-4003``)
    before it is accepted, and assigns ``seq`` itself, from the number of
    entries recorded so far -- a caller never supplies it, the same reason
    :class:`flux_engine.core.clock.Scheduler` assigns its own ``seq``
    (KERNEL-SPEC section 7): a value derived from "how many came before"
    must come from the one place that actually counts them.
    """

    def __init__(self) -> None:
        self._entries: List[AuditedDecision] = []

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> Tuple[AuditedDecision, ...]:
        """Every recorded decision so far, in recording order."""
        return tuple(self._entries)

    def record(
        self,
        decision: Mapping[str, Any],
        *,
        run_id: str,
        document_id: str,
        tick_ms: int,
        phase: Union[str, int],
        skill_ref: Optional[str] = None,
    ) -> AuditedDecision:
        """Validate and accept one decision. Returns the entry recorded.

        *skill_ref* defaults to the decision's own ``skillRef`` field when
        the caller does not supply one explicitly -- the two usually
        agree, and a caller with nothing extra to say need not repeat it.
        """
        _validate_decision(decision)
        seq = len(self._entries)
        meta = DecisionMeta(
            seq=seq,
            run_id=run_id,
            document_id=document_id,
            skill_ref=skill_ref if skill_ref is not None else decision.get("skillRef"),
            tick_ms=tick_ms,
            phase=phase,
        )
        entry = AuditedDecision(decision=dict(decision), meta=meta)
        self._entries.append(entry)
        return entry

    def decisions_bytes(self) -> bytes:
        """The exact bytes ``decisions.ndjson`` would contain, without writing it."""
        return b"".join(_line_bytes(entry.decision) for entry in self._entries)

    def meta_bytes(self) -> bytes:
        """The exact bytes ``decisions.meta.ndjson`` would contain, without writing it."""
        return b"".join(_line_bytes(entry.meta.as_dict()) for entry in self._entries)

    def decisions_digest(self) -> DigestRef:
        """The ``raw-bytes`` digest of :meth:`decisions_bytes` (KERNEL-SPEC section 10).

        Computable without writing anything to disk, so a caller can bind
        the run digest document to this value before -- or instead of --
        calling :meth:`write`.
        """
        return digest_of_bytes(self.decisions_bytes(), subject="decisionsDigest")

    def trace_records(self) -> List[Dict[str, Any]]:
        """One ``flux-trace/1`` ``decision`` op per recorded entry, in order.

        KERNEL-SPEC section 10's ``decision`` op fields are exactly
        ``seq, policyDigest, reasonCode, allow`` -- narrower than the full
        decision object, which is why the trace op and the audit record
        are two different things carrying two different amounts of detail
        about the same event.
        """
        return [
            trace_record(
                TraceOp.DECISION,
                seq=entry.meta.seq,
                policyDigest=entry.decision["policyDigest"],
                reasonCode=entry.decision["reasonCode"],
                allow=entry.decision["allow"],
            )
            for entry in self._entries
        ]

    def write(self, directory: Union[str, Path]) -> DecisionAuditWrite:
        """Write both files under *directory* (created if needed).

        Returns the paths written and the ``decisionsDigest`` taken over
        the bytes actually written -- not re-read from disk afterward, so
        the digest and the file content cannot disagree because of an
        intervening write from elsewhere.
        """
        out_dir = Path(directory)
        out_dir.mkdir(parents=True, exist_ok=True)

        decisions_bytes = self.decisions_bytes()
        meta_bytes = self.meta_bytes()

        decisions_path = out_dir / "decisions.ndjson"
        meta_path = out_dir / "decisions.meta.ndjson"
        decisions_path.write_bytes(decisions_bytes)
        meta_path.write_bytes(meta_bytes)

        return DecisionAuditWrite(
            decisions_path=decisions_path,
            meta_path=meta_path,
            decisions_digest=digest_of_bytes(decisions_bytes, subject="decisionsDigest"),
        )
