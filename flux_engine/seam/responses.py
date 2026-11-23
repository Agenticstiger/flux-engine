# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The response families and forecasts a Simulation declares (ADR 0029 sections 2 and 3).

A response family says what an intervention does to one journey's outcome, segment by segment,
as the calibration queries of the ``consumers`` registry measured it. A forecast item says which
cell of the next state's profile a family's arm will show. Both are declared in the FLUX bundle,
under two top-level keys of the namespace:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        responses:
          - id: offer-by-plan
            treatment: retention-offer
            experiment: offer-test
            journey: renewal
            segments: { basic: seg-basic, premium: seg-premium }
            estimate: { treated: renewed-offered, control: renewed-not-offered, confidence: "0.9" }
        forecasts:
          playback: pb-renewal
          confidence: "0.9"
          items:
            - { id: premium-offered, response: offer-by-plan, arm: treated, segment: premium }

This module reads the shape of the two blocks (:func:`read_responses_block`,
:func:`read_forecasts_block`) and checks them against the bundle's documents and registry
(:func:`check_responses`). It applies nothing: **a declaration changes no generated row**. Both
keys enter ``inputDigest`` through the Simulation's bytes. They are bound into no emit's ``H``,
because a journey's own parameters are bound into none, and into no ``G``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, TypeGuard

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.seam.consumers import ConsumerRegistry
from flux_engine.seam.scorecard import _operand

__all__ = [
    "BASES",
    "DEFAULT_BASIS",
    "DEFAULT_CONFIDENCE",
    "FORECASTS_KEY",
    "RESPONSES_KEY",
    "ARMS",
    "Estimate",
    "ForecastItem",
    "Forecasts",
    "ResponseFamily",
    "check_responses",
    "least_sweep_runs",
    "read_forecasts_block",
    "read_responses_block",
]

#: The top-level keys of the namespace that declare response families and forecasts.
RESPONSES_KEY = "responses"
FORECASTS_KEY = "forecasts"

#: The World temperature dial that dials a journey's reactions (``flux_engine.runtime.journey``
#: ``REACTION_DIAL``). Spelled here because the seam reads documents and imports no runtime.
REACTION_DIAL = "campaignResponse"

ARMS: Tuple[str, ...] = ("treated", "control")
BASES: Tuple[str, ...] = ("historical-holdout", "live-ab")
DEFAULT_BASIS = "historical-holdout"
DEFAULT_CONFIDENCE = "0.9"

_MOST_FAMILIES = 256
_MOST_ITEMS = 1024
_MOST_SEGMENTS = 1024
#: The most sweep runs a forecast's confidence may need (``FLX-3674``).
MOST_SWEEP_RUNS = 100_000

_RESPONSES_ITEM_KEYS = ("id", "treatment", "experiment", "journey", "segments", "estimate")
_ESTIMATE_KEYS = ("treated", "control", "confidence")
_FORECASTS_KEYS = ("playback", "confidence", "items")
_ITEM_KEYS = ("id", "response", "arm", "segment", "basis")

#: The registry's id grammar (``flux_engine.seam.consumers``), which a family and an item follow.
_ID = re.compile(r"[a-z][a-z0-9._-]{0,127}")

FLX_3660 = register(
    "FLX-3660",
    "the responses block is not a list of families of the keys they carry",
    "A Simulation declares its response families under "
    "extensions['com.agentics.flux/1'].responses, a list of 1 to 256 mappings that each carry "
    "id, treatment, experiment, journey, segments and estimate and nothing else. A key a family "
    "does not carry is refused rather than ignored, since a declaration nothing reads goes "
    "missing without anyone noticing. Fix the family named in the message.",
)

FLX_3661 = register(
    "FLX-3661",
    "a response family has a malformed or repeated id",
    "A family's id is a lowercase letter followed by at most 127 lowercase letters, digits, '.', "
    "'-' and '_', the grammar of a consumer query id, and it is unique across the families.",
)

FLX_3662 = register(
    "FLX-3662",
    "a response family names a document the bundle does not have, or one of the wrong kind",
    "treatment names a Treatment of the bundle, experiment an Experiment with a variant whose "
    "treatmentRef is that treatment, journey a Journey, and each segments value a Segment. A "
    "reference that resolves to nothing, or to a document of another kind, would leave the "
    "family applying to nothing.",
)

FLX_3663 = register(
    "FLX-3663",
    "a response family's segments are malformed",
    "segments maps a level of the estimate's groupBy column, written as a non-empty text, to the "
    "id of a Segment. A family names at least 1 and at most 1,024 segments.",
)

