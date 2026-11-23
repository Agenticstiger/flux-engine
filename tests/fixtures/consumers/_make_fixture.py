# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How ``tests/fixtures/consumers`` was made, and how to make it again.

The fixture is one small FLUID contract (``subscriptions.fluid.yaml``, a single expose), a
profile of it (``subscriptions.profile.json``, the newest format the engine reads, with the
table's statistics and a ``queries`` section for two calibration queries) and the bundle in
``bundle/`` that ``fluxsim init bundle --contract subscriptions.fluid.yaml --profile
subscriptions.profile.json`` wrote. The only edit made to that bundle is the ``consumers`` block
added to its Simulation, which declares three queries (two calibration, one held out), one
view and a ``calibration`` block over one assumed value (``CONSUMERS_BLOCK`` below).

``consumers.yml`` holds the same block as a file for ``--consumers``, without its ``calibration`` block; ``subscriptions.profile-request.yml``
and ``subscriptions.held-out-request.yml`` are the two requests ``init`` writes for the contract and that
file with no profile. ``subscriptions.held-out-profile.json`` answers the second: a profile document whose
``queries`` section holds the held-out query's rate and that no bundle pins.

Run ``python tests/fixtures/consumers/_make_fixture.py`` to write the profile, the bundle and the requests
again.
Everything is deterministic, so the files do not change unless this script does.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

HERE = Path(__file__).resolve().parent
NAMESPACE = "com.agentics.flux/1"
ROWS = 1200
SAMPLE = {"min": 1000, "max": 9999}

CONSUMERS_BLOCK: Dict[str, Any] = {
    "dialect": "snowflake",
    "asOf": "2026-06-30",
    "tables": {"prod.analytics.subscriptions": "subscriptions"},
    "prefixes": ["prod.analytics"],
    "columns": {"subscriptions": {"sub_plan": "plan"}},
    "views": [{"name": "offered", "sql": "SELECT * FROM subscriptions WHERE offered"}],
    "queries": [
        {
            "id": "premium-renewed",
            "sql": "SELECT * FROM prod.analytics.subscriptions WHERE sub_plan = 'premium' AND renewed",
            "unit": "subscriptions",
            "tolerance": "0.02",
        },
        {
            "id": "renewed-offered",
            "sql": "SELECT plan FROM offered WHERE renewed",
            "unit": "offered",
            "groupBy": "plan",
        },
        {
            "id": "renewed-since",
            "sql": "SELECT * FROM subscriptions WHERE renewed_at >= :since",
            "parameters": {"since": "2025-03-01"},
            "unit": "subscriptions",
            "heldOut": True,
        },
    ],
    "limits": {"seconds": 30, "memory": "1GiB"},
    # One assumed value of the bundle, which no rate of a query depends on: the fixture declares
    # what a fit may move, and the tests that fit build their own bundle from it (tests/calibrate).
    "calibration": {
        "parameters": [
            {
                "document": "simulation.flux.yml",
                "pointer": (
                    "/extensions/com.agentics.flux~11/emits/subscriptions/fidelity/columns/"
                    "renewed_at/binWeights/0"
                ),
                "min": "0.5",
                "max": "2",
                "step": "0.5",
            }
        ],
        "evaluations": 24,
        "scale": "0.25",
        "seed": 7,
    },
}


def _metric(definition: str, **fields: Any) -> Dict[str, Any]:
    return {
        "definition": definition,
        "disclosure": "acro-default",
        "sampleSize": SAMPLE,
        "status": "F",
        **fields,
    }


def _counts(count: int, distinct: Optional[int] = None) -> Dict[str, Any]:
    metrics = {
        "count": _metric("count/1", value=count),
        "nullCount": _metric("nullCount/1", value=0),
    }
    if distinct is not None:
        metrics["distinctCount"] = _metric("distinctCount/1", value=distinct)
    return metrics


def _histogram(levels: List[str], counts: List[int]) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": levels,
        "disclosure": "acro-default",
        "sampleSize": SAMPLE,
        "groups": [{"group": "", "k": sum(counts), "levelCounts": counts, "status": "F"}],
    }


