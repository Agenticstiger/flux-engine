# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Experiments: largest-remainder arm sizes, a seeded shuffle, and a readout.

``Experiment.spec.assignment.method`` is ``const: largest_remainder`` --
"integer cohort allocation with zero rounding loss" -- so the arm **sizes** are
:func:`~flux_engine.core.lrm.largest_remainder` over the variant weights and
carry no randomness at all. What randomness there is decides *which* eligible
subject lands in which arm, and it is one Fisher-Yates permutation
(:func:`~flux_engine.core.rng.permutation`) over the canonically sorted eligible
ordinals.

**The re-rooted stream.** KERNEL-SPEC §3 gives ``("experiment", id,
"assignment")`` its own row and says it is "rooted at
``Experiment.spec.assignment.seed``, **not** at ``root``". The assignment stream
is therefore derived from ``root.reseed(assignment_seed)`` -- the same contract
digest ``H``, a different seed -- which is what lets an experiment keep its
assignment while the rest of the run is re-seeded, and what makes changing the
World's seed leave the arms alone. An ``Experiment`` that declares no assignment
seed uses the run's own root.

**The readout is not part of any digest.** ``assignmentDigest`` covers the
``assign`` ops -- who is in which arm -- and nothing here. The per-arm counts
and the interval below are a *reading* of the run, computed after it, by
:class:`TwoProportionReadout` -- exact ``Decimal``, no float anywhere. It sits
behind :class:`ExperimentStatistics`, the port any other readout engine would
satisfy, so which engine ran is recorded in the readout and never inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Dict, FrozenSet, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

from flux_engine.core.lrm import largest_remainder
from flux_engine.core.rng import kernel_context, normal_ppf, permutation
from flux_engine.core.seedtree import SeedTree, SeedTreeRoot, experiment_assignment_path
from flux_engine.core.trace import TraceOp
from flux_engine.expr.types import nfc
from flux_engine.runtime.cast import Cast, strip_range
from flux_engine.world.tracing import TraceLog
from flux_engine.world.values import ONE, ZERO, probability_of

__all__ = [
    "DEFAULT_CONFIDENCE",
    "ArmCounts",
    "CompiledExperiment",
    "ExperimentStatistics",
    "ReadoutRow",
    "TwoProportionReadout",
    "assign_variants",
    "arm_counts",
    "compile_experiments",
    "default_readout",
    "read_out",
]

#: The two-sided confidence the interval is reported at.
DEFAULT_CONFIDENCE = Decimal("0.95")


@dataclass(frozen=True)
class Variant:
    """One arm of an experiment."""

    name: str
    weight: Decimal
    treatment_id: str
    is_control: bool


@dataclass(frozen=True)
class CompiledExperiment:
    """An ``Experiment`` with its variants read and its assignment root resolved."""

    experiment_id: str
    variants: Tuple[Variant, ...]
    assignment_seed: Optional[int]
    stream_ordinal: int

    @property
    def control(self) -> Optional[str]:
        """The variant flagged ``isControl``, or ``None`` when none is."""
        for variant in self.variants:
            if variant.is_control:
                return variant.name
        return None

    @property
    def weights(self) -> Mapping[str, Decimal]:
        """Variant weights in **declared** order -- the order the arms are laid out in."""
        return {variant.name: variant.weight for variant in self.variants}

    def root(self, run_root: SeedTreeRoot) -> SeedTreeRoot:
        """The root this experiment's assignment stream hangs from (§3)."""
        if self.assignment_seed is None:
            return run_root
        return run_root.reseed(self.assignment_seed)


def compile_experiments(cast: Cast) -> Tuple[CompiledExperiment, ...]:
    """Read every ``Experiment`` in *cast*, NFC-sorted by id."""
    built: List[CompiledExperiment] = []
    for experiment_id, document in cast.of_kind("Experiment"):
        spec = document.get("spec", {})
        variants = tuple(
            Variant(
                name=nfc(str(declared["name"])),
                weight=probability_of(
                    declared["weight"], what=f"Experiment {experiment_id!r} variant weight"
                ),
                treatment_id=strip_range(declared.get("treatmentRef", "")),
                is_control=bool(declared.get("isControl", False)),
            )
            for declared in spec.get("variants", []) or []
        )
        assignment = spec.get("assignment", {}) or {}
        seed = assignment.get("seed")
        built.append(
            CompiledExperiment(
                experiment_id=experiment_id,
                variants=variants,
                assignment_seed=int(seed) if seed is not None else None,
                stream_ordinal=cast.stream_ordinal(experiment_id),
            )
        )
    return tuple(built)


def assign_variants(
    *,
    experiment: CompiledExperiment,
    eligible: Sequence[int],
    run_root: SeedTreeRoot,
    trace: TraceLog,
) -> Mapping[int, str]:
    """Assign every eligible subject to an arm, tracing one ``assign`` op each.

    *eligible* is sorted here rather than trusted: the permutation is over
    "canonically sorted eligible ordinals", so a caller that collected the
    eligible set in encounter order must not be able to change the assignment.
    Ops are emitted in ordinal order, so ``assignmentDigest`` does not depend on
    the order the arms were laid out in.
    """
    ordinals = tuple(sorted(set(eligible)))
    if not ordinals or not experiment.variants:
        return {}
    sizes = largest_remainder(len(ordinals), dict(experiment.weights))
    tree = SeedTree(root=experiment.root(run_root))
    stream = tree.stream(experiment_assignment_path(experiment.experiment_id))
    shuffled = permutation(stream, len(ordinals))

    assigned: Dict[int, str] = {}
    slot = 0
    for variant in experiment.variants:
        for _ in range(sizes[variant.name]):
            assigned[ordinals[shuffled[slot]]] = variant.name
            slot += 1
    for ordinal in ordinals:
        trace.record(
            TraceOp.ASSIGN,
            experimentId=experiment.experiment_id,
            ordinal=ordinal,
            variant=assigned[ordinal],
        )
    return assigned


