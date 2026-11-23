# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Response families in the runtime (ADR 0029 section 2).

A response family says what an intervention does to one journey's outcome, segment by segment.
This module does the two things the declaration (:mod:`flux_engine.seam.responses`) leaves to
the run:

* **Estimate** (:func:`estimate_responses`): for each segment key, read the control and the treated
  cell of the family's two queries from the pinned profile. A cell's ``levelCounts`` are the
  records the query does not match and the records it matches, so a cell's ``n`` is their sum and
  its ``x`` the matches. The runtime's readout (:class:`~flux_engine.runtime.experiment.ExperimentStatistics`)
  gives the control rate ``p0 = x_c / n_c``, the effect ``d = x_t / n_t - p0`` and its interval
  ``[L, U]`` at the family's confidence. A withheld cell, or an arm with no records, gives that
  segment no estimate; it is reported, and its subjects keep their journey's own probability. A
  match count of zero is an estimate, a rate of zero.
* **Apply** (:class:`ResponseRuntime`): a subject's segment is the first of the family's
  :class:`Segment` documents, in the family's key order, whose query selects its Persona. In the
  family's journey an untreated subject succeeds with probability ``p0`` and a treated one, whose
  variant in the family's experiment names the family's treatment, with ``min(1, max(0, p0 + d))``.
  Every other subject keeps the probability its reaction declares. The draw is the one the
  journey always makes, on the same path and at the same index: only the probability it is
  compared with changes, so a subject whose probability is unchanged keeps its outcome.

The estimates are read from the pinned profile when a plan is made and recorded in the plan
(:meth:`ResponseEstimates.as_document`), so ``apply --run-id``, ``replay`` and ``verify`` need no
profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.core.rng import kernel_context
from flux_engine.core.seedtree import sweep_effective_seed
from flux_engine.expr.attrs import AttributeModel
from flux_engine.expr.compile import Predicate, compile_source
from flux_engine.expr.types import nfc
from flux_engine.runtime.cast import Cast, strip_range
from flux_engine.runtime.experiment import (
    ArmCounts,
    CompiledExperiment,
    ExperimentStatistics,
    default_readout,
)
from flux_engine.seam.parameters import (
    RESPONSE_CONTROL,
    RESPONSE_EFFECT,
    ParameterRegister,
    ResponseEstimate,
    ResponseInterval,
    add_response_estimates,
)
from flux_engine.seam.responses import ResponseFamily
from flux_engine.world.population import SubjectStore
from flux_stats.profile import Profile, QueryEntry

__all__ = [
    "REPLICATE_PLAN_KEY",
    "RESPONSES_PLAN_KEY",
    "Replicate",
    "ResponseEstimates",
    "ResponseRuntime",
    "SegmentEstimate",
    "Unestimated",
    "decimal_text",
    "estimate_responses",
    "query_cell",
    "register_estimates",
    "replicate_effect",
    "replicate_seed",
    "response_rate",
]

#: ``plan.json``'s key for the estimates the pinned profile gave the response families, so
#: ``apply --run-id``, ``replay`` and ``verify`` need no profile for them. Present only for a
#: Simulation that declares ``responses``.
RESPONSES_PLAN_KEY = "responseEstimates"

#: ``plan.json``'s key for the replicate a plan was made for (ADR 0029 section 3): ``r``, ``R`` and
#: the effects it took. Only a replicate plan holds it, so ``replay`` applies the same effects.
REPLICATE_PLAN_KEY = "replicate"

FLX_3671 = register(
    "FLX-3671",
    "a Simulation declares response families and pins no profile to read them from",
    "A response family is estimated from the cells of the profile the Simulation pins, so a "
    "Simulation that declares responses pins that profile under "
    "extensions['com.agentics.flux/1'].profile and passes it to plan with --profile. Nothing "
    "else is read for an estimate.",
)

FLX_3672 = register(
    "FLX-3672",
    "the response estimates a plan records are not well formed",
    "plan.json holds the estimates the pinned profile gave the response families under "
    "responseEstimates, and apply and replay read them back. A plan edited by hand, or written "
    "for other families than the Simulation declares now, is refused rather than applied.",
)

FLX_3673 = register(
    "FLX-3673",
    "a replicate does not fit the Simulation's response families",
    "A forecast runs the sweep as replicates, and a replicate's plan records its run r of R and "
    "the effect it took for each estimated segment of each family, under replicate. A replicate "
    "of a Simulation that declares no family, a run r outside 1 to R, an effect that is not "
    "an exact fraction, or effects that do not name each estimated segment exactly once, are "
    "refused rather than applied.",
)

