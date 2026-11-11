# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Metamorphic relation 5: adding a column, or a trait, leaves its siblings untouched.

Three levels:

* **Generator column.** Under a fixed ``H`` and root, adding a column at any position
  of the expose leaves every other column's values unchanged. End to end this does not
  hold, by design: a new contract column changes ``H``, which changes the root
  (KERNEL-SPEC §3). Pass criterion: identical ``repr`` per column name.
* **World trait.** ``materialise_population`` under a fixed root: adding a Persona
  trait (scalar, categorical or bounded normal, under any name, sorted before or after
  the others) leaves every other trait, the identifiers, markets, lifecycles and the
  derived columns identical for every subject.
* **End to end.** Personas are not in ``H``, so the claim also holds through the whole
  pipeline: ``apply`` on the small bundle with two traits added to its Persona
  writes the same emit streams and the same population records, event stream and
  decisions, apart from the added traits' own ``trait`` records (and the digests that
  cover them).
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy", "hypothesis_jsonschema")

import json  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, List  # noqa: E402

from hypothesis import assume, given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.properties import _support as support  # noqa: E402
from tests.accuracy.properties.checks import (  # noqa: E402
    check_generator_added_column,
    check_world_added_trait,
)
from tests.accuracy.properties.counterexamples import saving_counterexample  # noqa: E402

FAMILY = "properties"

SEED = st.integers(min_value=0, max_value=2**62)
UNIT = st.sampled_from([0.0, 0.1, 0.25, 0.5, 0.75, 1.0])
TRAIT_NAME = st.from_regex(r"[a-z][A-Za-z0-9]{0,7}", fullmatch=True)

DECLARATION = st.one_of(
    UNIT,
    st.sampled_from(
        [
            {"dist": "categorical", "levels": {"low": 0.5, "high": 0.5}},
            {"dist": "categorical", "levels": {"a": 0.25, "b": 0.25, "c": 0.5}},
        ]
    ),
    st.builds(
        lambda mean, sd, lo, width: {
            "dist": "normal",
            "mean": mean,
            "stddev": sd,
            "min": lo,
            "max": lo + width,
        },
        st.integers(-50, 50).map(float),
        st.sampled_from([0.0, 0.5, 2.0, 15.0]),
        st.integers(-60, 0).map(float),
        st.integers(1, 120).map(float),
    ),
)


def test_adding_a_generator_column(tier: Tier, accuracy_log: ResultLog) -> None:
    examples = {"n": 0}

    @tier.property_settings(pr=40, nightly=400, label="mr5-generator")
    @given(
        seed=SEED,
        position=st.integers(0, len(support.BASE_SCHEMA)),
        name=st.from_regex(r"x_[a-z0-9]{1,6}", fullmatch=True),
        spec=st.sampled_from(sorted(support.EXTRA_COLUMNS)),
    )
    def prop(seed: int, position: int, name: str, spec: str) -> None:
        payload = {
            "seed": seed,
            "rows": tier.pick(pr=6, nightly=24),
            "position": position,
            "name": name,
            "spec": spec,
        }
        with saving_counterexample("mr5_generator_added_column", payload):
            check_generator_added_column(payload)
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr5-generator-added-column",
        criterion="every other column's values identical after adding a column at any position",
        passed=True,
        metrics={"examples": examples["n"]},
    )


def test_adding_a_persona_trait(tier: Tier, accuracy_log: ResultLog) -> None:
    existing = set(support.small_world().persona["spec"]["traits"])
    examples = {"n": 0}

    @tier.property_settings(pr=40, nightly=300, label="mr5-world")
    @given(seed=SEED, name=TRAIT_NAME, declaration=DECLARATION, size=st.integers(1, 40))
    def prop(seed: int, name: str, declaration: Any, size: int) -> None:
        assume(name not in existing)
        payload = {"seed": seed, "size": size, "name": name, "declaration": declaration}
        with saving_counterexample("mr5_world_added_trait", payload):
            check_world_added_trait(payload)
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr5-world-added-trait",
        criterion="every other trait of every subject identical after adding a trait",
        passed=True,
        metrics={"examples": examples["n"]},
    )


def _records(path: Path) -> List[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_adding_a_trait_end_to_end(tmp_path: Path, tier: Tier, accuracy_log: ResultLog) -> None:
    added = {
        "zzAddedNormal": {"dist": "normal", "mean": 3.0, "stddev": 1.0, "min": 0},
        "aaAddedLevels": {"dist": "categorical", "levels": {"x": 0.5, "y": 0.5}},
        "mmAddedScalar": 0.77,
    }
    grown = support.copy_bundle(tmp_path / "grown")
    for name, declaration in added.items():
        support.add_persona_trait(grown, name, declaration)

    before = support.apply_only(support.SMALL_BUNDLE, tmp_path / "state-before")
    after = support.apply_only(grown, tmp_path / "state-after")

    assert before.output_digests == after.output_digests, "an emit's outputDigest moved"
    for name in sorted(n for n in before.files if n.startswith(("emits/", "seam-attestation"))):
        assert before.files[name] == after.files[name], f"{name} moved"
    for name in (
        "events.ndjson",
        "decisions.ndjson",
        "decisions.meta.ndjson",
        "suppressions.ndjson",
    ):
        if name in before.files:
            assert before.files[name] == after.files[name], f"{name} moved"

    def kept(raw: bytes) -> List[dict]:
        return [
            record
            for record in map(json.loads, raw.decode("utf-8").splitlines())
            if not (record.get("op") == "trait" and record.get("name") in added)
        ]

    population_before, population_after = (
        kept(before.files["population.ndjson"]),
        kept(after.files["population.ndjson"]),
    )
    assert population_before == population_after, "a population record moved"
    new_records = [
        r
        for r in map(json.loads, after.files["population.ndjson"].decode("utf-8").splitlines())
        if r.get("op") == "trait" and r.get("name") in added
    ]
    assert new_records, "the added traits were never drawn, so the comparison is vacuous"

    accuracy_log.record(
        family=FAMILY,
        case="mr5-end-to-end-added-trait",
        criterion="emits, events, decisions and every other population record byte-identical",
        passed=True,
        metrics={"addedTraitRecords": len(new_records), "population": len(population_before)},
    )