def _category(levels: List[str], counts: List[int]) -> Dict[str, Any]:
    ordered = sorted(zip(levels, counts, strict=True))  # a profile lists its levels sorted
    levels, counts = [name for name, _ in ordered], [count for _, count in ordered]
    return {
        "domain": "category",
        "metrics": _counts(sum(counts), len(levels)),
        "statistics": [_histogram(levels, counts)],
    }


def profile() -> Dict[str, Any]:
    day = 86_400_000
    first, last = 20_089 * day, 20_270 * day  # 2025-01-01 and 2025-06-30 as days since the epoch
    return {
        "_producer": "https://github.com/Agenticstiger/flux-engine",
        "_schemaURL": (
            "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json"
        ),
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "acro-default": {
                "minimumCell": 10,
                "zerosDisclosive": True,
                "roundingBase": 5,
                "dominance": {"n": 2, "k": "0.90"},
                "pRatio": "0.10",
            }
        },
        "tables": {
            "subscriptions": {
                "rowCount": ROWS,
                "columns": {
                    "subscription_id": {"domain": "category", "metrics": _counts(ROWS, ROWS)},
                    "plan": _category(["basic", "standard", "premium"], [600, 360, 240]),
                    "region": _category(["north", "south", "east", "west"], [400, 300, 300, 200]),
                    "offered": _category(["false", "true"], [300, 900]),
                    "renewed": _category(["false", "true"], [420, 780]),
                    "renewed_at": {
                        "domain": "timestamp",
                        "metrics": {
                            **_counts(780),
                            "min": _metric("min/1", value=str(first)),
                            "max": _metric("max/1", value=str(last)),
                        },
                    },
                    "amount": {
                        "domain": "number",
                        "metrics": {
                            **_counts(ROWS),
                            "min": _metric("min/1", value="5"),
                            "max": _metric("max/1", value="95"),
                            "sum": _metric("sum/1", value="48000"),
                        },
                    },
                },
            }
        },
        "queries": [
            {
                "name": "premium-renewed",
                "statistics": [_histogram(["false", "true"], [1020, 180])],
            },
            {
                "name": "renewed-offered",
                "statistics": [_histogram(["false", "true"], [310, 590])],
            },
        ],
    }


def held_out_profile() -> Dict[str, Any]:
    """The held-out profile: only the held-out query's rate, which no bundle pins.

    A profiler answers ``subscriptions.held-out-request.yml`` with a document of the profile format;
    its ``queries`` section is the only one that matters to ``fluxsim verify --held-out``.
    """
    document = profile()
    document["tables"] = {}
    document["queries"] = [
        {"name": "renewed-since", "statistics": [_histogram(["false", "true"], [480, 720])]}
    ]
    return document


def main() -> None:
    from flux_engine.cli._init import run_init

    (HERE / "subscriptions.profile.json").write_text(
        json.dumps(profile(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (HERE / "subscriptions.held-out-profile.json").write_text(
        json.dumps(held_out_profile(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    bundle = HERE / "bundle"
    if bundle.exists():
        shutil.rmtree(bundle)
    result = run_init(
        str(bundle),
        contract=str(HERE / "subscriptions.fluid.yaml"),
        profile=str(HERE / "subscriptions.profile.json"),
    )
    if result.exit_code != 0:
        raise SystemExit(result.stderr)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["extensions"][NAMESPACE]["consumers"] = CONSUMERS_BLOCK
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    # The two requests `fluxsim init --contract ... --consumers consumers.yml` writes with no profile.
    with tempfile.TemporaryDirectory() as scratch:
        asked = run_init(
            str(Path(scratch) / "bundle"),
            contract=str(HERE / "subscriptions.fluid.yaml"),
            consumers=str(HERE / "consumers.yml"),
        )
        if asked.exit_code != 0:
            raise SystemExit(asked.stderr)
        for written, kept in (
            ("bundle.profile-request.yml", "subscriptions.profile-request.yml"),
            ("bundle.held-out-request.yml", "subscriptions.held-out-request.yml"),
        ):
            shutil.copyfile(Path(scratch) / written, HERE / kept)


if __name__ == "__main__":
    main()