_ARMS = ("control", "treated")
_FRACTION = re.compile(r"-?(?:0|[1-9][0-9]*)/[1-9][0-9]*")


@dataclass(frozen=True)
class SegmentEstimate:
    """The control rate and the effect of one family in one segment, with the cells behind them.

    ``control`` and ``treated`` are ``(n, x)``: the records of the cell and the matches among
    them. ``p0``, ``effect``, ``low`` and ``high`` are the readout's, ``confidence`` the family's.
    ``cells`` names the control cell and then the treated cell, ``queries/<query id>/<group key>``.
    """

    response: int
    family: str
    segment: str
    control: Tuple[int, int]
    treated: Tuple[int, int]
    p0: Decimal
    effect: Decimal
    low: Decimal
    high: Decimal
    confidence: str
    cells: Tuple[str, str]

    def as_document(self) -> Dict[str, Any]:
        return {
            "response": self.response,
            "family": self.family,
            "segment": self.segment,
            "control": {"n": self.control[0], "x": self.control[1]},
            "treated": {"n": self.treated[0], "x": self.treated[1]},
            "p0": decimal_text(self.p0),
            "effect": decimal_text(self.effect),
            "low": decimal_text(self.low),
            "high": decimal_text(self.high),
            "confidence": self.confidence,
            "cells": list(self.cells),
        }


