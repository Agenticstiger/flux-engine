# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The fit: CMA-ES over declared parameters, scaled to 0 to 1 and bounded (ADR 0028 section 8).

:class:`CmaCalibrator` implements the engine's :class:`~flux_engine.api.calibration.Calibrator` protocol
with the method id ``flux.calibration.cma/1``. It knows nothing of bundles or queries: it is given an
``evaluate`` function that takes the candidate values (each rounded to its ``step``, as exact
:class:`~fractions.Fraction` numbers, in the order of the parameters) and returns the sum of the squared
gaps between each calibration query's rate and its target, and the gap of each query.

* **Determinism.** The optimiser's draws come from cma's own generator, seeded with the calibration's
  seed (the Simulation's when the block gives none), and never from the seed tree. Every evaluation seeds
  its rows from the unpatched bundle's ``H``, so candidates are compared on the same draws. cma seeds numpy's global
  generator to do it, so the state numpy had is put back when the fit ends. cma reads a seed of 0 as "seed
  from the clock", so the seed it is given is the declared one, moved into 1 to 2,147,483,647.
* **One parameter** is searched in two dimensions, the second of which no value reads, because cma 4.6.0
  cannot run a bounded fit in one (pycma issues 86 and 302).
* **The start point** is evaluated as well, before the first candidate, and kept when no candidate beats
  it, so a short fit is never worse than the start. It is not counted against the budget.
* **Budget.** At most ``evaluations`` candidates are evaluated: whole generations, and the first
  candidates of the next one when the budget ends inside it. A candidate that rounds to the values of an
  earlier one is not run again, and still counts against the budget.
* **The best wins, and of equals the earlier one.** The objective is an exact fraction, so a tie is a
  tie and the earlier candidate stays.
* **Aggregate only.** What crosses into :meth:`CmaCalibrator.calibrate` is the pinned profile's digest and
  the names of the queries (ADR 0003); no row of a run is ever handed to it.

cma is imported by :meth:`CmaCalibrator.calibrate` and by nothing before it, with its matplotlib
``UserWarning`` silenced, so importing this module costs nothing when no fit runs.
"""

from __future__ import annotations

import importlib
import warnings
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from flux_engine.api.calibration import Credibility, Provenance, Scorecard
from flux_engine.calibrate.errors import FLX_3708
from flux_engine.calibrate.parameters import FitParameter, exact_text
from flux_engine.calibrate.patch import METHOD_ID
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError

__all__ = ["CmaCalibrator", "Evaluation", "FitResult", "INSTALL_COMMAND", "cma_seed"]

INSTALL_COMMAND = "pip install 'flux-sim-engine[calibrate]'"

#: The spread cma starts with, as a share of the 0 to 1 range every parameter is scaled to.
_SIGMA = 0.3


@dataclass(frozen=True)
class Evaluation:
    """What one candidate scored: the objective, and the gap of each calibration query."""

    objective: Fraction
    #: ``(query id, gap)`` in the registry's order; ``None`` for a query with no gap to state (not
    #: counted, or no target).
    gaps: Tuple[Tuple[str, Optional[Fraction]], ...]


@dataclass(frozen=True)
class FitResult:
    """The best candidate and how the search went."""

    #: The values of the best candidate, in the order of the parameters.
    values: Tuple[Fraction, ...]
    evaluation: Evaluation
    #: Candidates evaluated, those that rounded to an earlier one included.
    evaluations: int
    #: Candidates that were run (the rest were read from an earlier identical one).
    runs: int
    seed: int
    cma_version: str


def cma_seed(seed: int) -> int:
    """*seed* as a seed cma takes: it reads 0 as "from the clock", so the range is 1 to 2**31 - 1."""
    return seed % (2**31 - 1) + 1


def _import_cma() -> Any:
    try:
        with warnings.catch_warnings():
            # cma warns on import that it cannot plot when matplotlib is absent; a fit plots nothing.
            warnings.filterwarnings("ignore", category=UserWarning, message=".*matplotlib.*")
            return importlib.import_module("cma")
    except ModuleNotFoundError as error:
        raise FluxError(
            FLX_3708, f"cma is not installed ({INSTALL_COMMAND}); nothing was run"
        ) from error


class CmaCalibrator:
    """Fits *parameters* to the queries *evaluate* scores, with CMA-ES. See the module docstring."""

    method_id = METHOD_ID

    def __init__(
        self,
        parameters: Sequence[FitParameter],
        evaluate: Callable[[Tuple[Fraction, ...]], Evaluation],
        *,
        evaluations: int,
        seed: int,
    ) -> None:
        self._parameters = tuple(parameters)
        self._evaluate = evaluate
        self._evaluations = evaluations
        self._seed = seed
        #: The last fit's result; ``None`` before :meth:`calibrate` has run.
        self.result: Optional[FitResult] = None

    def calibrate(
        self,
        aggregates: Mapping[str, Any],
        dials: Mapping[str, Decimal],
    ) -> Tuple[Mapping[str, Decimal], Scorecard]:
        """Fit the dials, keyed ``<file>#<pointer>``, and return the best values with a scorecard.

        *aggregates* holds ``profile`` (the digest of the profile that stated the targets) and
        ``queries`` (the calibration queries' ids). The scorecard says the dials were fitted, that
        nothing was graded, and that no credibility is established: only the held-out check
        (ADR 0028 section 9) can call a bundle calibrated.
        """
        cma = _import_cma()
        import numpy as np

        starts = [
            parameter.unit_of(
                Fraction(dials[parameter.key]) if parameter.key in dials else parameter.before
            )
            for parameter in self._parameters
        ]
        state = np.random.get_state()
        try:
            result = self._search(cma, starts)
        finally:
            np.random.set_state(state)
        self.result = result
        fitted: Dict[str, Decimal] = {
            parameter.key: Decimal(exact_text(value))
            for parameter, value in zip(self._parameters, result.values, strict=True)
        }
        evidence = digest_of_document(
            {
                "profile": str(aggregates.get("profile", "")),
                "queries": sorted(aggregates.get("queries", ())),
            },
            subject="calibration-aggregates",
        )
        return fitted, Scorecard(
            provenance=Provenance.FITTED,
            credibility=Credibility.NOT_ESTABLISHED,
            method_id=self.method_id,
            evidence_digest=evidence,
        )

    def _search(self, cma: Any, starts: Sequence[float]) -> FitResult:
        seen: Dict[Tuple[Fraction, ...], Evaluation] = {}
        best: Optional[Tuple[Tuple[Fraction, ...], Evaluation]] = None
        done = 0
        # cma does not support one dimension: a bounded fit of one parameter fails inside it
        # ("not yet initialized (dimension needed)", pycma issues 86 and 302). A single parameter is
        # searched in two dimensions, the second of which no value reads, so the search is as
        # deterministic as any other.
        start = list(starts) + [0.5] * max(0, 2 - len(starts))
        strategy = cma.CMAEvolutionStrategy(
            start,
            _SIGMA,
            {
                "bounds": [0, 1],
                "seed": cma_seed(self._seed),
                "verbose": -9,
                "verb_log": 0,
                "verb_disp": 0,
            },
        )
        # The start point is evaluated as well as the budget's candidates, and it is kept unless a
        # candidate beats it (ties keep the earlier), so a short fit is never worse than the start.
        first = tuple(
            parameter.snap(float(unit))
            for parameter, unit in zip(self._parameters, starts, strict=True)
        )
        seen[first] = self._evaluate(first)
        best = (first, seen[first])
        while done < self._evaluations and not strategy.stop():
            candidates = strategy.ask()
            taken = candidates[: self._evaluations - done]
            scores = []
            for candidate in taken:
                values = tuple(
                    parameter.snap(float(unit))
                    for parameter, unit in zip(
                        self._parameters, candidate[: len(self._parameters)], strict=True
                    )
                )
                if values not in seen:
                    seen[values] = self._evaluate(values)
                evaluation = seen[values]
                done += 1
                if best is None or evaluation.objective < best[1].objective:
                    best = (values, evaluation)
                scores.append(float(evaluation.objective))
            if len(taken) < len(candidates):
                break  # the budget ended inside this generation
            strategy.tell(candidates, scores)
        assert best is not None, "evaluations is at least 1"
        return FitResult(
            values=best[0],
            evaluation=best[1],
            evaluations=done,
            runs=len(seen),
            seed=self._seed,
            cma_version=str(cma.__version__),
        )
