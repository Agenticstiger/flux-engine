# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Metamorphic relation 1: the same seed gives the same bytes.

Levels, each with its own pass criterion:

* **Generator.** ``generate_rows`` twice under one root, and once under a hostile
  ambient ``decimal`` context (``prec = 5``, ``ROUND_UP``), give identical rows
  (compared by ``repr``, so ``Decimal('1.0')`` and ``Decimal('1.00')`` differ) and
  an identical ``generatorDigest``.
* **In process, end to end.** ``fluxsim plan`` then ``fluxsim apply --run-id`` on
  ``tests/fixtures/telco-fidelity-small`` into two fresh state directories: every
  emit stream, the seam attestation, the population, event and decision traces,
  the receipt and the run-digest file are byte-identical (the OpenLineage file is
  compared with its wall-clock ``eventTime`` removed: no digest covers it), and so are each emit's
  ``outputDigest`` and the ``runDigest``.
* **Across processes.** The same run in a fresh interpreter with another
  ``PYTHONHASHSEED`` is byte-identical to the in-process run.
* **Hostile ambient ``decimal`` context.** The same run in a child whose main
  thread's context is ``prec = 5``, ``ROUND_UP`` must be byte-identical too. It used
  to fail in two ways, because the run read the caller's context:
  ``PersonaSpec.from_document`` raised ``decimal.InvalidOperation`` at ``prec < 6``,
  and the gap rate in ``runtime/signals.py`` moved signal times by a millisecond at
  ``prec`` 6 or 7 with ``ROUND_UP``. Cheap committed cases under ``regressions/`` pin
  both.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy", "hypothesis_jsonschema")

from pathlib import Path  # noqa: E402
from typing import Dict  # noqa: E402

import pytest  # noqa: E402

from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.properties import _support as support  # noqa: E402
from tests.accuracy.properties.checks import check_generator_same_bytes  # noqa: E402
from tests.accuracy.properties.counterexamples import saving_counterexample  # noqa: E402

FAMILY = "properties"
CRITERION = "byte-identical output"


def test_generator_same_seed_same_rows(tier: Tier, accuracy_log: ResultLog) -> None:
    rows = tier.pick(pr=24, nightly=200)
    seeds = tier.seeds(tier.pick(pr=4, nightly=25), "mr1", "generator")
    for seed in seeds:
        payload = {"seed": seed, "rows": rows}
        with saving_counterexample("mr1_generator_same_bytes", payload):
            check_generator_same_bytes(payload)
    accuracy_log.record(
        family=FAMILY,
        case="mr1-generator-same-bytes",
        criterion="rows and generatorDigest identical, in a hostile decimal context too",
        passed=True,
        n=rows,
        metrics={"seeds": list(seeds)},
    )


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory, tier: Tier) -> Dict[str, object]:
    """The in-process run twice, and one run in a child with another ``PYTHONHASHSEED``."""
    root = tmp_path_factory.mktemp("mr1")
    first = support.plan_and_apply(support.SMALL_BUNDLE, root / "first")
    second = support.plan_and_apply(support.SMALL_BUNDLE, root / "second")
    hash_seed = _hash_seed(tier)
    child = support.hostile_child_run(support.SMALL_BUNDLE, root / "child", hash_seed=hash_seed)
    return {"first": first, "second": second, "child": child, "hash_seed": hash_seed}


def _hash_seed(tier: Tier) -> str:
    return str(1 + tier.seed("mr1", "pythonhashseed") % 4_000_000_000)


def _assert_same(left: support.RunBytes, right: support.RunBytes, what: str) -> None:
    assert left.run_id == right.run_id, f"{what}: run ids differ"
    assert sorted(left.files) == sorted(right.files), f"{what}: different files written"
    for name, content in left.files.items():
        assert content == right.files[name], f"{what}: {name} differs"
    assert left.output_digests == right.output_digests, f"{what}: outputDigest differs"
    assert left.run_digest == right.run_digest, f"{what}: runDigest differs"


def test_full_run_twice_in_process(runs: Dict[str, object], accuracy_log: ResultLog) -> None:
    first, second = runs["first"], runs["second"]
    assert first.output_digests, "the small bundle declares an emit; none was recorded"
    _assert_same(first, second, "in-process rerun")
    accuracy_log.record(
        family=FAMILY,
        case="mr1-run-twice",
        criterion=CRITERION,
        passed=True,
        metrics={"runDigest": first.run_digest, "files": sorted(first.files)},
    )


def test_full_run_in_child_with_another_hash_seed(
    runs: Dict[str, object], accuracy_log: ResultLog
) -> None:
    _assert_same(runs["first"], runs["child"], "child process, other PYTHONHASHSEED")
    accuracy_log.record(
        family=FAMILY,
        case="mr1-run-child-hashseed",
        criterion=CRITERION + " under another PYTHONHASHSEED",
        passed=True,
        metrics={"pythonHashSeed": runs["hash_seed"], "runDigest": runs["child"].run_digest},
    )


def test_full_run_under_hostile_decimal_context(
    tmp_path: Path, runs: Dict[str, object], tier: Tier, accuracy_log: ResultLog
) -> None:
    result = None
    try:
        result = support.hostile_child_run(
            support.SMALL_BUNDLE,
            tmp_path / "hostile",
            hash_seed=_hash_seed(tier),
            prec=5,
            rounding="ROUND_UP",
        )
        _assert_same(runs["first"], result, "hostile decimal context, prec=5 ROUND_UP")
    finally:
        accuracy_log.record(
            family=FAMILY,
            case="mr1-run-hostile-decimal",
            criterion=CRITERION + " under prec=5 ROUND_UP",
            passed=result is not None and result.run_digest == runs["first"].run_digest,
            metrics={"completed": result is not None},
        )