@dataclass(frozen=True)
class Unestimated:
    """A segment of a family the profile gave no estimate, and why."""

    response: int
    family: str
    segment: str
    reason: str

    def as_document(self) -> Dict[str, Any]:
        return {
            "response": self.response,
            "family": self.family,
            "segment": self.segment,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ResponseEstimates:
    """Every estimate the families got, and every segment that got none, in declared order."""

    estimates: Tuple[SegmentEstimate, ...] = ()
    unestimated: Tuple[Unestimated, ...] = ()

    def get(self, response: int, segment: str) -> Optional[SegmentEstimate]:
        for estimate in self.estimates:
            if estimate.response == response and estimate.segment == segment:
                return estimate
        return None

    def as_document(self) -> Dict[str, Any]:
        return {
            "estimates": [estimate.as_document() for estimate in self.estimates],
            "unestimated": [item.as_document() for item in self.unestimated],
        }

    @classmethod
    def from_document(
        cls, document: Any, families: Sequence[ResponseFamily], *, where: str
    ) -> "ResponseEstimates":
        """The estimates a plan recorded, checked against the families the Simulation declares."""
        if not isinstance(document, Mapping) or set(document) != {"estimates", "unestimated"}:
            raise FluxError(
                FLX_3672, f"{where}: {RESPONSES_PLAN_KEY} holds estimates and unestimated"
            )
        estimates = tuple(
            _read_estimate(item, families, f"{where}: estimates[{index}]")
            for index, item in enumerate(_listed(document["estimates"], where))
        )
        unestimated = tuple(
            _read_unestimated(item, families, f"{where}: unestimated[{index}]")
            for index, item in enumerate(_listed(document["unestimated"], where))
        )
        found = {(item.response, item.segment) for item in estimates} | {
            (item.response, item.segment) for item in unestimated
        }
        wanted = {
            (index, level)
            for index, family in enumerate(families)
            for level, _segment in family.segments
        }
        if len(found) != len(estimates) + len(unestimated) or found != wanted:
            raise FluxError(
                FLX_3672,
                f"{where}: {RESPONSES_PLAN_KEY} does not name each segment of each declared "
                "family exactly once",
            )
        return cls(estimates=estimates, unestimated=unestimated)


def replicate_effect(low: Decimal, high: Decimal, r: int, runs: int) -> Fraction:
    """Replicate *r* of *runs*: ``L + (U - L)(2r - 1) / (2R)``, the midpoint of its part of ``[L, U]``.

    Exact: the bounds are taken as the fractions they are, and nothing is rounded.
    """
    return Fraction(low) + (Fraction(high) - Fraction(low)) * (2 * r - 1) / (2 * runs)


@dataclass(frozen=True)
class Replicate:
    """Run *r* of the *runs* of a sweep, and the effect it takes for each estimated segment.

    ``effects`` holds ``(response index, family id, segment, effect)``, and is empty until
    :meth:`resolved` fills it from the estimates. A plan records the resolved replicate, and
    ``replay`` applies those effects as they are written.
    """

    r: int
    runs: int
    effects: Tuple[Tuple[int, str, str, Fraction], ...] = ()

    def __post_init__(self) -> None:
        if type(self.r) is not int or type(self.runs) is not int or not 1 <= self.r <= self.runs:
            raise FluxError(FLX_3673, f"replicate {self.r!r} of {self.runs!r} is not a run 1 to R")

    def resolved(
        self, families: Sequence[ResponseFamily], estimates: ResponseEstimates, *, where: str
    ) -> "Replicate":
        """This replicate with its effects: the estimates' own, or the recorded ones, checked."""
        if not families:
            raise FluxError(
                FLX_3673, f"{where}: a replicate needs the Simulation to declare responses"
            )
        if not self.effects:
            return Replicate(
                self.r,
                self.runs,
                tuple(
                    (
                        item.response,
                        item.family,
                        item.segment,
                        replicate_effect(item.low, item.high, self.r, self.runs),
                    )
                    for item in estimates.estimates
                ),
            )
        named = [(index, family, segment) for index, family, segment, _ in self.effects]
        wanted = [(item.response, item.family, item.segment) for item in estimates.estimates]
        if named != wanted:
            raise FluxError(
                FLX_3673,
                f"{where}: the replicate's effects do not name each estimated segment of each "
                "family exactly once",
            )
        return self

    def by_name(self) -> Dict[Tuple[str, str], Fraction]:
        """The effects as ``{(family id, segment): effect}``, the form the runtime takes."""
        return {(family, segment): effect for _index, family, segment, effect in self.effects}

    def as_document(self) -> Dict[str, Any]:
        return {
            "r": self.r,
            "R": self.runs,
            "effects": [
                {
                    "response": index,
                    "family": family,
                    "segment": segment,
                    "effect": f"{effect.numerator}/{effect.denominator}",
                }
                for index, family, segment, effect in self.effects
            ],
        }

    @classmethod
    def from_document(cls, document: Any, *, where: str) -> "Replicate":
        """The replicate a plan recorded, read back; what it names is checked by :meth:`resolved`."""
        if not isinstance(document, Mapping) or set(document) != {"r", "R", "effects"}:
            raise FluxError(FLX_3673, f"{where}: {REPLICATE_PLAN_KEY} holds r, R and effects")
        listed = document["effects"]
        if not isinstance(listed, list) or not listed:
            raise FluxError(FLX_3673, f"{where}: {REPLICATE_PLAN_KEY}.effects is a list of effects")
        effects: List[Tuple[int, str, str, Fraction]] = []
        for item in listed:
            if not isinstance(item, Mapping) or set(item) != {
                "response", "family", "segment", "effect"
            }:  # fmt: skip
                raise FluxError(
                    FLX_3673, f"{where}: an effect holds response, family, segment, effect"
                )
            text = item["effect"]
            if (
                type(item["response"]) is not int
                or not isinstance(item["family"], str)
                or not isinstance(item["segment"], str)
                or not isinstance(text, str)
                or _FRACTION.fullmatch(text) is None
            ):
                raise FluxError(FLX_3673, f"{where}: an effect is an exact fraction n/d")
            effects.append((item["response"], item["family"], item["segment"], Fraction(text)))
        return cls(document["r"], document["R"], tuple(effects))


def replicate_seed(simulation: Mapping[str, Any], seed: int, replicate: Replicate) -> int:
    """The effective seed of *replicate*: the sweep's run ``r - 1``, ``baseSeed + r - 1``.

    The sweep rule is "run ``k`` uses ``baseSeed + k``" for ``k`` from 0 to ``runs - 1``
    (:func:`~flux_engine.core.seedtree.sweep_effective_seed`), and replicate ``r`` of ``R`` is the
    sweep's run ``r - 1``, so the replicates use ``baseSeed`` to ``baseSeed + R - 1`` and never
    ``baseSeed + R``. ``baseSeed`` is ``spec.sweep.baseSeed``; a sweep that declares none starts
    at *seed*, the seed the Simulation's run takes without a sweep.
    """
    spec = simulation.get("spec")
    sweep = spec.get("sweep") if isinstance(spec, Mapping) else None
    declared = sweep.get("baseSeed") if isinstance(sweep, Mapping) else None
    if declared is not None and (isinstance(declared, bool) or not isinstance(declared, int)):
        raise FluxError(FLX_3673, "spec.sweep.baseSeed is an integer")
    return sweep_effective_seed(seed if declared is None else declared, replicate.r - 1)


def decimal_text(value: Decimal) -> str:
    """*value* as an exact decimal text: positional, with no exponent and no sign on zero."""
    if value == 0:
        return "0"
    return format(value, "f")


def _listed(value: Any, where: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise FluxError(FLX_3672, f"{where}: {RESPONSES_PLAN_KEY} members are lists")
    return value


def _family_at(index: Any, name: Any, families: Sequence[ResponseFamily], where: str) -> int:
    if type(index) is not int or not 0 <= index < len(families) or families[index].id != name:
        raise FluxError(FLX_3672, f"{where} does not name a declared family")
    return index


def _segment_of(family: ResponseFamily, level: Any, where: str) -> str:
    if not isinstance(level, str) or level not in dict(family.segments):
        raise FluxError(FLX_3672, f"{where}.segment is not a segment of the family")
    return level


def _decimal(value: Any, where: str) -> Decimal:
    try:
        if not isinstance(value, str):
            raise ValueError(value)
        return Decimal(value)
    except (ValueError, ArithmeticError):
        raise FluxError(FLX_3672, f"{where} is an exact decimal text") from None


def _counts(value: Any, where: str) -> Tuple[int, int]:
    if not isinstance(value, Mapping) or set(value) != {"n", "x"}:
        raise FluxError(FLX_3672, f"{where} holds n and x")
    n, x = value["n"], value["x"]
    if type(n) is not int or type(x) is not int or not 0 < n or not 0 <= x <= n:
        raise FluxError(FLX_3672, f"{where} holds a positive n and an x of at most n")
    return n, x


def _read_estimate(item: Any, families: Sequence[ResponseFamily], where: str) -> SegmentEstimate:
    keys = {
        "response", "family", "segment", "control", "treated", "p0", "effect", "low", "high",
        "confidence", "cells",
    }  # fmt: skip
    if not isinstance(item, Mapping) or set(item) != keys:
        raise FluxError(FLX_3672, f"{where} holds {sorted(keys)}")
    index = _family_at(item["response"], item["family"], families, where)
    family = families[index]
    cells = item["cells"]
    if (
        not isinstance(cells, list)
        or len(cells) != 2
        or not all(isinstance(cell, str) for cell in cells)
    ):
        raise FluxError(FLX_3672, f"{where}.cells names two cells")
    if item["confidence"] != family.estimate.declared_confidence:
        raise FluxError(FLX_3672, f"{where}.confidence is not the family's")
    return SegmentEstimate(
        response=index,
        family=family.id,
        segment=_segment_of(family, item["segment"], where),
        control=_counts(item["control"], f"{where}.control"),
        treated=_counts(item["treated"], f"{where}.treated"),
        p0=_decimal(item["p0"], f"{where}.p0"),
        effect=_decimal(item["effect"], f"{where}.effect"),
        low=_decimal(item["low"], f"{where}.low"),
        high=_decimal(item["high"], f"{where}.high"),
        confidence=item["confidence"],
        cells=(cells[0], cells[1]),
    )


def _read_unestimated(item: Any, families: Sequence[ResponseFamily], where: str) -> Unestimated:
    if not isinstance(item, Mapping) or set(item) != {"response", "family", "segment", "reason"}:
        raise FluxError(FLX_3672, f"{where} holds response, family, segment and reason")
    index = _family_at(item["response"], item["family"], families, where)
    reason = item["reason"]
    if not isinstance(reason, str) or not reason:
        raise FluxError(FLX_3672, f"{where}.reason is a non-empty text")
    return Unestimated(
        response=index,
        family=families[index].id,
        segment=_segment_of(families[index], item["segment"], where),
        reason=reason,
    )


def _group_cell(group: Any, query: str, level: str) -> Tuple[Optional[Tuple[int, int]], str]:
    """The ``(n, x)`` of one published group of a query, or ``None`` and the reason."""
    if not group.published or group.suppressed_cells or group.statistic is None:
        return None, f"the cell {query}/{level} is withheld"
    counts = group.statistic.level_counts
    if len(counts) != 2 or sum(counts) == 0:
        return None, f"the cell {query}/{level} holds no records"
    return (sum(counts), counts[1]), ""


def query_cell(
    queries: Mapping[str, QueryEntry], query: str, level: str
) -> Tuple[Optional[Tuple[int, int]], str]:
    """The ``(n, x)`` of one query cell, or ``None`` and the reason there is none."""
    entry = queries.get(query)
    if entry is None:
        return None, f"the profile holds no query {query!r}"
    for statistic in entry.statistics:
        if statistic.group_by is None:
            continue
        for group in statistic.groups:
            if group.key != level:
                continue
            return _group_cell(group, query, level)
        return None, f"the profile holds no cell {query}/{level}"
    return None, f"the profile publishes no grouped cell of the query {query!r}"


def estimate_responses(
    families: Sequence[ResponseFamily],
    profile: Profile,
    *,
    statistics: Optional[ExperimentStatistics] = None,
) -> ResponseEstimates:
    """The estimate of each segment of each family, from the cells of *profile* alone.

    The readout is *statistics* (the exact two-proportion readout by default). A segment whose
    control or treated cell is withheld, absent, or holds no record has no estimate and is listed
    in :attr:`ResponseEstimates.unestimated`; a cell with no match is an estimate.
    """
    readout = statistics if statistics is not None else default_readout()
    queries = {nfc(query.name): query for query in profile.queries}
    found: List[SegmentEstimate] = []
    left: List[Unestimated] = []
    for index, family in enumerate(families):
        confidence = Decimal(family.estimate.declared_confidence)
        for level, _segment in family.segments:
            arms: Dict[str, Tuple[int, int]] = {}
            reason = ""
            for arm in _ARMS:
                query = getattr(family.estimate, arm)
                cell, reason = query_cell(queries, query, level)
                if cell is None:
                    break
                arms[arm] = cell
            if len(arms) < len(_ARMS):
                left.append(Unestimated(index, family.id, level, reason))
                continue
            row = readout.compare(
                ArmCounts("control", arms["control"][0], arms["control"][1]),
                ArmCounts("treated", arms["treated"][0], arms["treated"][1]),
                confidence=confidence,
            )
            found.append(
                SegmentEstimate(
                    response=index,
                    family=family.id,
                    segment=level,
                    control=arms["control"],
                    treated=arms["treated"],
                    p0=row.control_rate,
                    effect=row.difference,
                    low=row.interval_low,
                    high=row.interval_high,
                    confidence=family.estimate.declared_confidence,
                    cells=(
                        f"queries/{family.estimate.control}/{level}",
                        f"queries/{family.estimate.treated}/{level}",
                    ),
                )
            )
    return ResponseEstimates(estimates=tuple(found), unestimated=tuple(left))


def register_estimates(
    register: ParameterRegister, estimates: ResponseEstimates, *, code: ErrorCode
) -> ParameterRegister:
    """*register* with the control rate and the effect of each estimate listed (register 1-3-0)."""
    listed: List[ResponseEstimate] = []
    for estimate in estimates.estimates:
        listed.append(
            ResponseEstimate(
                kind=RESPONSE_CONTROL,
                response=estimate.response,
                segment=estimate.segment,
                value=decimal_text(estimate.p0),
                cells=(estimate.cells[0],),
            )
        )
        listed.append(
            ResponseEstimate(
                kind=RESPONSE_EFFECT,
                response=estimate.response,
                segment=estimate.segment,
                value=decimal_text(estimate.effect),
                cells=estimate.cells,
                interval=ResponseInterval(
                    low=decimal_text(estimate.low),
                    high=decimal_text(estimate.high),
                    confidence=estimate.confidence,
                ),
            )
        )
    if not listed:
        return register
    return add_response_estimates(register, listed, code=code)


def response_rate(p0: Decimal, effect: Fraction, *, treated: bool) -> Decimal:
    """The probability a subject succeeds with: ``p0``, or ``min(1, max(0, p0 + effect))`` treated.

    The sum is exact; its decimal is taken once, in the kernel's context.
    """
    if not treated:
        return p0
    exact = min(Fraction(1), max(Fraction(0), Fraction(p0) + effect))
    with localcontext(kernel_context()):
        return Decimal(exact.numerator) / Decimal(exact.denominator)


@dataclass(frozen=True)
class _Family:
    """One family as the runtime applies it."""

    journey: str
    #: ``{ordinal: segment level}`` for every subject a segment selects.
    segment_of: Mapping[int, str]
    #: ``{ordinal: variant}`` of the family's experiment.
    assigned: Mapping[int, str]
    #: The ordinals whose variant names the family's treatment.
    treated: frozenset[int]
    #: ``{level: (untreated probability, treated probability)}`` for each estimated segment.
    rates: Mapping[str, Tuple[Decimal, Decimal]]


def _segment_ordinals(
    family: ResponseFamily, cast: Cast, model: AttributeModel, store: SubjectStore
) -> Dict[int, str]:
    """``{ordinal: segment level}`` for every subject the first selecting segment of *family* takes."""
    compiled: List[Tuple[str, Predicate]] = [
        (
            level,
            compile_source(
                str(cast.document(segment).get("spec", {}).get("query", "true")),
                model.subject,
            ),
        )
        for level, segment in family.segments
    ]
    segment_of: Dict[int, str] = {}
    for ordinal in store.ordinals:
        view = store.view(ordinal, model.subject)
        for level, selects in compiled:
            if selects(view):
                segment_of[ordinal] = level
                break
    return segment_of


def _segment_rates(
    index: int,
    family: ResponseFamily,
    estimates: ResponseEstimates,
    effects: Optional[Mapping[Tuple[str, str], Fraction]],
) -> Dict[str, Tuple[Decimal, Decimal]]:
    """``{level: (untreated probability, treated probability)}`` for each estimated segment."""
    rates: Dict[str, Tuple[Decimal, Decimal]] = {}
    for estimate in estimates.estimates:
        if estimate.response != index:
            continue
        effect = (effects or {}).get((family.id, estimate.segment))
        rates[estimate.segment] = (
            estimate.p0,
            response_rate(
                estimate.p0,
                Fraction(estimate.effect) if effect is None else effect,
                treated=True,
            ),
        )
    return rates


class ResponseRuntime:
    """What the families do to the journeys' reactions: a probability for each subject's draw.

    Built once per run from the estimates, the cast and the experiment's assignment
    (:meth:`build`); :meth:`probability` is the function :func:`flux_engine.runtime.journey.run_journeys`
    takes. *effects* overrides the effect of a segment: ``{(family id, segment): effect}``, exact,
    in place of the estimate's own.
    """

    def __init__(
        self,
        families: Tuple[_Family, ...],
        estimates: ResponseEstimates,
        assignments: Mapping[str, Mapping[int, str]],
    ) -> None:
        self._families = families
        self.estimates = estimates
        #: ``{experiment id: {ordinal: variant}}`` for each experiment a family names.
        self.assignments = assignments

    @classmethod
    def build(
        cls,
        *,
        families: Sequence[ResponseFamily],
        estimates: ResponseEstimates,
        cast: Cast,
        model: AttributeModel,
        store: SubjectStore,
        experiments: Mapping[str, CompiledExperiment],
        assignments: Mapping[str, Mapping[int, str]],
        effects: Optional[Mapping[Tuple[str, str], Fraction]] = None,
    ) -> "ResponseRuntime":
        built: List[_Family] = []
        for index, family in enumerate(families):
            segment_of = _segment_ordinals(family, cast, model, store)
            cast.document(family.experiment)  # FLX-2301 when the run's cast lacks it
            experiment = experiments[strip_range(family.experiment)]
            names = frozenset(
                variant.name
                for variant in experiment.variants
                if strip_range(variant.treatment_id) == strip_range(family.treatment)
            )
            assigned = assignments[experiment.experiment_id]
            built.append(
                _Family(
                    journey=strip_range(family.journey),
                    segment_of=segment_of,
                    assigned=assigned,
                    treated=frozenset(
                        ordinal for ordinal, variant in assigned.items() if variant in names
                    ),
                    rates=_segment_rates(index, family, estimates, effects),
                )
            )
        return cls(tuple(built), estimates, assignments)

    def probability(self, journey_id: str, ordinal: int, declared: Decimal) -> Decimal:
        """The probability *ordinal*'s reaction in *journey_id* is drawn against.

        The first family of the journey whose segments select the subject sets it; a subject in
        no segment, or in a segment with no estimate, keeps *declared*.
        """
        for family in self._families:
            if family.journey != journey_id:
                continue
            level = family.segment_of.get(ordinal)
            if level is None:
                continue
            rates = family.rates.get(level)
            if rates is None:
                return declared
            return rates[1] if ordinal in family.treated else rates[0]
        return declared

    def arm_of(self, family: int, ordinal: int) -> Optional[str]:
        """The arm of a subject in family *family*: ``treated``, ``control``, or ``None`` if unassigned.

        Treated is a variant that names the family's treatment, control any other variant.
        """
        member = self._families[family]
        if ordinal in member.treated:
            return "treated"
        return "control" if ordinal in member.assigned else None

    def segment_of(self, family: int, ordinal: int) -> Optional[str]:
        """The segment level of family *family* that selects the subject, if any."""
        return self._families[family].segment_of.get(ordinal)
