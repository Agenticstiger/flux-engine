# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The numbers a fit may move (ADR 0028 section 8): each is read against the parameter register.

A parameter of ``calibration.parameters`` names a document by its file name and an RFC 6901 pointer to one
number in it. It is accepted only when that number is one the register lists, with the source
``assumption`` or ``default``: what the profile or the contract states is not moved. A value is always
an exact :class:`~fractions.Fraction`, and a candidate is rounded to the parameter's ``step`` before it
is used, so a rerun of the same candidate is the same bundle.

This module imports the standard library, the register and the bundle loader, and no SQL library.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from typing import Any, Dict, Mapping, Sequence, Tuple

from flux_engine.calibrate.errors import FLX_3705, FLX_3706
from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import CalibrationParameter
from flux_engine.seam.coverage import printable
from flux_engine.seam.parameters import ParameterRegister, Source, parse_pointer
from flux_stats.spelling import parse_exact, render_exact

__all__ = ["FitParameter", "document_ids", "exact_text", "resolve_parameters"]

#: The sources a fit may move.
_MOVABLE = frozenset({Source.ASSUMPTION, Source.DEFAULT})


@dataclass(frozen=True)
class FitParameter:
    """One value of the fit: where it is, its bounds and step, and the value the bundle has now."""

    #: The file name the calibration block gave (``simulation.flux.yml``).
    document: str
    #: The id of the document in that file, which the register names it by.
    doc_id: str
    pointer: str
    tokens: Tuple[str, ...]
    minimum: Fraction
    maximum: Fraction
    step: Fraction
    before: Fraction
    source: Source

    @property
    def key(self) -> str:
        """The name the calibrator's dials carry: the file and the pointer."""
        return f"{self.document}#{self.pointer}"

    def snap(self, unit: float) -> Fraction:
        """The value *unit* (0 to 1 across the range) stands for, rounded to the step and kept in range.

        The step is a grid anchored at zero. A rounded value below ``minimum`` or above ``maximum``
        moves to the nearest grid value inside the range, and the range holds at least one
        (:func:`resolve_parameters` checks it).
        """
        position = self.minimum + (self.maximum - self.minimum) * Fraction(min(1.0, max(0.0, unit)))
        grid = round(position / self.step)
        low, high = ceil(self.minimum / self.step), floor(self.maximum / self.step)
        return self.step * min(high, max(low, grid))

    def unit_of(self, value: Fraction) -> float:
        """*value* as a position from 0 to 1 across the range, for the optimiser's start."""
        position = (value - self.minimum) / (self.maximum - self.minimum)
        return float(min(Fraction(1), max(Fraction(0), position)))


def document_ids(files: Mapping[str, Sequence[Mapping[str, Any]]]) -> Dict[str, str]:
    """The id of the one document in each FLUX file; ``FLX-3706`` for a file that holds another count."""
    ids: Dict[str, str] = {}
    for name, documents in files.items():
        if len(documents) != 1:
            raise FluxError(
                FLX_3706,
                f"{printable(name)} holds {len(documents)} documents; a calibration parameter "
                "names a file with exactly one",
            )
        ids[name] = str(documents[0].get("id"))
    return ids


def resolve_parameters(
    declared: Sequence[CalibrationParameter],
    *,
    register: ParameterRegister,
    flux_files: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Tuple[FitParameter, ...]:
    """Each declared parameter as a :class:`FitParameter`, or the refusal that names it.

    *register* is the bundle's parameter register and *flux_files* its FLUX files (the closed set of
    ADR 0006) with the documents each holds. ``FLX-3706``: a file outside the set or with more than one
    document, a pointer that reaches no single number the register lists, a value outside ``min`` and
    ``max``, a step with no grid value inside the range, or two parameters naming one number.
    ``FLX-3705``: a number whose source is not ``assumption`` or ``default``.
    """
    by_place = {
        (registered.number.document, registered.number.tokens): registered
        for registered in register.numbers
    }
    resolved = []
    seen: Dict[Tuple[str, Tuple[str, ...]], int] = {}
    for index, parameter in enumerate(declared):
        at = f"calibration.parameters[{index}]"
        if parameter.document not in flux_files:
            raise FluxError(
                FLX_3706,
                f"{at}.document {printable(parameter.document)!r} is not a FLUX document of the "
                f"bundle (the files are {sorted(printable(name) for name in flux_files)})",
            )
        doc_id = document_ids({parameter.document: flux_files[parameter.document]})[
            parameter.document
        ]
        try:
            tokens = parse_pointer(parameter.pointer)
        except ValueError as error:  # the block's reader checked the shape; this is the escape rule
            raise FluxError(FLX_3706, f"{at}.pointer: {error}") from error
        registered = by_place.get((doc_id, tokens))
        if registered is None:
            raise FluxError(
                FLX_3706,
                f"{at}.pointer {printable(parameter.pointer)!r} reaches no single number the "
                f"parameter register lists in {printable(parameter.document)!r}",
            )
        if registered.source not in _MOVABLE:
            raise FluxError(
                FLX_3705,
                f"{at}: {printable(parameter.document)!r} {printable(parameter.pointer)!r} has the "
                f"source {registered.source.value}; a fit moves only assumption or default numbers",
            )
        if (doc_id, tokens) in seen:
            raise FluxError(
                FLX_3706,
                f"{at} names the same number as calibration.parameters[{seen[(doc_id, tokens)]}]",
            )
        seen[(doc_id, tokens)] = index
        before = parse_exact(registered.number.value)
        if not parameter.minimum <= before <= parameter.maximum:
            raise FluxError(
                FLX_3706,
                f"{at}: the bundle's value {registered.number.value} is outside min "
                f"{render_exact(parameter.minimum)} and max {render_exact(parameter.maximum)}",
            )
        if ceil(parameter.minimum / parameter.step) > floor(parameter.maximum / parameter.step):
            raise FluxError(
                FLX_3706,
                f"{at}: no multiple of step {render_exact(parameter.step)} lies between min and max",
            )
        resolved.append(
            FitParameter(
                document=parameter.document,
                doc_id=doc_id,
                pointer=parameter.pointer,
                tokens=tokens,
                minimum=parameter.minimum,
                maximum=parameter.maximum,
                step=parameter.step,
                before=before,
                source=registered.source,
            )
        )
    return tuple(resolved)


def exact_text(value: Fraction) -> str:
    """*value* as the exact decimal text a patch and the register carry, never ``numerator/denominator``."""
    text = render_exact(value)
    if "/" in text:  # a step is a decimal, so a grid value always terminates
        raise ValueError(f"{value} has no finite decimal spelling")
    return text
