# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The v0.1 calibration path: declare, never fit.

:class:`DeclaredCalibrator` implements :class:`flux_engine.api.calibration.Calibrator`
by reading a ``Playback`` document's authored ``spec.scorecard`` block
(SEAM-FIDELITY-SPEC references RFC-05: "the twin states its own
trustworthiness") and turning it into a
:class:`~flux_engine.api.calibration.Scorecard` -- without ever computing a
proper scoring rule, because v0.1 has no scoring mechanism (that lands in a
later increment; see :mod:`flux_engine.accuracy._scoring`).

Two ideas carry the whole module.

**Never invent a grade.** :attr:`~flux_engine.api.calibration.Scorecard.grade`
means "the value of a proper scoring rule" (api/calibration.py). An owner's
authored letter grade (``spec.scorecard.grade``, one of ``A``-``F``) is not
that -- it is an assertion, not a measurement -- so it never lands in
``Scorecard.grade``. It travels instead inside :attr:`Scorecard.evidence_digest`,
which points at the whole authored block, letter grade included, so nothing
is lost, and nothing pretends to be a computed score.

**The seam credibility gate matches the offline validator.**
:func:`require_min_credibility` re-implements
``flux_spec.validate.Bundle._check_seam_credibility`` (the max scorecard
credibility over every ``Playback`` bound to a given ``worldRef``) as a
standalone, typed function -- so a caller of this package gets the same
answer the offline validator would give, without needing a full bundle run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple

from flux_engine.api.calibration import Credibility, Provenance, Scorecard
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError, register

__all__ = [
    "FLX_7010",
    "DECLARED_METHOD_ID",
    "DeclaredCalibrator",
    "max_seam_credibility",
    "require_min_credibility",
    "scorecard_from_playback",
]

FLX_7010 = register(
    "FLX-7010",
    "seam credibility gate not met",
    "A Simulation.spec.emits[] entry declares minCredibility, but no Playback "
    "scorecard bound to the referenced worldRef reports at least that "
    "credibility (FLUX RFC-05; mirrors flux_spec.validate's offline check). "
    "Author a Playback with a higher scorecard.credibility, lower the "
    "declared minCredibility, or remove the gate.",
)

#: The only method a declared calibration ever reports -- it fits nothing,
#: so the id names the path, not an algorithm.
DECLARED_METHOD_ID = "flux.calibration.declared/1"


def _decimal(value: Any, *, subject: str) -> Decimal:
    """Coerce an author-typed JSON number to :class:`~decimal.Decimal`.

    FLUX documents are loaded through PyYAML/``json``, so a field declared
    ``unitInterval`` (KERNEL-SPEC's own JSON Schema type) arrives as a
    Python ``float`` -- an author's decimal literal, not a computed
    quantity. Routing it through ``Decimal(x)`` would hash the binary
    approximation of that literal; ``Decimal(str(x))`` recovers the
    shortest decimal string Python's own float repr already round-trips to,
    which is the author's literal for every value this engine's authors can
    actually type. A value already a ``Decimal`` or an ``int`` passes
    through unchanged.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError(f"{subject}: expected a number, got bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"{subject}: expected a number, got {type(value).__name__}")


#: The scorecard fields the FLUX schema types as ``number`` (``unitInterval``)
#: and this module therefore re-types as ``Decimal`` before anything hashes
#: the authored block -- KERNEL-SPEC §2.1 refuses a float anywhere in a
#: digested document, and the raw authored mapping is a straight YAML/JSON
#: parse that still carries Python floats for those two fields.
_SCORECARD_NUMERIC_FIELDS = ("intervalCoverage", "credibility")


def _decimalized_scorecard(authored: Mapping[str, Any]) -> Dict[str, Any]:
    """Return *authored* with its two numeric fields re-typed as ``Decimal``.

    Every other field (``grade``, ``backtestWindow``) is already a string
    per the FLUX schema and passes through unchanged. This is the mapping
    :func:`scorecard_from_playback` digests -- never the raw authored one,
    which still carries Python floats.
    """
    result: Dict[str, Any] = dict(authored)
    for field_name in _SCORECARD_NUMERIC_FIELDS:
        if field_name in result:
            result[field_name] = _decimal(
                result[field_name], subject=f"spec.scorecard.{field_name}"
            )
    return result


def scorecard_from_playback(
    playback: Mapping[str, Any],
    *,
    method_id: str = DECLARED_METHOD_ID,
) -> Scorecard:
    """Build the :class:`Scorecard` for one ``Playback`` document.

    Reads ``spec.scorecard`` -- ``grade``, ``intervalCoverage``,
    ``credibility``, ``backtestWindow``. When the block is absent (an owner
    has not yet declared one, or declared none at all), returns a scorecard
    reporting :attr:`Credibility.NOT_ESTABLISHED` rather than raising:
    "nothing was established" is itself an honest, representable answer,
    not an error.

    ``grade`` is always ``None`` on the returned scorecard -- see the module
    docstring -- but the whole authored block, when present, is preserved
    verbatim behind :attr:`Scorecard.evidence_digest`.
    """
    spec = playback.get("spec") if isinstance(playback, Mapping) else None
    authored = spec.get("scorecard") if isinstance(spec, Mapping) else None
    if not isinstance(authored, Mapping) or not authored:
        return Scorecard(
            provenance=Provenance.DECLARED,
            credibility=Credibility.NOT_ESTABLISHED,
            method_id=method_id,
        )

    interval_coverage: Optional[Decimal] = None
    if "intervalCoverage" in authored:
        interval_coverage = _decimal(
            authored["intervalCoverage"], subject="spec.scorecard.intervalCoverage"
        )

    backtest_window = authored.get("backtestWindow")
    if backtest_window is not None and not isinstance(backtest_window, str):
        raise TypeError("spec.scorecard.backtestWindow: expected a string")

    evidence_digest = digest_of_document(
        _decimalized_scorecard(authored), subject="Playback.spec.scorecard"
    )

    return Scorecard(
        provenance=Provenance.DECLARED,
        credibility=Credibility.DECLARED,
        method_id=method_id,
        grade=None,
        interval_coverage=interval_coverage,
        backtest_window=backtest_window,
        evidence_digest=evidence_digest,
    )


@dataclass(frozen=True)
class DeclaredCalibrator:
    """``Calibrator`` that declares rather than fits, for one ``Playback``.

    :meth:`calibrate` never changes *dials* -- v0.1 has no fitting
    mechanism -- and returns them unchanged alongside the
    :class:`Scorecard` built from :func:`scorecard_from_playback`.
    Satisfies :class:`flux_engine.api.calibration.Calibrator` structurally
    (the Protocol is ``runtime_checkable``): no subclassing, no
    registration.
    """

    playback: Mapping[str, Any] = field(default_factory=dict)
    method_id: str = DECLARED_METHOD_ID

    def calibrate(
        self,
        aggregates: Mapping[str, Any],
        dials: Mapping[str, Decimal],
    ) -> Tuple[Mapping[str, Decimal], Scorecard]:
        """Return *dials* unchanged, plus this Playback's declared scorecard.

        *aggregates* is accepted for Protocol conformance and read by
        nothing: a declared calibration fits no aggregate statistic to
        anything (ADR-0003's aggregate-only rule constrains what a
        *fitting* calibrator may cross the boundary with; a calibrator that
        fits nothing has no boundary to police).
        """
        del aggregates  # unused: declared calibration fits nothing (see docstring)
        return dict(dials), scorecard_from_playback(self.playback, method_id=self.method_id)


def max_seam_credibility(
    flux_docs: Mapping[str, Any],
    *,
    world_ref: str,
) -> Optional[Decimal]:
    """Return the highest scorecard credibility among Playbacks bound to *world_ref*.

    Mirrors ``flux_spec.validate.Bundle._check_seam_credibility``'s
    reduction exactly: only ``Playback`` documents whose
    ``spec.worldRef`` equals *world_ref* and which carry a
    ``spec.scorecard.credibility`` are considered; ``None`` when there are
    none.
    """
    scores = []
    for doc in flux_docs.values():
        if not isinstance(doc, Mapping) or doc.get("kind") != "Playback":
            continue
        spec = doc.get("spec")
        if not isinstance(spec, Mapping) or spec.get("worldRef") != world_ref:
            continue
        scorecard = spec.get("scorecard")
        if isinstance(scorecard, Mapping) and "credibility" in scorecard:
            scores.append(_decimal(scorecard["credibility"], subject="spec.scorecard.credibility"))
    return max(scores) if scores else None


def require_min_credibility(
    flux_docs: Mapping[str, Any],
    *,
    world_ref: str,
    min_credibility: Any,
) -> Decimal:
    """Raise ``FLX-7010`` unless some Playback bound to *world_ref* meets *min_credibility*.

    Returns the maximum credibility found on success, so a caller can
    quote it in a receipt without a second pass over *flux_docs*.
    """
    threshold = _decimal(min_credibility, subject="emits[].minCredibility")
    best = max_seam_credibility(flux_docs, world_ref=world_ref)
    if best is None:
        raise FluxError(
            FLX_7010,
            f"world {world_ref!r}: minCredibility {threshold} declared but no Playback "
            "scorecard calibrates this world",
        )
    if best < threshold:
        raise FluxError(
            FLX_7010,
            f"world {world_ref!r}: best scorecard credibility {best} is below the "
            f"required minCredibility {threshold}",
        )
    return best
