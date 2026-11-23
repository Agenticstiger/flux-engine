# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The lifetimes and the dates within a linked row's lifetime a Simulation configures (ADR 0024).

A Simulation declares them under the top-level ``lifetimes``, ``within`` and ``rates`` keys of its
namespace, in the FLUX bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        lifetimes:
          - { emit: customer, start: cu_start, end: cu_end }
        within:
          - { emit: account, relationship: customer, date: a_open }

A ``lifetimes`` entry names an emitted expose, a ``start`` column and, optionally, an ``end``
column of the same kind, with an optional ``duration`` histogram, an ``endPastWindow`` and a
``target``. A ``within`` entry names an emitted expose, a ``relationship`` (to-one, or a chain of
them written with dots) and exactly one of ``date``, a column of the emit, and ``lifetime: true``,
with an optional ``offset`` histogram, ``target`` and ``exposure: true`` (ADR 0025 section 4). A ``rates`` entry (ADR 0025 section 2) names an
emitted expose and a ``column`` that a ``within`` entry or a lifetime's duration sets, with the
seasonal factors ``month``, ``dayOfWeek`` and ``hourOfWeek`` as lists of weights. Whether the columns
exist and are dates, and whether the relationships are the contract's, is checked where the contract
is known (:mod:`flux_engine.seam.resolve`).

**The entries change the rows the emit generates**, so each is bound into ``H`` before any row is
drawn: :func:`lifetime_bound_into_h`, :func:`within_bound_into_h` and :func:`rates_bound_into_h` are
the members the emit's entry for ``H`` gains, present only when the emit has entries, so a bundle that declares none
hashes exactly as before. The members record each entry as written: a default is read where the
entry is used and never written into them. ``G`` leaves each member out, as it leaves out the
links' (:func:`flux_engine.seam.emit.seam_reusable_digest`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError
from flux_stats.spelling import MOST_DIGITS

__all__ = [
    "END_PAST_WINDOW_VALUES",
    "Duration",
    "DwellDeclaration",
    "DwellFamilyDeclaration",
    "ExponentialDeclaration",
    "Histogram",
    "LIFETIMES_KEY",
    "LifetimeEntry",
    "MOST_HOPS",
    "MOST_STATES",
    "RATES_FACTOR_SIZES",
    "RATES_KEY",
    "RatesEntry",
    "WITHIN_KEY",
    "WithinEntry",
    "lifetime_bound_into_h",
    "duration_declares_open",
    "lifetime_entries_of",
    "rates_bound_into_h",
    "rates_entries_of",
    "read_lifetimes_block",
    "read_rates_block",
    "read_within_block",
    "within_bound_into_h",
    "within_entries_of",
]

#: The top-level keys of the namespace that configure lifetimes and dates within a lifetime, and the
#: members an emit's ``H`` gains.
LIFETIMES_KEY = "lifetimes"
WITHIN_KEY = "within"
RATES_KEY = "rates"

#: What an end after the run's window is: no end, or the drawn date.
END_PAST_WINDOW_VALUES: Tuple[str, ...] = ("open", "kept")

_LIFETIME_KEYS = ("emit", "start", "end", "duration", "endPastWindow", "target")
_WITHIN_KEYS = ("emit", "relationship", "date", "lifetime", "offset", "target", "exposure")
_DURATION_KEYS = ("bins", "binWeights", "closed", "open")
_OFFSET_KEYS = ("bins", "binWeights", "closed")
_HISTOGRAM_FORM_KEYS = ("bins", "binWeights", "closed")
_EXPONENTIAL_KEYS = ("mean", "open")
_FAMILY_KEYS = ("given", "states", "otherwise")
_RATES_KEYS = ("emit", "column", "month", "dayOfWeek", "hourOfWeek")
#: The weights a seasonal factor lists (ADR 0025 section 2): 12 months, 7 weekdays, 168 hours of a week.
RATES_FACTOR_SIZES: Dict[str, int] = {"month": 12, "dayOfWeek": 7, "hourOfWeek": 168}
#: The most digits one weight of a ``rates`` factor may be written with: a window's table of
#: weights holds one integer per cell, as wide as the weights are long (ADR 0025 section 4).
RATES_MOST_DIGITS = 30
#: The most states a dwell family lists (ADR 0025 section 3).
MOST_STATES = 1024
_CLOSED_VALUES = ("lower", "upper")
_MOST_ENTRIES = 4096
_MOST_BINS = 1024
_WHOLE = re.compile(r"-?[0-9]+")
#: The most digits a bin edge has: whole days up to 9 999 999, about 27 000 years, past any date range.
_MOST_EDGE_DIGITS = 7
#: The most to-one relationships a ``within`` entry follows, the bound the planner shares.
MOST_HOPS = 32
_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")


