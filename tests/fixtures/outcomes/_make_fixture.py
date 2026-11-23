# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How ``tests/fixtures/outcomes`` was made, and how to make it again.

The fixture is the subscriptions contract of ``tests/fixtures/consumers`` (``subscriptions.fluid.yaml``),
a profile of it (``subscriptions.profile.json``) whose ``queries`` section holds the two grouped
calibration queries ``renewed-offered`` and ``renewed-not-offered`` (one grouped cell per plan, as
profile format 1-3-0 states a query's rate per group), and the bundle in ``bundle/``
that ``fluxsim init`` wrote and this script then completes:

* a World of 400 subjects that all start at risk, with the ``engagement`` dial at 1 so that the
  subjects that emit a renewal signal are spread over both Personas, and no ``campaignResponse``
  dial, which a response family refuses; and two Personas, ``persona-basic`` and ``persona-premium``;
* a Segment for each (``seg-basic``, ``seg-premium``);
* a Journey ``jr-renewal`` with a success reaction per Persona, in a Module that binds it, and the
  Signal that triggers it;
* the Treatments ``tr-retention-offer`` and ``tr-no-offer``, and an Experiment ``exp-offer-test``
  with a control variant and an offer variant;
* a Playback ``pb-renewal`` with ``drift`` and ``scorecard``;
* a Simulation with ``spec.sweep.runs`` of 20, the ``consumers`` block and the ``responses`` and
  ``forecasts`` blocks of ADR 0029.

``states/state-1.profile.json`` to ``states/state-3.profile.json`` are three releases of the same
profile, for the walk-forward test (``tests/outcomes/test_walk_forward.py``). They differ in the
query cells and in nothing else, and the cells are drawn here from a stated truth:

* ``TRUTH`` gives, for each plan, the base rate of a renewal among the subscriptions not offered
  the retention offer and the uplift the offer adds, and ``STATE_TRUTH`` which truth each state
  draws from. States 1 and 2 draw from ``TRUTH``; state 3 draws from the same truth except that
  the premium plan has changed, which is the drift one pass of the walk flags.
* ``POPULATION`` gives the records of each plan in each arm. A cell's matches are a binomial draw
  of those records at the truth's rate, made with ``random.Random`` seeded by ``SEED`` and the
  state, the arm and the plan, so the counts are written out and the files are rebuilt exactly.

Run ``python tests/fixtures/outcomes/_make_fixture.py`` to write the profile, the states and the
bundle again. Everything is deterministic, so the files do not change unless this script does.
"""

from __future__ import annotations

import json
import random
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List

import yaml

HERE = Path(__file__).resolve().parent
NAMESPACE = "com.agentics.flux/1"
ROWS = 1200
SAMPLE = {"min": 1000, "max": 9999}
OWNER = {"owner": {"team": "billing"}}

#: ``(k, true count)`` of each plan among the offered and the not offered subscriptions.
OFFERED = {"basic": (450, 282), "premium": (180, 144), "standard": (270, 195)}
NOT_OFFERED = {"basic": (150, 75), "premium": (60, 36), "standard": (90, 60)}

#: The seed of the generator that draws the states' cells.
SEED = 20_260_630

#: ``(base rate, uplift)`` of a renewal for each plan: the rate among subscriptions not offered the
#: retention offer, and what the offer adds to it.
TRUTH: Dict[str, tuple[Fraction, Fraction]] = {
    "basic": (Fraction(50, 100), Fraction(12, 100)),
    "premium": (Fraction(60, 100), Fraction(20, 100)),
    "standard": (Fraction(65, 100), Fraction(7, 100)),
}

#: Premium subscribers renew far less in state 3 and the offer adds less; no other plan changes.
DRIFTED_TRUTH = {**TRUTH, "premium": (Fraction(35, 100), Fraction(15, 100))}

#: The truth each state draws its cells from.
STATE_TRUTH = {1: TRUTH, 2: TRUTH, 3: DRIFTED_TRUTH}

#: The records of each plan among the subscriptions offered and not offered, in every state.
POPULATION = {
    "offered": {"basic": 450, "premium": 180, "standard": 270},
    "not_offered": {"basic": 150, "premium": 60, "standard": 90},
}

CONSUMERS_BLOCK: Dict[str, Any] = {
    "dialect": "snowflake",
    "asOf": "2026-06-30",
    "views": [
        {"name": "offered", "sql": "SELECT * FROM subscriptions WHERE offered"},
        {"name": "not_offered", "sql": "SELECT * FROM subscriptions WHERE NOT offered"},
    ],
    "queries": [
        {
            "id": "renewed-offered",
            "sql": "SELECT plan FROM offered WHERE renewed",
            "unit": "offered",
            "groupBy": "plan",
        },
        {
            "id": "renewed-not-offered",
            "sql": "SELECT plan FROM not_offered WHERE renewed",
            "unit": "not_offered",
            "groupBy": "plan",
        },
    ],
}

RESPONSES_BLOCK: List[Dict[str, Any]] = [
    {
        "id": "offer-by-plan",
        "treatment": "tr-retention-offer",
        "experiment": "exp-offer-test",
        "journey": "jr-renewal",
        "segments": {"basic": "seg-basic", "premium": "seg-premium"},
        "estimate": {
            "treated": "renewed-offered",
            "control": "renewed-not-offered",
            "confidence": "0.9",
        },
    }
]

FORECASTS_BLOCK: Dict[str, Any] = {
    "playback": "pb-renewal",
    "confidence": "0.9",
    "items": [
        {
            "id": "premium-offered",
            "response": "offer-by-plan",
            "arm": "treated",
            "segment": "premium",
            "basis": "historical-holdout",
        },
        {
            "id": "basic-control",
            "response": "offer-by-plan",
            "arm": "control",
            "segment": "basic",
        },
    ],
}


def _document(
    kind: str, doc_id: str, name: str, spec: Dict[str, Any], **top: Any
) -> Dict[str, Any]:
    return {
        "fluxVersion": "0.5.0",
        "kind": kind,
        "id": doc_id,
        "name": name,
        "metadata": OWNER,
        **top,
        "spec": spec,
    }


def _persona(key: str, name: str) -> Dict[str, Any]:
    return _document("Persona", f"persona-{key}", name, {"traits": {"digitalFluency": 0.5}})


def _reaction(probability: float) -> Dict[str, Any]:
    return {"successProbability": probability, "outcome": "renewed", "nextState": "active"}


def documents() -> Dict[str, Dict[str, Any]]:
    """The FLUX documents this script adds to the bundle, by file name."""
    return {
        "world.flux.yml": _document(
            "World",
            "world-telco.silver.subscriptions",
            "World for Subscriptions",
            {
                "seed": 42,
                "vertical": "telco",
                "population": {"size": 400, "lifecycleMix": {"atrisk": 1.0}},
                "temperature": {"engagement": 1.0},
            },
        ),
        "persona-basic.flux.yml": _persona("basic", "Basic Subscriber"),
        "persona-premium.flux.yml": _persona("premium", "Premium Subscriber"),
        "segment-basic.flux.yml": _document(
            "Segment",
            "seg-basic",
            "Basic Subscribers",
            {"query": "persona == 'persona-basic'", "queryLanguage": "flux-expr/1"},
        ),
        "segment-premium.flux.yml": _document(
            "Segment",
            "seg-premium",
            "Premium Subscribers",
            {"query": "persona == 'persona-premium'", "queryLanguage": "flux-expr/1"},
        ),
        "signal.flux.yml": _document(
            "Signal",
            "sig-renewal-due",
            "Renewal Due Signal",
            {
                "envelope": "cloudevents/1.0",
                "source": "urn:flux:billing-gateway",
                "type": "com.telco.renewal.due",
                "dataschema": "https://example.com/schemas/renewal-due.json",
            },
        ),
        "taxonomy.flux.yml": _document(
            "JourneyTaxonomy",
            "jt-subscriber-lifecycle",
            "Subscriber Lifecycle Taxonomy",
            {
                "states": ["active", "atrisk", "churned"],
                "transitions": [
                    {"from": "active", "to": "atrisk"},
                    {"from": "atrisk", "to": "active"},
                    {"from": "atrisk", "to": "churned"},
                ],
            },
        ),
        "journey.flux.yml": _document(
            "Journey",
            "jr-renewal",
            "Renewal Journey",
            {
                "trigger": {"signalRef": "sig-renewal-due"},
                "intervention": {"treatmentRef": "tr-retention-offer", "action": "offer_renewal"},
                "taxonomyRef": "jt-subscriber-lifecycle",
                "personaReactions": {
                    "persona-basic": _reaction(0.5),
                    "persona-premium": _reaction(0.6),
                },
            },
        ),
        "module.flux.yml": _document(
            "Module",
            "module-renewal",
            "Renewal Module",
            {
                "resources": ["sig-renewal-due", "jr-renewal"],
                "binds": [{"port": "event-envelope", "config": {"spec": "cloudevents/1.0"}}],
            },
            version="1.0.0",
        ),
        "treatment-retention-offer.flux.yml": _document(
            "Treatment",
            "tr-retention-offer",
            "Retention Offer",
            {"actionType": "renewal_offer", "payloadTemplate": {"message": "Renew now for less"}},
        ),
        "treatment-no-offer.flux.yml": _document(
            "Treatment",
            "tr-no-offer",
            "No Offer",
            {"actionType": "none", "payloadTemplate": {"message": "Your plan renews"}},
        ),
        "experiment.flux.yml": _document(
            "Experiment",
            "exp-offer-test",
            "Offer Test",
            {
                "variants": [
                    {
                        "name": "control",
                        "weight": 0.5,
                        "treatmentRef": "tr-no-offer",
                        "isControl": True,
                    },
                    {"name": "offer", "weight": 0.5, "treatmentRef": "tr-retention-offer"},
                ],
                "assignment": {"method": "largest_remainder", "seed": 7},
            },
        ),
        "playback.flux.yml": _document(
            "Playback",
            "pb-renewal",
            "Renewal Playback",
            {
                "worldRef": "world-telco.silver.subscriptions",
                "sourceStream": "kafka://billing.events.v1",
                "personaMapping": [
                    {"behaviorPattern": "late_payment_count >= 2", "mapToPersona": "persona-basic"}
                ],
                "calibration": {
                    "targets": ["population.lifecycleMix"],
                    "window": "P90D",
                    "method": "mle",
                },
                "drift": {"metric": "psi", "threshold": 0.2, "onBreach": "warn"},
                "scorecard": {
                    "backtestWindow": "P90D",
                    "grade": "B",
                    "intervalCoverage": 0.92,
                    "credibility": 0.78,
                },
            },
        ),
    }


def _persona_entries() -> List[Dict[str, Any]]:
    """The register entries of the two Personas, which replace the one ``init`` wrote."""
    return [
        {
            "document": f"persona-{key}",
            "pointer": "/spec",
            "source": "default",
            "reason": "The generator emits read none of a Persona's numbers, so they change no row.",
        }
        for key in ("basic", "premium")
    ]


def _metric(definition: str, **fields: Any) -> Dict[str, Any]:
    return {
        "definition": definition,
        "disclosure": "acro-default",
        "sampleSize": SAMPLE,
        "status": "F",
        **fields,
    }


def _counts(count: int, distinct: int | None = None) -> Dict[str, Any]:
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


def _grouped(cells: Dict[str, tuple[int, int]]) -> Dict[str, Any]:
    """A grouped ``levelHistogram/1`` over ``["false", "true"]``: one cell per plan of *cells*.

    Each cell is ``(k, true count)`` of one plan, so a group's ``levelCounts`` are the records the
    query does not match and the records it matches (profile format 1-3-0).
    """
    return {
        "definition": "levelHistogram/1",
        "levels": ["false", "true"],
        "disclosure": "acro-default",
        "sampleSize": SAMPLE,
        "groupBy": "plan",
        "groups": [
            {"group": plan, "k": k, "levelCounts": [k - true, true], "status": "F"}
            for plan, (k, true) in sorted(cells.items())
        ],
    }


def _category(levels: List[str], counts: List[int]) -> Dict[str, Any]:
    ordered = sorted(zip(levels, counts, strict=True))  # a profile lists its levels sorted
    levels, counts = [name for name, _ in ordered], [count for _, count in ordered]
    return {
        "domain": "category",
        "metrics": _counts(sum(counts), len(levels)),
        "statistics": [_histogram(levels, counts)],
    }


def state_cells(state: int) -> Dict[str, Dict[str, tuple[int, int]]]:
    """The offered and not offered cells of *state* as ``(k, true count)`` for each plan.

    A cell is a binomial draw of its records at the state's truth, so the counts are noisy the
    way a release of real counts is. The generator is seeded by the script's seed and by the
    state, the arm and the plan, so no cell depends on another.
    """
    truth = STATE_TRUTH[state]
    cells: Dict[str, Dict[str, tuple[int, int]]] = {}
    for arm_index, arm in enumerate(("offered", "not_offered")):
        cells[arm] = {}
        for plan_index, plan in enumerate(sorted(POPULATION[arm])):
            base, uplift = truth[plan]
            rate = float(base + uplift) if arm == "offered" else float(base)
            records = POPULATION[arm][plan]
            rng = random.Random(SEED + 100 * state + 10 * arm_index + plan_index)
            cells[arm][plan] = (records, sum(rng.random() < rate for _ in range(records)))
    return cells


def profile(
    offered: Dict[str, tuple[int, int]] = OFFERED,
    not_offered: Dict[str, tuple[int, int]] = NOT_OFFERED,
) -> Dict[str, Any]:
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
            {"name": "renewed-offered", "statistics": [_grouped(offered)]},
            {"name": "renewed-not-offered", "statistics": [_grouped(not_offered)]},
        ],
    }


def main() -> None:
    from flux_engine.cli._init import run_init

    (HERE / "subscriptions.profile.json").write_text(
        json.dumps(profile(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    states = HERE / "states"
    states.mkdir(exist_ok=True)
    for state in STATE_TRUTH:
        cells = state_cells(state)
        (states / f"state-{state}.profile.json").write_text(
            json.dumps(profile(cells["offered"], cells["not_offered"]), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
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
    (bundle / "persona.flux.yml").unlink()
    for name, document in documents().items():
        (bundle / name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = document["spec"]
    spec["personaRefs"] = ["persona-basic", "persona-premium"]
    spec["segmentRefs"] = ["seg-basic", "seg-premium"]
    spec["signalRefs"] = ["sig-renewal-due"]
    spec["experimentRefs"] = ["exp-offer-test"]
    spec["moduleRefs"] = ["module-renewal"]
    spec["sweep"] = {"runs": 20}
    namespace = document["extensions"][NAMESPACE]
    namespace["parameters"] = [
        entry for entry in namespace["parameters"] if "persona-telco" not in entry["document"]
    ] + _persona_entries()
    namespace["consumers"] = CONSUMERS_BLOCK
    namespace["responses"] = RESPONSES_BLOCK
    namespace["forecasts"] = FORECASTS_BLOCK
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
