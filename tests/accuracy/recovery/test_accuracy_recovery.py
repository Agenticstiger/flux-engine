# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Recovery test for ``accuracy register | score | record``.

Predictions with known intervals are scored against readouts drawn from a known truth. The
criteria (``docs/accuracy/DESIGN.md``, "Recovery tests"):

* **Containment.** Each recorded verdict equals an independent containment check,
  ``low <= observed <= high`` on exact decimals, boundaries included; a prediction with no
  interval, or a readout with basis ``none``, is ``not-scored``.
* **Completeness.** Every registered prediction appears in the published record, and the
  record's counts (registered, scored, passed) equal the independent counts.
* **Coverage.** Over many predictions whose intervals are centred on a normal truth at
  nominal coverage ``c``, with readouts drawn from that truth, the within-interval rate is
  consistent with ``c``: ``c`` lies inside the 99% Clopper-Pearson interval around the
  measured rate. The measured rate and the interval are recorded.

Decimals travel in both spellings the CLI accepts, a JSON number and its string, chosen per
field from the seed. The CLI is driven in process (``flux_engine.cli.main``).

Two engine defects this module found are fixed and stay pinned as ordinary cases: a
registered interval bound that is a JSON zero is scored, not dropped, and ``accuracy
record`` publishes the digests ``register`` sealed.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

from flux_engine.cli import main  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import clopper_pearson  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402

FAMILY = "recovery"
#: 99% per check, Bonferroni over the two coverages: a correct engine is red on about 1% of
#: nights, not 2%.
LEVEL = 0.995
REGISTERED_AT = "2026-01-01T00:00:00Z"
OBSERVED_AT = "2026-02-01T00:00:00Z"
HORIZON_END = "2026-01-31T00:00:00Z"
RUN_DIGEST = "sha256:" + "0" * 64
METRIC = "recovery_rate"
WITHIN, OUTSIDE, NOT_SCORED = "within-interval", "outside-interval", "not-scored"


Spelled = Union[str, int, float]


# ---------------------------------------------------------------------------
# One prediction, spelled in the JSON documents the CLI reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One prediction and its readout, with the exact decimals and how each is spelled."""

    pid: str
    predicted: Decimal
    low: Optional[Decimal]
    high: Optional[Decimal]
    observed: Decimal
    #: What the JSON document carries for each field (a string, or a JSON number).
    spelling: Dict[str, Spelled]
    basis: str = "live-ab"

    def expected(self) -> str:
        """The independent verdict: containment on exact decimals."""
        if self.basis == "none" or self.low is None or self.high is None:
            return NOT_SCORED
        return WITHIN if self.low <= self.observed <= self.high else OUTSIDE


def spell(value: Decimal, *, as_number: bool) -> Spelled:
    """*value* as a JSON string, or as the JSON number that denotes the same decimal."""
    if not as_number:
        return format(value, "f")
    number = float(value)
    if Decimal(repr(number)) != value:
        raise HarnessError(f"{value} does not round-trip as a JSON number")
    return number


def make_case(
    pid: str,
    *,
    predicted: str,
    low: Optional[str],
    high: Optional[str],
    observed: str,
    numbers: Sequence[str] = (),
    basis: str = "live-ab",
) -> Case:
    """A case from decimal strings; the fields named in *numbers* are spelled as JSON numbers."""
    texts = {
        "predicted": predicted,
        "interval_low": low,
        "interval_high": high,
        "observed": observed,
    }
    values = {name: None if text is None else Decimal(text) for name, text in texts.items()}
    spelling: Dict[str, Spelled] = {}
    for name, text in texts.items():
        if text is None:
            continue
        if name not in numbers:
            spelling[name] = text
        elif "." in text or "e" in text.lower():
            number = float(text)
            if Decimal(repr(number)) != Decimal(text):
                raise HarnessError(f"{text} is not a JSON-number decimal")
            spelling[name] = number
        else:
            spelling[name] = int(text)
    return Case(
        pid=pid,
        predicted=values["predicted"],  # type: ignore[arg-type]
        low=values["interval_low"],
        high=values["interval_high"],
        observed=values["observed"],  # type: ignore[arg-type]
        spelling=spelling,
        basis=basis,
    )