@dataclass(frozen=True)
class Histogram:
    """A histogram over whole days, as EXPR-SPEC-2 section 6 writes a ``distribution``.

    ``bins`` are the edges, strictly ascending whole days as decimal strings, and ``bin_weights``
    gives one weight to each interval between two edges. ``closed`` is ``lower`` or ``upper`` when
    declared, and ``None`` for the default (``lower``). ``open`` is a duration's optional weight
    for the rows whose lifetime does not end. All are kept as written.
    """

    bins: Tuple[str, ...]
    bin_weights: Tuple[str, ...]
    closed: Optional[str] = None
    open: Optional[str] = None

    @property
    def closed_edge(self) -> str:
        """The edge of a bin that belongs to it: ``lower`` unless the histogram says ``upper``."""
        return self.closed if self.closed is not None else "lower"

    def as_member(self) -> Dict[str, Any]:
        """The histogram as the entry's member holds it: each optional key only when declared."""
        member: Dict[str, Any] = {"bins": list(self.bins), "binWeights": list(self.bin_weights)}
        if self.closed is not None:
            member["closed"] = self.closed
        if self.open is not None:
            member["open"] = self.open
        return member


@dataclass(frozen=True)
class ExponentialDeclaration:
    """An exponential dwell as declared (ADR 0025 section 3): a mean in days and an optional open share.

    Both are decimal strings kept as written: ``mean`` above 0, ``open`` from 0 up to below 1.
    """

    mean: str
    open: Optional[str] = None

    def as_member(self) -> Dict[str, Any]:
        """The dwell as the entry's member holds it: ``open`` only when declared."""
        member: Dict[str, Any] = {"mean": self.mean}
        if self.open is not None:
            member["open"] = self.open
        return member


#: A dwell: a duration histogram, or an exponential.
DwellDeclaration = Union[Histogram, ExponentialDeclaration]


@dataclass(frozen=True)
class DwellFamilyDeclaration:
    """A dwell family as declared (ADR 0025 section 3): a dwell chosen by the text of a column's value.

    ``states`` pairs a state's text with its dwell, sorted by text, with distinct texts.
    ``otherwise`` is the dwell of a row whose state is NULL or not listed.
    """

    given: str
    states: Tuple[Tuple[str, DwellDeclaration], ...]
    otherwise: Optional[DwellDeclaration] = None

    def as_member(self) -> Dict[str, Any]:
        """The family as the entry's member holds it: ``otherwise`` only when declared."""
        member: Dict[str, Any] = {
            "given": self.given,
            "states": {text: dwell.as_member() for text, dwell in self.states},
        }
        if self.otherwise is not None:
            member["otherwise"] = self.otherwise.as_member()
        return member


#: What a lifetime's ``duration`` holds: a histogram, an exponential or a dwell family.
Duration = Union[Histogram, ExponentialDeclaration, DwellFamilyDeclaration]


def duration_declares_open(duration: Duration) -> bool:
    """Whether any dwell of *duration* leaves rows with no end (an ``open`` weight or share)."""
    if isinstance(duration, Histogram):
        return duration.open is not None
    if isinstance(duration, ExponentialDeclaration):
        return duration.open is not None and Fraction(duration.open) > 0
    members = [dwell for _, dwell in duration.states]
    if duration.otherwise is not None:
        members.append(duration.otherwise)
    return any(duration_declares_open(member) for member in members)


@dataclass(frozen=True)
class LifetimeEntry:
    """One emit's lifetime: its start, its optional end and how the end is drawn, as written."""

    emit: str
    start: str
    end: Optional[str] = None
    duration: Optional[Duration] = None
    #: ``open`` or ``kept`` when declared; the default is resolved where the contract is known.
    end_past_window: Optional[str] = None
    #: The percent of rows that violate the lifetime, as a decimal string.
    target: Optional[str] = None

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``lifetimes`` member holds it: no ``emit``, no absent key."""
        member: Dict[str, Any] = {"start": self.start}
        if self.end is not None:
            member["end"] = self.end
        if self.duration is not None:
            member["duration"] = self.duration.as_member()
        if self.end_past_window is not None:
            member["endPastWindow"] = self.end_past_window
        if self.target is not None:
            member["target"] = self.target
        return member


@dataclass(frozen=True)
class WithinEntry:
    """One date (or period) of an emit within the lifetime of the row a relationship reaches."""

    emit: str
    #: The to-one relationships followed in turn, the emit's first; the last target holds the lifetime.
    relationship: Tuple[str, ...]
    #: The DATE or TIMESTAMP column that is set; ``None`` for a period.
    date: Optional[str] = None
    #: ``True`` for ``lifetime: true``, the emit's own lifetime.
    lifetime: bool = False
    offset: Optional[Histogram] = None
    target: Optional[str] = None
    #: ``True`` for ``exposure: true``: the number of the parent's children follows its exposure
    #: (ADR 0025 section 4). Only a single relationship carries it.
    exposure: bool = False

    @property
    def column(self) -> str:
        """What the entry sets as a rule line names it: the column, or ``lifetime``."""
        return self.date if self.date is not None else "lifetime"

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``within`` member holds it: no ``emit``, no absent key."""
        member: Dict[str, Any] = {"relationship": ".".join(self.relationship)}
        if self.date is not None:
            member["date"] = self.date
        if self.lifetime:
            member["lifetime"] = True
        if self.offset is not None:
            member["offset"] = self.offset.as_member()
        if self.target is not None:
            member["target"] = self.target
        if self.exposure:
            # Only an entry that declares it lists it, so an entry without keeps its bound bytes.
            member["exposure"] = True
        return member