@dataclass(frozen=True)
class ArmCounts:
    """Raw per-arm counts: the only thing a readout is allowed to see."""

    variant: str
    trials: int
    successes: int

    def __post_init__(self) -> None:
        if self.trials < 0 or self.successes < 0 or self.successes > self.trials:
            raise ValueError(f"arm {self.variant!r}: {self.successes} successes of {self.trials}")

    @property
    def rate(self) -> Decimal:
        """Successes over trials, or 0 for an empty arm."""
        if self.trials == 0:
            return ZERO
        with localcontext(kernel_context()):
            return Decimal(self.successes) / Decimal(self.trials)


def arm_counts(
    assignments: Mapping[int, str], successes: Union[Sequence[int], FrozenSet[int]]
) -> Tuple[ArmCounts, ...]:
    """Fold an assignment and a success set into per-arm counts, arm-name sorted."""
    won = frozenset(successes)
    trials: Dict[str, int] = {}
    hits: Dict[str, int] = {}
    for ordinal in sorted(assignments):
        variant = assignments[ordinal]
        trials[variant] = trials.get(variant, 0) + 1
        hits[variant] = hits.get(variant, 0) + (1 if ordinal in won else 0)
    return tuple(
        ArmCounts(variant=variant, trials=trials[variant], successes=hits[variant])
        for variant in sorted(trials, key=nfc)
    )


@dataclass(frozen=True)
class ReadoutRow:
    """One treatment arm compared against the control arm."""

    variant: str
    control: str
    trials: int
    successes: int
    rate: Decimal
    control_rate: Decimal
    difference: Decimal
    interval_low: Decimal
    interval_high: Decimal
    confidence: Decimal
    engine: str


class ExperimentStatistics(Protocol):
    """The port a readout implementation satisfies."""

    @property
    def engine(self) -> str:
        """The name recorded in every row this implementation produces."""

    def compare(
        self, control: ArmCounts, treatment: ArmCounts, *, confidence: Decimal
    ) -> ReadoutRow:
        """Compare one treatment arm against the control arm."""


class TwoProportionReadout:
    """Wald interval on the difference of two proportions, in exact ``Decimal``.

    ``d = p_t - p_c``; ``se = sqrt(p_t(1-p_t)/n_t + p_c(1-p_c)/n_c)``; the
    interval is ``d ± z·se`` with ``z`` from
    :func:`~flux_engine.core.rng.normal_ppf` -- the same decimal ``Φ⁻¹`` the
    kernel's truncated normal uses, so the readout needs no float library and
    no libm. An arm with no trials gets a zero-width interval at zero rather
    than a division by zero: no trials is no evidence, not an infinite one.
    """

    engine = "two-proportion"

    def compare(
        self, control: ArmCounts, treatment: ArmCounts, *, confidence: Decimal
    ) -> ReadoutRow:
        with localcontext(kernel_context()):
            p_c = control.rate
            p_t = treatment.rate
            difference = p_t - p_c
            variance = ZERO
            for arm, rate in ((control, p_c), (treatment, p_t)):
                if arm.trials > 0:
                    variance += rate * (ONE - rate) / Decimal(arm.trials)
            half = ZERO
            if variance > 0:
                z = normal_ppf((ONE + confidence) / 2)
                half = z * variance.sqrt()
            return ReadoutRow(
                variant=treatment.variant,
                control=control.variant,
                trials=treatment.trials,
                successes=treatment.successes,
                rate=p_t,
                control_rate=p_c,
                difference=difference,
                interval_low=difference - half,
                interval_high=difference + half,
                confidence=confidence,
                engine=self.engine,
            )


def default_readout() -> ExperimentStatistics:
    """The exact two-proportion interval, whatever else is installed."""
    return TwoProportionReadout()


def read_out(
    counts: Sequence[ArmCounts],
    *,
    control: Optional[str] = None,
    statistics: Optional[ExperimentStatistics] = None,
    confidence: Decimal = DEFAULT_CONFIDENCE,
) -> Tuple[ReadoutRow, ...]:
    """Compare every non-control arm against the control, arm-name sorted.

    *control* defaults to the first arm by name, which is what an ``Experiment``
    that flags no ``isControl`` leaves the reader with; naming it explicitly is
    always better and :attr:`CompiledExperiment.control` is where it comes from.
    """
    if not counts:
        return ()
    engine = statistics if statistics is not None else default_readout()
    by_name = {arm.variant: arm for arm in counts}
    control_name = nfc(control) if control is not None else sorted(by_name, key=nfc)[0]
    baseline = by_name[control_name]
    return tuple(
        engine.compare(baseline, by_name[name], confidence=confidence)
        for name in sorted(by_name, key=nfc)
        if name != control_name
    )