def prediction_document(case: Case) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "prediction_id": case.pid,
        "run_digest": RUN_DIGEST,
        "metric_id": METRIC,
        "statement": f"{METRIC} stays inside its interval",
        "predicted": case.spelling["predicted"],
        "horizon_end": HORIZON_END,
        "registered_at": REGISTERED_AT,
    }
    for name in ("interval_low", "interval_high"):
        if name in case.spelling:
            document[name] = case.spelling[name]
    return document


def readout_document(case: Case) -> Dict[str, Any]:
    return {
        "prediction_id": case.pid,
        "metric_id": METRIC,
        "observed": case.spelling["observed"],
        "sample_size": 100,
        "basis": case.basis,
        "observed_at": OBSERVED_AT,
    }


# ---------------------------------------------------------------------------
# Driving the CLI
# ---------------------------------------------------------------------------


class CliFailure(RuntimeError):
    """A CLI call the test needs did not succeed: a harness failure, not a defect finding."""


class HarnessError(RuntimeError):
    """A test's own precondition failed. Never an ``AssertionError``, so it cannot be
    read as a finding about the engine."""


def cli(*argv: str) -> Dict[str, Any]:
    """Run ``flux_engine.cli.main`` in process; return its ``--json`` envelope."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        code = main([*argv, "--json"])
    envelope: Dict[str, Any] = json.loads(buffer.getvalue())
    if code != 0 or not envelope["ok"]:
        raise CliFailure(f"{argv}: {envelope['error']}")
    return envelope


@dataclass
class Run:
    """What one register, score and record pass over a set of cases produced."""

    cases: List[Case]
    outcomes: Dict[str, str]
    seals: Dict[str, str]
    record: Dict[str, Any]


def run_pipeline(cases: Sequence[Case], workdir: Path) -> Run:
    """``register`` then ``score`` every case, then publish the ``record`` for the batch."""
    state = workdir / "state"
    scratch = workdir / "docs"
    scratch.mkdir(parents=True, exist_ok=True)
    outcomes: Dict[str, str] = {}
    seals: Dict[str, str] = {}
    for case in cases:
        prediction_path = scratch / "prediction.json"
        prediction_path.write_text(json.dumps(prediction_document(case)), encoding="utf-8")
        registered = cli(
            "accuracy", "register", "--file", str(prediction_path), "--state-dir", str(state)
        )
        seals[case.pid] = registered["artifacts"]["sealDigest"]
        readout_path = scratch / "readout.json"
        readout_path.write_text(json.dumps(readout_document(case)), encoding="utf-8")
        scored = cli(
            "accuracy",
            "score",
            "--prediction-id",
            case.pid,
            "--readout",
            str(readout_path),
            "--state-dir",
            str(state),
        )
        outcomes[case.pid] = scored["artifacts"]["outcome"]
    out = workdir / "record.json"
    cli(
        "accuracy",
        "record",
        "--registered-at",
        REGISTERED_AT,
        "--out",
        str(out),
        "--state-dir",
        str(state),
    )
    record: Dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return Run(cases=list(cases), outcomes=outcomes, seals=seals, record=record)


# ---------------------------------------------------------------------------
# Predictions centred on a known truth
# ---------------------------------------------------------------------------

SIX = Decimal("0.000001")


def quantised(value: float) -> Decimal:
    # float() first: under NumPy 2 the repr of an np.float64 is "np.float64(...)".
    return Decimal(format(float(value), ".6f")).quantize(SIX)


def truth_cases(coverage: float, count: int, seed: int) -> List[Case]:
    """*count* predictions whose intervals hold the truth with probability *coverage*.

    Prediction *i* has its own truth ``Normal(mu_i, sigma_i)``. The interval is the central
    ``coverage`` interval of that truth, from scipy's quantile function, rounded to six
    decimals; the readout is one draw from the same truth. ``mu`` stays well clear of zero so
    that no bound is a zero (zero bounds have their own cases below). Each decimal is spelled
    as a JSON number or as a string, chosen by the seed.
    """
    rng = np.random.default_rng(seed)
    z = float(stats.norm.ppf((1.0 + coverage) / 2.0))
    mu = rng.uniform(20.0, 80.0, size=count)
    sigma = rng.uniform(1.0, 4.0, size=count)
    draws = rng.normal(mu, sigma)
    spellings = rng.integers(0, 2, size=(count, 4)).astype(bool)
    cases = []
    for i in range(count):
        low, high = quantised(mu[i] - z * sigma[i]), quantised(mu[i] + z * sigma[i])
        values = {
            "predicted": quantised(mu[i]),
            "interval_low": low,
            "interval_high": high,
            "observed": quantised(draws[i]),
        }
        spelling = {
            name: spell(value, as_number=bool(spellings[i, j]))
            for j, (name, value) in enumerate(values.items())
        }
        cases.append(
            Case(
                pid=f"p-{coverage}-{i:05d}".replace(".", "_"),
                predicted=values["predicted"],
                low=low,
                high=high,
                observed=values["observed"],
                spelling=spelling,
            )
        )
    return cases


COVERAGES = (0.8, 0.95)


@pytest.fixture(scope="module", params=COVERAGES, ids=[f"c={c}" for c in COVERAGES])
def truth_run(
    request: pytest.FixtureRequest, tier: Tier, tmp_path_factory: pytest.TempPathFactory
) -> Tuple[float, int, int, Run]:
    coverage: float = request.param
    count = tier.pick(pr=50, nightly=1000)
    seed = tier.seed("accuracy-recovery", coverage, "truth")
    cases = truth_cases(coverage, count, seed)
    run = run_pipeline(cases, tmp_path_factory.mktemp(f"truth-{coverage}"))
    return coverage, count, seed, run


def test_every_verdict_equals_an_independent_containment_check(
    truth_run: Tuple[float, int, int, Run], accuracy_log: ResultLog
) -> None:
    coverage, count, seed, run = truth_run
    wrong = {
        c.pid: (run.outcomes[c.pid], c.expected())
        for c in run.cases
        if run.outcomes[c.pid] != c.expected()
    }
    accuracy_log.record(
        family=FAMILY,
        case=f"accuracy-containment-c{coverage}",
        criterion="each verdict equals low <= observed <= high on exact decimals",
        passed=not wrong,
        seed=seed,
        n=count,
        metrics={"mismatches": len(wrong)},
    )
    assert not wrong, dict(list(wrong.items())[:5])


def test_every_registered_prediction_is_in_the_record(
    truth_run: Tuple[float, int, int, Run], accuracy_log: ResultLog
) -> None:
    coverage, count, seed, run = truth_run
    record = run.record
    expected_within = sum(1 for c in run.cases if c.expected() == WITHIN)
    accuracy_log.record(
        family=FAMILY,
        case=f"accuracy-record-c{coverage}",
        criterion="the record covers every registered prediction and counts the independent passes",
        passed=record["predictions_registered"] == count
        and record["predictions_scored"] == count
        and record["passed"] == expected_within,
        seed=seed,
        n=count,
        metrics={"registered": record["predictions_registered"], "passed": record["passed"]},
    )
    assert record["registered_at"] == REGISTERED_AT
    assert record["kind"] == "AccuracyRecord"
    assert record["predictions_registered"] == count
    assert record["predictions_scored"] == count
    assert record["passed"] == expected_within
    assert record["targets"] == [METRIC]
    # One digest per prediction, no two alike: nothing was dropped or counted twice.
    assert len(record["predictionDigests"]) == count == len(set(record["predictionDigests"]))


def test_the_record_publishes_the_sealed_digests(
    truth_run: Tuple[float, int, int, Run], accuracy_log: ResultLog
) -> None:
    coverage, count, seed, run = truth_run
    published = set(run.record["predictionDigests"])
    sealed = set(run.seals.values())
    accuracy_log.record(
        family=FAMILY,
        case=f"accuracy-record-seal-digests-c{coverage}",
        criterion="the record's predictionDigests are the sealDigests register returned",
        passed=published == sealed,
        seed=seed,
        n=count,
        metrics={"matching": len(published & sealed)},
    )
    assert published == sealed


def test_within_interval_rate_is_consistent_with_the_nominal_coverage(
    truth_run: Tuple[float, int, int, Run], accuracy_log: ResultLog
) -> None:
    coverage, count, seed, run = truth_run
    within = sum(1 for outcome in run.outcomes.values() if outcome == WITHIN)
    low, high = clopper_pearson(within, count, LEVEL)
    accuracy_log.record(
        family=FAMILY,
        case=f"accuracy-coverage-c{coverage}",
        criterion=f"nominal coverage lies inside the {LEVEL:.1%} Clopper-Pearson interval of the measured rate",
        passed=low <= coverage <= high,
        seed=seed,
        n=count,
        metrics={
            "nominal": coverage,
            "withinInterval": within,
            "rate": within / count,
            "ciLow": low,
            "ciHigh": high,
            "level": LEVEL,
        },
    )
    assert (
        low <= coverage <= high
    ), f"{within}/{count} within; CI [{low:.4f}, {high:.4f}] vs {coverage}"


def test_the_coverage_criterion_has_power_against_a_wrong_truth(tier: Tier) -> None:
    """Readouts with 1.6 times the spread the intervals assumed fall outside the criterion.

    A negative control for the test above, through the engine's own scoring function and
    without the CLI: if the criterion could not fail, passing it would say nothing.
    """
    from flux_engine.accuracy.records import score
    from flux_engine.api.accuracy import ObservedReadout, PredictionRecord
    from flux_engine.api.claims import ClaimBasis
    from flux_engine.core.digest import DigestDomain, DigestRef

    count = 2000
    coverage = 0.8
    rng = np.random.default_rng(tier.seed("accuracy-recovery", "negative-control"))
    z = float(stats.norm.ppf((1.0 + coverage) / 2.0))
    within = 0
    digest = DigestRef(value=RUN_DIGEST, domain=DigestDomain.JCS_DOCUMENT, subject="runDigest")
    for i in range(count):
        mu, sigma = float(rng.uniform(20, 80)), float(rng.uniform(1, 4))
        prediction = PredictionRecord(
            prediction_id=f"n{i}",
            run_digest=digest,
            metric_id=METRIC,
            statement="s",
            predicted=quantised(mu),
            horizon_end=HORIZON_END,
            registered_at=REGISTERED_AT,
            interval_low=quantised(mu - z * sigma),
            interval_high=quantised(mu + z * sigma),
        )
        readout = ObservedReadout(
            prediction_id=f"n{i}",
            metric_id=METRIC,
            observed=quantised(float(rng.normal(mu, 1.6 * sigma))),
            sample_size=100,
            basis=ClaimBasis.LIVE_AB,
            observed_at=OBSERVED_AT,
        )
        within += score(prediction, readout).outcome.value == WITHIN
    low, high = clopper_pearson(within, count, LEVEL)
    assert not (low <= coverage <= high), (within, low, high)


# ---------------------------------------------------------------------------
# Boundaries, spellings, and cases with nothing to score
# ---------------------------------------------------------------------------

#: (id, low, high, observed, expected, JSON-number fields). Bounds are never zero here.
BOUNDARY_CASES: Tuple[Tuple[str, str, str, str, str, Tuple[str, ...]], ...] = (
    ("at-low", "10", "20", "10", WITHIN, ()),
    ("at-high", "10", "20", "20", WITHIN, ()),
    ("inside", "10", "20", "15.5", WITHIN, ()),
    ("just-below-low", "10", "20", "9.999999", OUTSIDE, ()),
    ("just-above-high", "10", "20", "20.000001", OUTSIDE, ()),
    ("degenerate-point", "7.25", "7.25", "7.25", WITHIN, ()),
    ("degenerate-miss", "7.25", "7.25", "7.250001", OUTSIDE, ()),
    ("negative-interval", "-20", "-10", "-10", WITHIN, ()),
    ("negative-outside", "-20", "-10", "-9.5", OUTSIDE, ()),
    ("straddles-zero", "-5", "5", "0", WITHIN, ()),
    ("numbers-at-low", "0.1", "0.3", "0.1", WITHIN, ("interval_low", "interval_high", "observed")),
    ("numbers-at-high", "0.1", "0.3", "0.3", WITHIN, ("interval_low", "interval_high", "observed")),
    (
        "numbers-above",
        "0.1",
        "0.3",
        "0.30001",
        OUTSIDE,
        ("interval_low", "interval_high", "observed"),
    ),
    ("number-observed-string-bounds", "1.5", "2.5", "2.5", WITHIN, ("observed",)),
    (
        "string-observed-number-bounds",
        "1.5",
        "2.5",
        "1.5",
        WITHIN,
        ("interval_low", "interval_high"),
    ),
    ("integers-as-numbers", "1", "3", "3", WITHIN, ("interval_low", "interval_high", "observed")),
    # More digits than a binary float holds: only an exact decimal comparison separates them.
    ("deep-decimal-at-high", "1", "1.0000000000000000001", "1.0000000000000000001", WITHIN, ()),
    ("deep-decimal-above", "1", "1.0000000000000000001", "1.0000000000000000002", OUTSIDE, ()),
    ("deep-decimal-below", "1.0000000000000000001", "2", "1.0000000000000000000", OUTSIDE, ()),
    ("zero-string-low", "0", "5", "0", WITHIN, ()),
    ("zero-string-high", "-5", "0", "0", WITHIN, ()),
)


def boundary_cases() -> List[Case]:
    cases = [
        make_case(
            f"b-{name}", predicted="12", low=low, high=high, observed=observed, numbers=numbers
        )
        for name, low, high, observed, _, numbers in BOUNDARY_CASES
    ]
    cases += [
        # Nothing to contain the observation in, or nothing measured: never a pass or a fail.
        make_case("b-no-interval", predicted="12", low=None, high=None, observed="12"),
        make_case("b-no-low", predicted="12", low=None, high="20", observed="12"),
        make_case("b-no-high", predicted="12", low="1", high=None, observed="12"),
        make_case("b-basis-none", predicted="12", low="1", high="20", observed="12", basis="none"),
        make_case(
            "b-basis-holdout",
            predicted="12",
            low="1",
            high="20",
            observed="12",
            basis="historical-holdout",
        ),
    ]
    return cases


@pytest.fixture(scope="module")
def boundary_run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return run_pipeline(boundary_cases(), tmp_path_factory.mktemp("boundary"))


@pytest.mark.parametrize("name,expected", [(c[0], c[4]) for c in BOUNDARY_CASES])
def test_boundaries_and_spellings(
    name: str, expected: str, boundary_run: Run, accuracy_log: ResultLog
) -> None:
    case = next(c for c in boundary_run.cases if c.pid == f"b-{name}")
    if case.expected() != expected:  # the independent check must agree with the table
        raise HarnessError(f"{name}: the case table and the independent check disagree")
    accuracy_log.record(
        family=FAMILY,
        case=f"accuracy-boundary-{name}",
        criterion="verdict equals containment at the boundary",
        passed=boundary_run.outcomes[case.pid] == expected,
        n=1,
    )
    assert boundary_run.outcomes[case.pid] == expected


@pytest.mark.parametrize(
    "pid,expected",
    [
        ("b-no-interval", NOT_SCORED),
        ("b-no-low", NOT_SCORED),
        ("b-no-high", NOT_SCORED),
        ("b-basis-none", NOT_SCORED),
        ("b-basis-holdout", WITHIN),
    ],
)
def test_nothing_to_score_is_not_scored(pid: str, expected: str, boundary_run: Run) -> None:
    assert boundary_run.outcomes[pid] == expected


def test_the_record_counts_match_the_independent_counts(boundary_run: Run) -> None:
    record = boundary_run.record
    cases = boundary_run.cases
    assert record["predictions_registered"] == len(cases)
    assert record["predictions_scored"] == sum(1 for c in cases if c.expected() != NOT_SCORED)
    assert record["passed"] == sum(1 for c in cases if c.expected() == WITHIN)
    assert len(set(record["predictionDigests"])) == len(cases)


def test_seeded_boundary_ties_are_within(tier: Tier, tmp_path: Path) -> None:
    """Readouts set exactly on a drawn bound (either one) are within, whatever the spelling."""
    count = tier.pick(pr=20, nightly=200)
    cases = truth_cases(0.9, count, tier.seed("accuracy-recovery", "ties"))
    rng = np.random.default_rng(tier.seed("accuracy-recovery", "ties", "pick"))
    tied = []
    for case in cases:
        on_low = bool(rng.integers(0, 2))
        observed = case.low if on_low else case.high
        assert observed is not None
        spelling = {
            **case.spelling,
            "observed": spell(observed, as_number=bool(rng.integers(0, 2))),
        }
        tied.append(Case(case.pid, case.predicted, case.low, case.high, observed, spelling))
    run = run_pipeline(tied, tmp_path)
    assert {run.outcomes[c.pid] for c in tied} == {WITHIN}


# ---------------------------------------------------------------------------
# Zero bounds
# ---------------------------------------------------------------------------

#: (low, high, observed, expected, JSON-number fields). A zero is spelled as a JSON number
#: in the fields the last element names, and as the string "0" everywhere else.
ZERO_CASES: Tuple[Any, ...] = (
    pytest.param("0", "5", "2", WITHIN, ("interval_low",), id="low-int0-within"),
    pytest.param("0", "5", "7", OUTSIDE, ("interval_low",), id="low-int0-outside"),
    pytest.param("0", "5", "0", WITHIN, ("interval_low", "observed"), id="low-int0-tie"),
    pytest.param("-5", "0", "-1", WITHIN, ("interval_high",), id="high-int0-within"),
    pytest.param("-5", "0", "1", OUTSIDE, ("interval_high",), id="high-int0-outside"),
    pytest.param("0", "0", "0", WITHIN, ("interval_low", "interval_high"), id="both-int0"),
    pytest.param("0.0", "5", "2", WITHIN, ("interval_low",), id="low-float0-within"),
    pytest.param("-5", "0.0", "-1", WITHIN, ("interval_high",), id="high-float0-within"),
    pytest.param("0", "5", "2", WITHIN, (), id="low-string0-within"),
    pytest.param("-5", "0", "1", OUTSIDE, (), id="high-string0-outside"),
    pytest.param("0", "0", "0", WITHIN, (), id="both-string0"),
)


@pytest.mark.parametrize("low,high,observed,expected,numbers", ZERO_CASES)
def test_zero_bounds_are_scored_not_dropped(
    low: str,
    high: str,
    observed: str,
    expected: str,
    numbers: Tuple[str, ...],
    tmp_path: Path,
    accuracy_log: ResultLog,
) -> None:
    case = make_case("z-1", predicted="1", low=low, high=high, observed=observed, numbers=numbers)
    if case.expected() != expected:
        raise HarnessError("the independent check disagrees with the case table")
    run = run_pipeline([case], tmp_path)
    accuracy_log.record(
        family=FAMILY,
        case=f"accuracy-zero-bound-{low}-{high}-{observed}-{'-'.join(numbers) or 'strings'}",
        criterion="a registered zero bound is scored, not dropped",
        passed=run.outcomes[case.pid] == expected,
        n=1,
    )
    assert run.outcomes[case.pid] == expected