@dataclass(frozen=True)
class RatesEntry:
    """The seasonal rates of one date column of an emit (ADR 0025 section 2), the weights as written.

    ``month`` has 12 weights (January first), ``day_of_week`` 7 (Monday first) and ``hour_of_week``
    168 (Monday 00:00 to 00:59 UTC first), each a decimal string; a factor that is not given is
    ``None`` and counts as 1 everywhere. At least one factor is given.
    """

    emit: str
    column: str
    month: Optional[Tuple[str, ...]] = None
    day_of_week: Optional[Tuple[str, ...]] = None
    hour_of_week: Optional[Tuple[str, ...]] = None

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``rates`` member holds it: no ``emit``, each factor only when declared."""
        member: Dict[str, Any] = {"column": self.column}
        if self.month is not None:
            member["month"] = list(self.month)
        if self.day_of_week is not None:
            member["dayOfWeek"] = list(self.day_of_week)
        if self.hour_of_week is not None:
            member["hourOfWeek"] = list(self.hour_of_week)
        return member


def _name(entry: Mapping[str, Any], key: str, at: str, code: ErrorCode) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FluxError(code, f"{at}.{key} is a non-empty string")
    return nfc(value)


def _no_unknown_key(
    entry: Mapping[str, Any], allowed: Sequence[str], at: str, code: ErrorCode
) -> None:
    unknown = sorted(str(key) for key in entry if str(key) not in allowed)
    if unknown:
        raise FluxError(code, f"{at} declares {unknown}; the entry carries {list(allowed)}")


def _emit(entry: Mapping[str, Any], emits: FrozenSet[str], at: str, code: ErrorCode) -> str:
    emit = _name(entry, "emit", at, code)
    if emit not in emits:
        raise FluxError(
            code,
            f"{at}.emit names {emit!r}, which this Simulation does not emit "
            f"(it emits {sorted(emits)})",
        )
    return emit


def _too_long(text: str) -> bool:
    """Whether a decimal string holds more than :data:`MOST_DIGITS` digits."""
    return len(text) - text.count(".") > MOST_DIGITS


def _target(entry: Mapping[str, Any], at: str, code: ErrorCode) -> Optional[str]:
    """A ``target``: a percent as a decimal string in [0, 100), or ``None`` when absent."""
    if "target" not in entry:
        return None
    text = entry["target"]
    if not isinstance(text, str) or _DECIMAL.fullmatch(text) is None or _too_long(text):
        raise FluxError(
            code,
            f"{at}.target is a percent as a decimal string from 0 up to 100 (not 100), as '2.5', "
            f"of at most {MOST_DIGITS} digits",
        )
    if Fraction(text) >= 100:
        raise FluxError(
            code,
            f"{at}.target is a percent as a decimal string from 0 up to 100 (not 100), as '2.5'",
        )
    return text


def _weight(raw: Any, at: str, code: ErrorCode) -> Fraction:
    if not isinstance(raw, str) or _DECIMAL.fullmatch(raw) is None or _too_long(raw):
        raise FluxError(
            code,
            f"{at} is a weight written as a decimal string of at least zero, "
            f"of at most {MOST_DIGITS} digits",
        )
    return Fraction(raw)


def _check_edges(bins: Any, at: str, code: ErrorCode) -> None:
    """Refuse (*code*) ``bins`` that are not a list of two or more ascending whole days, as decimal strings."""
    if not isinstance(bins, Sequence) or isinstance(bins, (str, bytes)) or len(bins) < 2:
        raise FluxError(code, f"{at}.bins is a list of at least two whole days, as '0', '30'")
    if len(bins) > _MOST_BINS:
        raise FluxError(code, f"{at}.bins holds more than {_MOST_BINS} edges")
    if any(not isinstance(item, str) or _WHOLE.fullmatch(item) is None for item in bins):
        raise FluxError(code, f"{at}.bins are whole days written as decimal strings of integers")
    if any(len(item.lstrip("-")) > _MOST_EDGE_DIGITS for item in bins):
        raise FluxError(
            code, f"{at}.bins are whole days of at most {_MOST_EDGE_DIGITS} digits each"
        )
    days = [int(item) for item in bins]
    if any(later <= earlier for earlier, later in zip(days, days[1:], strict=False)):
        raise FluxError(code, f"{at}.bins ascend: each edge is above the one before it")


def _bin_weight_total(weights: Any, edges: int, at: str, code: ErrorCode) -> Fraction:
    """The sum of ``binWeights``, one per bin of *edges* edges, or a refusal (*code*)."""
    if not isinstance(weights, Sequence) or isinstance(weights, (str, bytes)):
        raise FluxError(code, f"{at}.binWeights is a list of weights, one per bin")
    if len(weights) != edges - 1:
        raise FluxError(
            code,
            f"{at}.binWeights holds {len(weights)} weights for {edges - 1} bins "
            f"(a list of {edges} edges)",
        )
    return sum(
        (_weight(item, f"{at}.binWeights[{n}]", code) for n, item in enumerate(weights)),
        Fraction(0),
    )


def _histogram(raw: Any, at: str, keys: Sequence[str], code: ErrorCode) -> Histogram:
    """A ``duration`` or an ``offset``: ``bins``, ``binWeights``, ``closed`` and, for a duration, ``open``."""
    if not isinstance(raw, Mapping):
        raise FluxError(code, f"{at} is a mapping with {list(keys)}")
    _no_unknown_key(raw, keys, at, code)
    bins: Any = raw.get("bins")
    weights: Any = raw.get("binWeights")
    _check_edges(bins, at, code)
    total = _bin_weight_total(weights, len(bins), at, code)
    closed = raw.get("closed")
    if "closed" in raw and closed not in _CLOSED_VALUES:
        raise FluxError(code, f"{at}.closed is 'lower' or 'upper'")
    opened = raw.get("open")
    if "open" in raw:
        total += _weight(opened, f"{at}.open", code)
    if total == 0:
        raise FluxError(code, f"{at} holds no weight: every binWeights entry is zero")
    return Histogram(
        bins=tuple(bins),
        bin_weights=tuple(weights),
        closed=closed if "closed" in raw else None,
        open=opened if "open" in raw else None,
    )


def _non_negative_bins(histogram: Histogram, at: str, code: ErrorCode) -> None:
    if int(histogram.bins[0]) < 0:
        # An end drawn before its start would break the lifetime on a row the generator made;
        # a share of such rows is what a target asks for (ADR 0024 section 7).
        raise FluxError(code, f"{at}.bins start at '0' or later: a duration is not negative")


def _exponential(raw: Mapping[str, Any], at: str, code: ErrorCode) -> ExponentialDeclaration:
    _no_unknown_key(raw, _EXPONENTIAL_KEYS, at, code)
    mean = raw.get("mean")
    if (
        not isinstance(mean, str)
        or _DECIMAL.fullmatch(mean) is None
        or _too_long(mean)
        or Fraction(mean) == 0
    ):
        raise FluxError(
            code,
            f"{at}.mean is the mean duration in days as a decimal string above 0, as '30', "
            f"of at most {MOST_DIGITS} digits",
        )
    opened = raw.get("open")
    if "open" in raw and (
        not isinstance(opened, str)
        or _DECIMAL.fullmatch(opened) is None
        or _too_long(opened)
        or Fraction(opened) >= 1
    ):
        raise FluxError(
            code,
            f"{at}.open is the share of rows with no end as a decimal string from 0 up to 1 "
            f"(not 1), as '0.1', of at most {MOST_DIGITS} digits",
        )
    return ExponentialDeclaration(mean=mean, open=opened if "open" in raw else None)


def _dwell(raw: Any, at: str, code: ErrorCode) -> DwellDeclaration:
    """A histogram or an exponential, told apart by its keys; anything mixed or unknown is refused."""
    if not isinstance(raw, Mapping):
        raise FluxError(code, f"{at} is a mapping: a histogram or an exponential with a mean")
    keys = {str(key) for key in raw}
    if keys & set(_FAMILY_KEYS):
        raise FluxError(code, f"{at} is a histogram or an exponential, not a dwell family")
    if "mean" in keys:
        if keys & set(_HISTOGRAM_FORM_KEYS):
            raise FluxError(
                code,
                f"{at} mixes an exponential's mean with a histogram's keys: it is one or the other",
            )
        return _exponential(raw, at, code)
    histogram = _histogram(raw, at, _DURATION_KEYS, code)
    _non_negative_bins(histogram, at, code)
    return histogram


def _states(states_raw: Any, at: str, code: ErrorCode) -> List[Tuple[str, DwellDeclaration]]:
    """The ``states`` of a dwell family as ``(text, dwell)`` pairs, each text in NFC and none twice."""
    if not isinstance(states_raw, Mapping):
        raise FluxError(code, f"{at}.states is a mapping from a state's text to a dwell")
    if len(states_raw) > MOST_STATES:
        raise FluxError(code, f"{at}.states holds more than {MOST_STATES} states")
    states: List[Tuple[str, DwellDeclaration]] = []
    for text, member in states_raw.items():
        if not isinstance(text, str) or not text:
            raise FluxError(code, f"{at}.states has a key that is not a non-empty string")
        states.append((nfc(text), _dwell(member, f"{at}.states[{text!r}]", code)))
    texts = [text for text, _ in states]
    if len(set(texts)) != len(texts):
        raise FluxError(code, f"{at}.states names one state twice (after NFC)")
    return states


def _family(
    raw: Mapping[str, Any], at: str, code: ErrorCode, *, start: str, end: str
) -> DwellFamilyDeclaration:
    _no_unknown_key(raw, _FAMILY_KEYS, at, code)
    given = _name(raw, "given", at, code)
    if given in (start, end):
        raise FluxError(
            code,
            f"{at}.given names {given!r}, the lifetime's {'start' if given == start else 'end'}: "
            "the state is another column",
        )
    states = _states(raw["states"], at, code) if "states" in raw else []
    otherwise = _dwell(raw["otherwise"], f"{at}.otherwise", code) if "otherwise" in raw else None
    if not states and otherwise is None:
        raise FluxError(code, f"{at} lists no states and no otherwise: it holds no dwell")
    return DwellFamilyDeclaration(
        given=given, states=tuple(sorted(states, key=lambda pair: pair[0])), otherwise=otherwise
    )


def _duration(raw: Any, at: str, code: ErrorCode, *, start: str, end: str) -> Duration:
    """A ``duration``: a histogram, an exponential or a dwell family, told apart by its keys."""
    if not isinstance(raw, Mapping):
        raise FluxError(
            code, f"{at} is a mapping: a histogram, an exponential with a mean, or a dwell family"
        )
    keys = {str(key) for key in raw}
    forms = [
        name
        for name, found in (
            ("a histogram", keys & set(_HISTOGRAM_FORM_KEYS)),
            ("an exponential", keys & {"mean"}),
            ("a dwell family", keys & set(_FAMILY_KEYS)),
        )
        if found
    ]
    if len(forms) > 1:
        raise FluxError(code, f"{at} mixes the keys of {' and '.join(forms)}: it is one of them")
    if not forms:
        raise FluxError(
            code,
            f"{at} is a histogram (bins, binWeights), an exponential (mean) or a dwell family "
            "(given, states)",
        )
    if "a dwell family" in forms:
        return _family(raw, at, code, start=start, end=end)
    return _dwell(raw, at, code)


def _block(block: Any, where: str, shape: str, code: ErrorCode) -> Sequence[Any]:
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {shape} entries")
    if len(block) > _MOST_ENTRIES:
        raise FluxError(code, f"{where} holds more than {_MOST_ENTRIES} entries")
    return block


def _lifetime(
    entry: Mapping[str, Any], emits: FrozenSet[str], at: str, code: ErrorCode
) -> LifetimeEntry:
    _no_unknown_key(entry, _LIFETIME_KEYS, at, code)
    emit = _emit(entry, emits, at, code)
    start = _name(entry, "start", at, code)
    end = _name(entry, "end", at, code) if "end" in entry else None
    if end == start:
        raise FluxError(code, f"{at} names {start!r} as its start and as its end")
    for key in ("duration", "endPastWindow", "target"):
        if key in entry and end is None:
            raise FluxError(code, f"{at}.{key} needs an end: the entry names none")
    duration = (
        _duration(entry["duration"], f"{at}.duration", code, start=start, end=end)
        if "duration" in entry and end is not None
        else None
    )
    past = entry.get("endPastWindow")
    if "endPastWindow" in entry and past not in END_PAST_WINDOW_VALUES:
        raise FluxError(code, f"{at}.endPastWindow is 'open' or 'kept'")
    return LifetimeEntry(
        emit=emit,
        start=start,
        end=end,
        duration=duration,
        end_past_window=past if "endPastWindow" in entry else None,
        target=_target(entry, at, code),
    )


def read_lifetimes_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[LifetimeEntry, ...]:
    """Read a ``lifetimes`` block, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused: a block that is not a list, more
    than 4 096 entries, an entry that is not a mapping or carries a key other than ``emit``,
    ``start``, ``end``, ``duration``, ``endPastWindow`` and ``target``, an emit the Simulation does
    not emit or declares a lifetime for twice, a ``start`` or ``end`` that is not a non-empty
    string or an ``end`` that is the ``start``, a ``duration``, ``endPastWindow`` or ``target``
    on an entry with no ``end``, a histogram that is not a mapping of ``bins`` (strictly ascending
    whole days from ``0`` up, at least two, at most 1 024), one ``binWeights`` per bin (decimals of
    at least zero, not all zero, the ``open`` weight counted), ``closed`` (``lower`` or ``upper``)
    and ``open``, an ``endPastWindow`` that is not ``open`` or ``kept``, and a ``target`` that is
    not a decimal string from 0 up to 100. Whether the columns exist and are dates is checked where the
    contract is known.

    A ``duration`` is one of three forms, told apart by its keys: a histogram as above; an
    exponential ``{mean, open}`` (``mean`` a decimal string above 0, ``open`` an optional decimal
    string from 0 up to below 1); or a dwell family ``{given, states, otherwise}`` (``given`` a
    column other than the start and the end, ``states`` a mapping of at most 1 024 non-empty texts
    to a histogram or an exponential, ``otherwise`` an optional one, at least one of the two). A
    ``duration`` that mixes the keys of two forms, or carries a key of none, is refused.
    """
    found: Dict[str, LifetimeEntry] = {}
    for index, entry in enumerate(_block(block, where, "{emit, start, end}", code)):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            raise FluxError(code, f"{at} is a mapping with emit, start and optionally end")
        read = _lifetime(entry, emits, at, code)
        if read.emit in found:
            raise FluxError(
                code, f"{at} declares a second lifetime for {read.emit!r}; one per emit"
            )
        found[read.emit] = read
    return tuple(found.values())


def _relationship(entry: Mapping[str, Any], at: str, code: ErrorCode) -> Tuple[str, ...]:
    text = _name(entry, "relationship", at, code)
    names = tuple(text.split("."))
    if any(not name.strip() for name in names):
        raise FluxError(code, f"{at}.relationship {text!r} holds an empty name between its dots")
    if len(names) > MOST_HOPS:
        raise FluxError(
            code, f"{at}.relationship follows {len(names)} relationships; at most {MOST_HOPS}"
        )
    return names


def _within(
    entry: Mapping[str, Any], emits: FrozenSet[str], at: str, code: ErrorCode
) -> WithinEntry:
    _no_unknown_key(entry, _WITHIN_KEYS, at, code)
    emit = _emit(entry, emits, at, code)
    relationship = _relationship(entry, at, code)
    flagged = "lifetime" in entry
    if flagged and entry["lifetime"] is not True:
        raise FluxError(code, f"{at}.lifetime is true, or absent")
    if flagged == ("date" in entry):
        raise FluxError(
            code, f"{at} names exactly one of date (a column of the emit) and lifetime: true"
        )
    offset = (
        _histogram(entry["offset"], f"{at}.offset", _OFFSET_KEYS, code)
        if "offset" in entry
        else None
    )
    exposed = "exposure" in entry
    if exposed and entry["exposure"] is not True:
        raise FluxError(code, f"{at}.exposure is true, or absent; false states nothing")
    if exposed and len(relationship) != 1:
        raise FluxError(
            code,
            f"{at}.exposure is on {'.'.join(relationship)!r}, a chain of relationships; a row is "
            "counted among the children of the row its own link names, so exposure takes one "
            "relationship",
        )
    return WithinEntry(
        emit=emit,
        relationship=relationship,
        date=_name(entry, "date", at, code) if "date" in entry else None,
        lifetime=flagged,
        offset=offset,
        target=_target(entry, at, code),
        exposure=exposed,
    )


def _on_one_chain(entries: Sequence[Tuple[int, WithinEntry]], where: str, code: ErrorCode) -> None:
    """Refuse (*code*) entries for one column that do not lie on one chain, or carry two offsets."""
    longest = max((entry.relationship for _, entry in entries), key=len)
    for index, entry in entries:
        if longest[: len(entry.relationship)] != entry.relationship:
            raise FluxError(
                code,
                f"{where}[{index}] names {'.'.join(entry.relationship)!r} for {entry.emit}.{entry.column}, "
                f"which is not a prefix of {'.'.join(longest)!r}: the entries of one column lie on one chain",
            )
    offsets = [index for index, entry in entries if entry.offset is not None]
    if len(offsets) > 1:
        raise FluxError(
            code,
            f"{where}[{offsets[1]}] is a second offset for {entries[0][1].emit}.{entries[0][1].column}; "
            "at most one entry of a column carries one",
        )


def read_within_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[WithinEntry, ...]:
    """Read a ``within`` block in the order declared, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused: a block that is not a list, more
    than 4 096 entries, an entry that is not a mapping or carries a key other than ``emit``,
    ``relationship``, ``date``, ``lifetime``, ``offset``, ``target`` and ``exposure``, an emit the Simulation
    does not emit, a ``relationship`` that is not a non-empty dotted name, an entry that names both
    or neither of ``date`` and ``lifetime: true``, a ``lifetime`` that is not ``true``, an
    ``offset`` that is not a histogram (as :func:`read_lifetimes_block` reads one, with no
    ``open``), a ``target`` that is not a decimal string from 0 up to 100, one relationship
    declared twice for a column, entries of one column that do not lie on one chain (each a prefix
    of the longest), and two ``offset`` entries for a column. ``exposure`` is ``true`` or left out
    (``false`` is refused: it states nothing), it takes a single relationship and no chain, and one
    relationship of an emit carries it on at most one entry (ADR 0025 section 4). Whether the columns and the
    relationships are the contract's is checked where the contract is known.
    """
    read: List[WithinEntry] = []
    for index, entry in enumerate(_block(block, where, "{emit, relationship, date}", code)):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            raise FluxError(code, f"{at} is a mapping with emit, relationship and date or lifetime")
        read.append(_within(entry, emits, at, code))
    by_column: Dict[Tuple[str, str], List[Tuple[int, WithinEntry]]] = {}
    for index, item in enumerate(read):
        group = by_column.setdefault((item.emit, item.column), [])
        if any(other.relationship == item.relationship for _, other in group):
            raise FluxError(
                code,
                f"{where}[{index}] declares {'.'.join(item.relationship)!r} twice for "
                f"{item.emit}.{item.column}",
            )
        group.append((index, item))
    for group in by_column.values():
        _on_one_chain(group, where, code)
    exposed: Dict[Tuple[str, Tuple[str, ...]], int] = {}
    for index, item in enumerate(read):
        if item.exposure:
            if (item.emit, item.relationship) in exposed:
                raise FluxError(
                    code,
                    f"{where}[{index}] is a second exposure entry for {item.emit}."
                    f"{'.'.join(item.relationship)}; the number of a parent's children follows "
                    "one entry's exposure",
                )
            exposed[(item.emit, item.relationship)] = index
    return tuple(read)


def _factor(
    entry: Mapping[str, Any], key: str, at: str, code: ErrorCode
) -> Optional[Tuple[str, ...]]:
    """A seasonal factor: a list of weights of the size the key has, not all zero, or ``None`` when absent."""
    if key not in entry:
        return None
    raw, size = entry[key], RATES_FACTOR_SIZES[key]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != size:
        raise FluxError(
            code,
            f"{at}.{key} is a list of {size} weights written as decimal strings of at least zero",
        )
    weights = [_weight(item, f"{at}.{key}[{n}]", code) for n, item in enumerate(raw)]
    for n, item in enumerate(raw):
        if len(item) - item.count(".") > RATES_MOST_DIGITS:
            raise FluxError(
                code,
                f"{at}.{key}[{n}] is a weight of more than {RATES_MOST_DIGITS} digits; a rates "
                f"weight is written with at most {RATES_MOST_DIGITS}",
            )
    if not any(weights):
        raise FluxError(code, f"{at}.{key} holds no weight: every entry is zero")
    return tuple(raw)


def _rates(entry: Mapping[str, Any], emits: FrozenSet[str], at: str, code: ErrorCode) -> RatesEntry:
    _no_unknown_key(entry, _RATES_KEYS, at, code)
    emit = _emit(entry, emits, at, code)
    column = _name(entry, "column", at, code)
    month = _factor(entry, "month", at, code)
    week = _factor(entry, "dayOfWeek", at, code)
    hours = _factor(entry, "hourOfWeek", at, code)
    if month is None and week is None and hours is None:
        raise FluxError(
            code, f"{at} gives no factor: name at least one of month, dayOfWeek, hourOfWeek"
        )
    return RatesEntry(emit=emit, column=column, month=month, day_of_week=week, hour_of_week=hours)


def read_rates_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[RatesEntry, ...]:
    """Read a ``rates`` block in the order declared, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused: a block that is not a list, more
    than 4 096 entries, an entry that is not a mapping or carries a key other than ``emit``,
    ``column``, ``month``, ``dayOfWeek`` and ``hourOfWeek``, an emit the Simulation does not emit, a
    ``column`` that is not a non-empty string, a column declared twice for an emit, an entry that
    gives no factor, and a factor that is not a list of the right size (``month`` 12, ``dayOfWeek``
    7, ``hourOfWeek`` 168) of decimal strings of at least zero, of at most 30 digits each, not all
    zero. Whether the column is a date a ``within`` entry or a duration sets, and whether
    ``hourOfWeek`` sits on a TIMESTAMP, is checked where the contract is known.
    """
    read: List[RatesEntry] = []
    seen: set[Tuple[str, str]] = set()
    for index, entry in enumerate(_block(block, where, "{emit, column, month}", code)):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            raise FluxError(code, f"{at} is a mapping with emit, column and at least one factor")
        item = _rates(entry, emits, at, code)
        if (item.emit, item.column) in seen:
            raise FluxError(
                code, f"{at} declares {item.emit}.{item.column} twice; one entry per column"
            )
        seen.add((item.emit, item.column))
        read.append(item)
    return tuple(read)


def lifetime_bound_into_h(entries: Sequence[LifetimeEntry], emit: str) -> Optional[Dict[str, Any]]:
    """The ``lifetimes`` member of *emit*'s entry for ``H``, or ``None`` when it declares none.

    The emit's one entry as :meth:`LifetimeEntry.as_member` writes it, so the entry gains no key
    when the emit has no lifetime.
    """
    wanted = nfc(emit)
    for entry in entries:
        if entry.emit == wanted:
            return entry.as_member()
    return None


def within_bound_into_h(
    entries: Sequence[WithinEntry], emit: str
) -> Optional[List[Dict[str, Any]]]:
    """The ``within`` member of *emit*'s entry for ``H``, or ``None`` when it declares none.

    The emit's entries as :meth:`WithinEntry.as_member` writes them, in the order declared.
    """
    wanted = nfc(emit)
    member = [entry.as_member() for entry in entries if entry.emit == wanted]
    return member or None


def rates_bound_into_h(entries: Sequence[RatesEntry], emit: str) -> Optional[List[Dict[str, Any]]]:
    """The ``rates`` member of *emit*'s entry for ``H``, or ``None`` when it declares none.

    The emit's entries as :meth:`RatesEntry.as_member` writes them, in the order declared.
    """
    wanted = nfc(emit)
    member = [entry.as_member() for entry in entries if entry.emit == wanted]
    return member or None


def _histogram_of(raw: Any) -> Optional[Histogram]:
    if not isinstance(raw, Mapping):
        return None
    return Histogram(
        bins=tuple(raw["bins"]),
        bin_weights=tuple(raw["binWeights"]),
        closed=raw.get("closed"),
        open=raw.get("open"),
    )


def _dwell_of(raw: Any) -> DwellDeclaration:
    if "mean" in raw:
        return ExponentialDeclaration(mean=raw["mean"], open=raw.get("open"))
    return Histogram(
        bins=tuple(raw["bins"]),
        bin_weights=tuple(raw["binWeights"]),
        closed=raw.get("closed"),
        open=raw.get("open"),
    )


def _duration_of(raw: Any) -> Optional[Duration]:
    """The duration a bound member holds, in whichever of its three forms."""
    if not isinstance(raw, Mapping):
        return None
    if "given" in raw:
        other = raw.get("otherwise")
        return DwellFamilyDeclaration(
            given=raw["given"],
            states=tuple(
                sorted(
                    ((text, _dwell_of(member)) for text, member in raw.get("states", {}).items()),
                    key=lambda pair: pair[0],
                )
            ),
            otherwise=None if other is None else _dwell_of(other),
        )
    return _dwell_of(raw)


def lifetime_entries_of(
    extension: Optional[Mapping[str, Any]], emit: str
) -> Tuple[LifetimeEntry, ...]:
    """The lifetime an emit's bound extension holds (the member :func:`lifetime_bound_into_h` made)."""
    raw = extension.get(LIFETIMES_KEY) if extension is not None else None
    if not isinstance(raw, Mapping):
        return ()
    return (
        LifetimeEntry(
            emit=nfc(emit),
            start=str(raw["start"]),
            end=raw.get("end"),
            duration=_duration_of(raw.get("duration")),
            end_past_window=raw.get("endPastWindow"),
            target=raw.get("target"),
        ),
    )