FLX_3664 = register(
    "FLX-3664",
    "a response family's estimate does not name two grouped calibration queries",
    "estimate.treated and estimate.control name queries of the consumers registry that are not "
    "heldOut, and both carry a groupBy over the same column. confidence is an exact decimal "
    'strictly between 0 and 1, written as a string, such as "0.9". A family with no consumers '
    "registry has no query to name.",
)

FLX_3665 = register(
    "FLX-3665",
    "a response family sits on a journey whose reactions the World's temperature dials",
    "A World that declares the campaignResponse dial allocates each journey step's frozen share "
    "over a group of subjects, and splitting that group by segment and arm would move subjects "
    "the family does not touch. Remove the dial from the World, or the family.",
)

FLX_3666 = register(
    "FLX-3666",
    "the forecasts block is not a mapping of the keys it carries, or has no responses to read",
    "A Simulation declares its forecasts under extensions['com.agentics.flux/1'].forecasts, a "
    "mapping of playback, confidence and items and nothing else, with at most 1,024 items. "
    "Forecasts read response families, so a Simulation that declares forecasts declares "
    "responses too.",
)

FLX_3667 = register(
    "FLX-3667",
    "a forecast item has a malformed or repeated id",
    "An item's id follows the grammar of a response family id and is unique across the items.",
)

FLX_3668 = register(
    "FLX-3668",
    "a forecast item names a response, an arm, a segment or a basis that does not exist",
    "An item's response names a family of the responses block, arm is treated or control, "
    "segment is a key of that family's segments, and basis is historical-holdout (the default) "
    "or live-ab.",
)

FLX_3669 = register(
    "FLX-3669",
    "a forecasts playback or confidence is not usable",
    "playback, when present, names a Playback of the bundle. confidence is an exact decimal "
    'strictly between 0 and 1, written as a string, and is "0.9" when absent.',
)

FLX_3670 = register(
    "FLX-3670",
    "a Simulation declares forecasts without a sweep long enough for their interval",
    "A forecast is the median of the runs of spec.sweep, and its interval runs between the "
    "values of rank a and R + 1 - a, with a the largest whole number not above R (1 - c) / 2. A "
    "Simulation with no sweep, or one whose runs R leave a below 1 (fewer than 20 runs at 0.9), "
    "cannot give an interval at its confidence c.",
)

FLX_3674 = register(
    "FLX-3674",
    "a forecasts confidence needs more sweep runs than the engine allows",
    "The sweep of a forecast needs the least whole number of runs R with R (1 - c) / 2 at least "
    "1, which is 2 / (1 - c) rounded up, so a confidence c close to 1 needs a very long sweep: "
    "0.99998 needs 100,000. A confidence that needs more than 100,000 runs is refused where the "
    "forecasts block is read, naming the block, since no sweep could be run for it. Declare a "
    "lower confidence.",
)


@dataclass(frozen=True)
class Estimate:
    """The two calibration queries a family reads and the confidence of its interval."""

    treated: str
    control: str
    confidence: Fraction
    declared_confidence: str


@dataclass(frozen=True)
class ResponseFamily:
    """One response family, as written."""

    id: str
    treatment: str
    experiment: str
    journey: str
    #: ``(level, Segment id)`` pairs sorted by level.
    segments: Tuple[Tuple[str, str], ...]
    estimate: Estimate


@dataclass(frozen=True)
class ForecastItem:
    """One forecast: a family, an arm, a segment of the family and the claim's basis."""

    id: str
    response: str
    arm: str
    segment: str
    basis: str = DEFAULT_BASIS


@dataclass(frozen=True)
class Forecasts:
    """The ``forecasts`` block as read."""

    items: Tuple[ForecastItem, ...] = ()
    playback: Optional[str] = None
    confidence: Fraction = Fraction(9, 10)
    declared_confidence: str = DEFAULT_CONFIDENCE


def _mapping(value: Any, at: str, keys: Sequence[str], code: ErrorCode) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FluxError(code, f"{at} is a mapping of {list(keys)}")
    unknown = sorted(str(key) for key in value if str(key) not in keys)
    if unknown:
        raise FluxError(code, f"{at} declares {unknown}; it carries {list(keys)}")
    return value


def _is_list(value: Any) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _text(value: Any, at: str, code: ErrorCode) -> str:
    if not isinstance(value, str) or not value:
        raise FluxError(code, f"{at} is a non-empty string")
    return nfc(value)


def _name(mapping: Mapping[str, Any], key: str, at: str, code: ErrorCode) -> str:
    return _text(mapping.get(key), f"{at}.{key}", code)


def _id(value: Any, at: str, code: ErrorCode) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(nfc(value)):
        raise FluxError(
            code,
            f"{at}.id is a lowercase letter then at most 127 lowercase letters, digits, "
            "'.', '-' and '_'",
        )
    return nfc(value)


def _confidence(value: Any, at: str, code: ErrorCode) -> Tuple[Fraction, str]:
    exact, text = _operand(value, at, code)
    if not 0 < exact < 1:
        raise FluxError(code, f"{at} is an exact decimal strictly between 0 and 1")
    return exact, text


def _segments(raw: Any, at: str) -> Tuple[Tuple[str, str], ...]:
    if not isinstance(raw, Mapping) or not raw:
        raise FluxError(FLX_3663, f"{at}.segments is a mapping of at least 1 level to a Segment")
    if len(raw) > _MOST_SEGMENTS:
        raise FluxError(FLX_3663, f"{at}.segments holds more than {_MOST_SEGMENTS} segments")
    pairs: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise FluxError(FLX_3663, f"{at}.segments has the key {key!r}; a key is a level text")
        level = nfc(key)
        if level in pairs:
            raise FluxError(FLX_3663, f"{at}.segments declares the level {level!r} twice")
        pairs[level] = _text(value, f"{at}.segments[{level}]", FLX_3663)
    return tuple(sorted(pairs.items()))


def _estimate(raw: Any, at: str) -> Estimate:
    mapping = _mapping(raw, f"{at}.estimate", _ESTIMATE_KEYS, FLX_3664)
    treated = _name(mapping, "treated", f"{at}.estimate", FLX_3664)
    control = _name(mapping, "control", f"{at}.estimate", FLX_3664)
    exact, text = _confidence(mapping.get("confidence"), f"{at}.estimate.confidence", FLX_3664)
    return Estimate(treated=treated, control=control, confidence=exact, declared_confidence=text)


def read_responses_block(block: Any, *, where: str) -> Tuple[ResponseFamily, ...]:
    """Read a ``responses`` block, or refuse it with an FLX-366n code naming the family.

    Refused: a block that is not a list, none or more than 256 families, a family with a key it does not
    carry, an id outside the registry's grammar or repeated, a name that is not a non-empty
    string, segments that are not 1 to 1,024 level-to-Segment pairs, and an estimate whose
    confidence is not an exact decimal strictly between 0 and 1. What the names resolve to is
    checked where the bundle is known (:func:`check_responses`).
    """
    if not _is_list(block):
        raise FluxError(FLX_3660, f"{where} is a list of response families")
    if not block or len(block) > _MOST_FAMILIES:
        raise FluxError(FLX_3660, f"{where} holds 1 to {_MOST_FAMILIES} families")
    families = []
    seen = set()
    for index, entry in enumerate(block):
        at = f"{where}[{index}]"
        mapping = _mapping(entry, at, _RESPONSES_ITEM_KEYS, FLX_3660)
        family_id = _id(mapping.get("id"), at, FLX_3661)
        if family_id in seen:
            raise FluxError(FLX_3661, f"{at} declares the family {family_id!r} twice")
        seen.add(family_id)
        at = f"{at} ({family_id})"
        families.append(
            ResponseFamily(
                id=family_id,
                treatment=_name(mapping, "treatment", at, FLX_3660),
                experiment=_name(mapping, "experiment", at, FLX_3660),
                journey=_name(mapping, "journey", at, FLX_3660),
                segments=_segments(mapping.get("segments"), at),
                estimate=_estimate(mapping.get("estimate"), at),
            )
        )
    return tuple(families)