def within_entries_of(extension: Optional[Mapping[str, Any]], emit: str) -> Tuple[WithinEntry, ...]:
    """The ``within`` entries an emit's bound extension holds, in the order declared."""
    raw = extension.get(WITHIN_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(
        WithinEntry(
            emit=nfc(emit),
            relationship=tuple(str(item["relationship"]).split(".")),
            date=item.get("date"),
            lifetime=item.get("lifetime") is True,
            offset=_histogram_of(item.get("offset")),
            target=item.get("target"),
            exposure=item.get("exposure") is True,
        )
        for item in raw
        if isinstance(item, Mapping)
    )


def rates_entries_of(extension: Optional[Mapping[str, Any]], emit: str) -> Tuple[RatesEntry, ...]:
    """The ``rates`` entries an emit's bound extension holds, in the order declared."""
    raw = extension.get(RATES_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()

    def factor(item: Mapping[str, Any], key: str) -> Optional[Tuple[str, ...]]:
        found = item.get(key)
        return None if found is None else tuple(str(weight) for weight in found)

    return tuple(
        RatesEntry(
            emit=nfc(emit),
            column=str(item["column"]),
            month=factor(item, "month"),
            day_of_week=factor(item, "dayOfWeek"),
            hour_of_week=factor(item, "hourOfWeek"),
        )
        for item in raw
        if isinstance(item, Mapping)
    )