def read_forecasts_block(block: Any, *, where: str) -> Forecasts:
    """Read a ``forecasts`` block, or refuse it with an FLX-366n code naming the item.

    Refused: a block that is not a mapping of ``playback``, ``confidence`` and ``items``, more
    than 1,024 items, an item with a key it does not carry, an id outside the registry's grammar
    or repeated, an arm other than ``treated`` and ``control``, a basis other than
    ``historical-holdout`` and ``live-ab``, and a confidence that is not an exact decimal
    strictly between 0 and 1. Which family and segment an item names is checked where the
    families are known (:func:`check_responses`).
    """
    mapping = _mapping(block, where, _FORECASTS_KEYS, FLX_3666)
    playback = _name(mapping, "playback", where, FLX_3669) if "playback" in mapping else None
    exact, text = (
        _confidence(mapping["confidence"], f"{where}.confidence", FLX_3669)
        if "confidence" in mapping
        else (Fraction(DEFAULT_CONFIDENCE), DEFAULT_CONFIDENCE)
    )
    least_sweep_runs(exact, at=f"{where}.confidence")
    listed = mapping.get("items")
    if not _is_list(listed):
        raise FluxError(FLX_3666, f"{where}.items is a list of forecast items")
    if len(listed) > _MOST_ITEMS:
        raise FluxError(FLX_3666, f"{where}.items holds more than {_MOST_ITEMS} items")
    items = []
    seen = set()
    for index, entry in enumerate(listed):
        at = f"{where}.items[{index}]"
        item = _mapping(entry, at, _ITEM_KEYS, FLX_3668)
        item_id = _id(item.get("id"), at, FLX_3667)
        if item_id in seen:
            raise FluxError(FLX_3667, f"{at} declares the item {item_id!r} twice")
        seen.add(item_id)
        at = f"{at} ({item_id})"
        arm = item.get("arm")
        if arm not in ARMS:
            raise FluxError(FLX_3668, f"{at}.arm is {arm!r}; it is one of {list(ARMS)}")
        basis = item.get("basis", DEFAULT_BASIS)
        if basis not in BASES:
            raise FluxError(FLX_3668, f"{at}.basis is {basis!r}; it is one of {list(BASES)}")
        items.append(
            ForecastItem(
                id=item_id,
                response=_name(item, "response", at, FLX_3668),
                arm=arm,
                segment=_name(item, "segment", at, FLX_3668),
                basis=basis,
            )
        )
    return Forecasts(
        items=tuple(items), playback=playback, confidence=exact, declared_confidence=text
    )


def least_sweep_runs(confidence: Fraction, *, at: str = "confidence") -> int:
    """The fewest sweep runs R with ``floor(R (1 - c) / 2) >= 1``: 20 at 0.9, since 19 gives 0.

    ``R (1 - c) / 2 >= 1`` holds exactly when ``R >= 2 / (1 - c)``, so the least R is that quotient
    rounded up, found from the integers of the fraction, in one step. A confidence that needs more
    than :data:`MOST_SWEEP_RUNS` runs, or none at 1 or above, is ``FLX-3674``, naming *at*.
    """
    gap = confidence.denominator - confidence.numerator
    if gap <= 0:
        raise FluxError(FLX_3674, f"{at} is {confidence}, which no number of runs can give")
    runs = max(2, -(-2 * confidence.denominator // gap))
    if runs > MOST_SWEEP_RUNS:
        raise FluxError(
            FLX_3674,
            f"{at} needs {runs} sweep runs, and at most {MOST_SWEEP_RUNS} are read; "
            "declare a lower confidence",
        )
    return runs


def _document(
    documents: Mapping[str, Mapping[str, Any]], name: str, kind: str, at: str
) -> Mapping[str, Any]:
    found = documents.get(name)
    if found is None or found.get("kind") != kind:
        have = sorted(key for key, doc in documents.items() if doc.get("kind") == kind)
        raise FluxError(
            FLX_3662, f"{at} names {name!r}, which is not a {kind} of the bundle (has {have})"
        )
    return found


def _check_estimate_queries(
    family: ResponseFamily, registry: Optional[ConsumerRegistry], at: str
) -> None:
    """Both estimate queries are consumers queries, not held out, grouped by the same column."""
    groups = []
    for arm in ("treated", "control"):
        name = getattr(family.estimate, arm)
        query = (
            next((q for q in registry.queries if q.id == name), None)
            if registry is not None
            else None
        )
        if query is None:
            raise FluxError(
                FLX_3664,
                f"{at}.estimate.{arm} names {name!r}, which is not a query of the consumers "
                "registry",
            )
        if query.held_out:
            raise FluxError(
                FLX_3664,
                f"{at}.estimate.{arm} names {name!r}, which is heldOut; a calibration "
                "query is not",
            )
        if query.group_by is None:
            raise FluxError(FLX_3664, f"{at}.estimate.{arm} names {name!r}, which has no groupBy")
        groups.append(query.group_by)
    if groups[0] != groups[1]:
        raise FluxError(
            FLX_3664,
            f"{at}.estimate groups its queries by {groups[0]!r} and {groups[1]!r}; both group by "
            "the same column",
        )


def _check_family(
    family: ResponseFamily,
    documents: Mapping[str, Mapping[str, Any]],
    registry: Optional[ConsumerRegistry],
    dialled: bool,
    where: str,
) -> None:
    at = f"{where}: responses ({family.id})"
    _document(documents, family.treatment, "Treatment", f"{at}.treatment")
    experiment = _document(documents, family.experiment, "Experiment", f"{at}.experiment")
    spec = experiment.get("spec")
    variants = spec.get("variants") if isinstance(spec, Mapping) else None
    if not _is_list(variants) or not any(
        isinstance(variant, Mapping)
        and nfc(str(variant.get("treatmentRef", ""))) == family.treatment
        for variant in variants
    ):
        raise FluxError(
            FLX_3662,
            f"{at}.experiment {family.experiment!r} has no variant whose treatmentRef is "
            f"{family.treatment!r}",
        )
    _document(documents, family.journey, "Journey", f"{at}.journey")
    for level, segment in family.segments:
        _document(documents, segment, "Segment", f"{at}.segments[{level}]")
    if dialled:
        raise FluxError(
            FLX_3665,
            f"{at}.journey {family.journey!r}: the World declares the {REACTION_DIAL!r} "
            "temperature dial, which dials every journey's reactions",
        )
    _check_estimate_queries(family, registry, at)


def _check_forecasts(
    forecasts: Forecasts,
    families: Sequence[ResponseFamily],
    documents: Mapping[str, Mapping[str, Any]],
    simulation: Mapping[str, Any],
    where: str,
) -> None:
    at = f"{where}: forecasts"
    if forecasts.playback is not None:
        _document_playback(documents, forecasts.playback, at)
    by_id = {family.id: family for family in families}
    for item in forecasts.items:
        here = f"{at}.items ({item.id})"
        family = by_id.get(item.response)
        if family is None:
            raise FluxError(
                FLX_3668,
                f"{here}.response names {item.response!r}, which is not a family "
                f"(families {sorted(by_id)})",
            )
        levels = [level for level, _ in family.segments]
        if item.segment not in levels:
            raise FluxError(
                FLX_3668,
                f"{here}.segment is {item.segment!r}; the family {family.id!r} has {levels}",
            )
    spec = simulation.get("spec")
    sweep = spec.get("sweep") if isinstance(spec, Mapping) else None
    runs = sweep.get("runs") if isinstance(sweep, Mapping) else None
    least = least_sweep_runs(forecasts.confidence)
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < least:
        raise FluxError(
            FLX_3670,
            f"{at}: spec.sweep.runs is {runs!r}; a forecast at confidence "
            f"{forecasts.declared_confidence} needs at least {least} runs",
        )


def _document_playback(documents: Mapping[str, Mapping[str, Any]], name: str, at: str) -> None:
    found = documents.get(name)
    if found is None or found.get("kind") != "Playback":
        raise FluxError(
            FLX_3669, f"{at}.playback names {name!r}, which is not a Playback of the bundle"
        )


def check_responses(
    families: Sequence[ResponseFamily],
    forecasts: Optional[Forecasts],
    *,
    documents: Mapping[str, Mapping[str, Any]],
    registry: Optional[ConsumerRegistry],
    simulation: Mapping[str, Any],
    where: str,
) -> None:
    """Refuse a declaration the bundle cannot carry, naming the family or item.

    *documents* is the bundle's FLUX documents by id, *registry* the Simulation's ``consumers``
    block (``None`` when it declares none) and *simulation* its document. A family names
    documents of the right kind, a variant of its experiment names its treatment, its queries
    are grouped calibration queries of one column, and its journey is not dialled by the World.
    An item names a family and one of its segments, a Playback it names exists, and the sweep is
    long enough for its confidence. Forecasts with no families are refused.
    """
    if forecasts is not None and not families:
        raise FluxError(
            FLX_3666,
            f"{where}: forecasts is declared, and {RESPONSES_KEY!r} declares no family to read",
        )
    spec = simulation.get("spec")
    world = documents.get(nfc(str(spec.get("worldRef", "")))) if isinstance(spec, Mapping) else None
    world_spec = world.get("spec") if isinstance(world, Mapping) else None
    temperature = world_spec.get("temperature") if isinstance(world_spec, Mapping) else None
    dialled = isinstance(temperature, Mapping) and any(
        nfc(str(key)) == REACTION_DIAL for key in temperature
    )
    for family in families:
        _check_family(family, documents, registry, dialled, where)
    if forecasts is not None:
        _check_forecasts(forecasts, families, documents, simulation, where)
