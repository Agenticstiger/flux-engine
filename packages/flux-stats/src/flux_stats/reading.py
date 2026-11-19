# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Reading records, the one way the fidelity check and a profiler read them.

Two sides form the same statistic only if they read a record the same way:
which cell a column names, what a null does, how a number, an instant or a
category becomes the exact value a statistic is formed from, how rows fall
into groups, and every refusal a record can meet. This module is that reading.
flux-sim-engine's fidelity check reads its records with it (SEAM-FIDELITY-SPEC
§4, §5), and so does a profiler, which runs where the data lives and never
with the engine. What is formed from what it reads is one registered definition
per statistic (:mod:`flux_stats.registry`).

Nothing here keeps a record. No function returns one, and a diagnostic names a
column, a row index and a value class, never a value (KERNEL-SPEC §9).

Each function asks a column only for what it reads. Reading one cell or one
category needs a name (:class:`NamedColumn`), reading a range column's value a
name and a domain (:class:`ValueColumn`), and the record-set readers the five
attributes of :class:`Column`. The engine passes the declarations it parsed
from a contract's fidelity block; a profiler, which declares no range, can pass
objects that carry only what it has.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, TypeVar

from flux_stats.definitions import level_order
from flux_stats.errors import FluxError, register
from flux_stats.fixedpoint import FLX_5005, Grid, Interval
from flux_stats.spelling import MOST_DIGITS

__all__ = [
    "FLX_5022",
    "FLX_5031",
    "MS_PER_DAY",
    "UNGROUPED",
    "Column",
    "NamedColumn",
    "NullPolicy",
    "Row",
    "ValueColumn",
    "ValueDomain",
    "category",
    "cell",
    "digits_written_out",
    "encode_value",
    "exact",
    "group_keys",
    "level_cells",
    "nfc",
    "parse_date_days",
    "parse_rfc3339_ms",
    "read_column",
    "read_exact",
    "read_pairs",
]

FLX_5022 = register(
    "FLX-5022",
    "virtual clock or scheduler input outside its declared domain",
    "The virtual clock carries integer milliseconds from a declared RFC 3339 start in "
    "UTC (KERNEL-SPEC §7). A timestamp without an offset, with sub-millisecond "
    "precision the clock cannot represent, a tick of zero or fewer milliseconds, time "
    "bounds that end before they start, or an item scheduled outside those bounds are "
    "all refused: a run whose clock is not the declared clock cannot be replayed "
    "against the plan that declared it.",
)

FLX_5031 = register(
    "FLX-5031",
    "a column named by the fidelity block is not a member of the expose",
    "SEAM-FIDELITY-SPEC §2: every column and every groupBy is a member of "
    "the referenced expose schema. A column the expose does not declare "
    "has no type, no position in the emitted row, and no committed "
    "counterpart, so the block is refused offline rather than failing at "
    "check time.",
)


# ---------------------------------------------------------------------------
# Closed sets
# ---------------------------------------------------------------------------


class NullPolicy(str, Enum):
    """What a column's declaration says about nulls (SEAM-FIDELITY-SPEC §2).

    ``EXCLUDE`` drops the record from every property over that column;
    ``FAIL`` refuses the check. There is no third option that would let a
    null be counted as a value.
    """

    EXCLUDE = "exclude"
    FAIL = "fail"


class ValueDomain(str, Enum):
    """How a ``RANGE`` column's values are read before they are encoded.

    ``NUMBER`` values are exact decimals. ``TIMESTAMP`` values are RFC
    3339 strings converted to integer milliseconds by KERNEL-SPEC §7's
    parser (:func:`parse_rfc3339_ms`), which is the only spelling of an
    instant the engine has. ``DATE`` values are ``YYYY-MM-DD`` calendar
    dates (:func:`parse_date_days`), read as whole days since 1970-01-01, so
    a day is the unit of the column's range, bins and statistics. A column is
    ``DATE`` when its declared type is DATE; a profile reads it as a
    timestamp in milliseconds (:data:`MS_PER_DAY`).
    """

    NUMBER = "number"
    TIMESTAMP = "timestamp"
    DATE = "date"


class NamedColumn(Protocol):
    """A column as reading one cell or one category needs it: its name."""

    @property
    def name(self) -> str:
        """The column's name, the key its cell has in a row."""


class ValueColumn(NamedColumn, Protocol):
    """A range column as reading one of its values needs it: a name and a domain."""

    @property
    def domain(self) -> ValueDomain:
        """How a range column's values are read."""


class Column(ValueColumn, Protocol):
    """What the record-set readers read of a column's declaration (SEAM-FIDELITY-SPEC §2).

    flux-sim-engine's ``ColumnDeclaration`` has this shape. Only a range
    column is asked for :attr:`interval`, and only a categorical one for
    :attr:`support_values`.
    """

    @property
    def null_policy(self) -> NullPolicy:
        """What a null in this column does."""

    @property
    def interval(self) -> Interval:
        """A range column's declared closed range."""

    @property
    def support_values(self) -> Tuple[str, ...]:
        """A categorical column's declared levels, as NFC strings."""


#: One record as it reaches a statistic: a mapping from column name to a
#: value that is a string, an int, a ``Decimal`` or ``None``. Never a
#: ``float`` -- KERNEL-SPEC §2.1 keeps binary floating point out of every
#: hashed path, and both the committed slice reader and the emit stream
#: hand over exact values.
Row = Mapping[str, Any]

#: The ungrouped group's key. A string rather than ``None`` keeps the
#: canonical document's mapping keys uniformly strings. It is not reserved:
#: the empty string is also a category a grouped property can hold, as a row
#: value or as a declared level, so whether a property is grouped is read
#: from its declaration (``group_by``) and never from this key.
UNGROUPED = ""


# ---------------------------------------------------------------------------
# Spelling a string and an instant (KERNEL-SPEC §2.1, §7)
# ---------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MS = timedelta(milliseconds=1)

#: RFC 3339 ``date-time``: a full date, a ``T`` separator, a full time, an optional
#: fractional part, and a mandatory offset (``Z`` or ``±HH:MM``).
#:
#: The parser diverges from the standard library on purpose: it never calls
#: ``datetime.fromisoformat``, whose accepted forms differ between the Pythons this
#: package supports (3.10 refuses ``Z`` and a fraction of other than 3 or 6 digits,
#: and 3.14 accepts hour 24). The pattern's groups go to the ``datetime`` constructor
#: instead, so Python 3.10 to 3.14 give one result. On 3.11 to 3.13, where the engine
#: is tested, it is the result ``fromisoformat`` gave:
#: ``tests/test_reading.py::test_the_parser_reads_every_input_as_the_fromisoformat_reading_did``
#: compares the two readings on 20,051 inputs on every run, and a one-off check
#: compared 198,219 (ADR 0009, amendment of 2026-11-17).
_RFC3339 = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"[Tt]"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?"
    r"(?P<offset>[Zz]|[+-]\d{2}:\d{2})$"
)


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()

#: Milliseconds in a day: a profile reads a DATE column as a timestamp, so a
#: day of the column's own unit is this many of the profile's.
MS_PER_DAY = 86_400_000


def parse_date_days(text: str) -> int:
    """Read a ``YYYY-MM-DD`` calendar date as whole days since 1970-01-01.

    Refuses (``FLX-5022``) anything else: a date-time, a date with another
    separator, digits of another script, a trailing newline, and a date that
    does not exist (``2026-02-30``, year ``0000``). It never calls
    ``date.fromisoformat``, whose accepted forms differ between the Pythons
    this package supports.
    """
    if not isinstance(text, str):
        raise FluxError(FLX_5022, f"date must be a string, got {type(text).__name__}")
    if _DATE.fullmatch(text) is None:
        raise FluxError(FLX_5022, "date is not a YYYY-MM-DD calendar date")
    year, month, day = (int(part) for part in text.split("-"))
    try:
        return date(year, month, day).toordinal() - _EPOCH_ORDINAL
    except ValueError:
        raise FluxError(FLX_5022, "date is not a calendar date") from None


def nfc(text: str) -> str:
    """Return *text* in Unicode Normalization Form C (KERNEL-SPEC §2.1).

    Applied to every string **and every key**: the composed and decomposed
    spellings of the same word are the same word, and a digest that
    disagreed about that would be a digest of the input's encoding rather
    than of its content.
    """
    return unicodedata.normalize("NFC", text)


def parse_rfc3339_ms(text: str) -> int:
    """Parse an RFC 3339 timestamp to integer milliseconds since the Unix epoch, UTC.

    Refuses (``FLX-5022``) a value with no offset, and a value carrying sub-millisecond
    precision: §7's clock is integer milliseconds, so a microsecond in the declaration has
    no representation, and silently truncating it would make the run's time bounds
    different from the declared ones by an amount nothing records. Refuses as well a
    date or a time that does not exist, and an instant that falls outside the years 1
    to 9999 once it is moved to UTC.

    The cases it decides, each pinned in ``tests/test_reading.py``:

    * ``T`` or ``t`` separates the date from the time, and ``Z`` or ``z`` is UTC.
    * ``-00:00`` is UTC, as ``+00:00`` is. An offset's minutes add to its hours, so
      ``+00:60`` is ``+01:00``; an offset of 24 hours or more (``+24:00``,
      ``-23:60``) is refused.
    * A fraction of any length reads as milliseconds, provided every digit past the
      third is a zero: ``.1230000`` is 123 ms, and ``.1234`` is refused.
    * Hour 24, minute 60 and second 60 are refused; the clock has no leap second.
    * Year 0000 is refused, and so is an instant outside the years 1 to 9999 once
      it is moved to UTC (``0001-01-01T00:00:00+00:01``).
    * Digits of another script, and a trailing newline, are refused.
    """
    if not isinstance(text, str):
        raise FluxError(FLX_5022, f"timestamp must be a string, got {type(text).__name__}")
    match = _RFC3339.match(text)
    if match is None:
        raise FluxError(FLX_5022, "timestamp is not an RFC 3339 date-time with an offset")
    fraction = match.group("fraction")
    digits = "" if fraction is None else fraction[1:]
    if len(digits) > 3 and set(digits[3:]) != {"0"}:
        raise FluxError(
            FLX_5022,
            "timestamp carries sub-millisecond precision, which the virtual clock "
            "does not represent",
        )
    # The pattern's \d takes any script's decimal digits and its $ a trailing newline.
    # Neither is RFC 3339, and the date-time read refused both.
    if not text.isascii() or match.end() != len(text):
        raise FluxError(FLX_5022, "timestamp is not a valid date-time")
    year, month, day = (int(part) for part in match.group("date").split("-"))
    hour, minute, second = (int(part) for part in match.group("time").split(":"))
    offset = match.group("offset")
    shift = timedelta(0)
    if offset not in ("Z", "z"):
        shift = timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
        if offset[0] == "-":
            shift = -shift
    milliseconds = int((digits + "00")[:3])
    # Each refusal drops the error it caught: an offset's error spells the offset,
    # and a refusal names no part of the value (KERNEL-SPEC §9).
    try:
        moment = datetime(
            year, month, day, hour, minute, second, milliseconds * 1000, tzinfo=timezone(shift)
        )
    except ValueError:
        raise FluxError(FLX_5022, "timestamp is not a valid date-time") from None
    try:
        utc = moment.astimezone(timezone.utc)
    except OverflowError:
        raise FluxError(
            FLX_5022, "timestamp falls outside the years 1 to 9999 once moved to UTC"
        ) from None
    return (utc - _EPOCH) // _ONE_MS


# ---------------------------------------------------------------------------
# Reading one value
# ---------------------------------------------------------------------------


def digits_written_out(number: Decimal) -> int:
    """How many digits the finite *number* needs, written out without an exponent.

    A leading ``0.`` does not count, and a zero counts the digits its scale
    spells: the canonical form keeps the scale a number was written with
    (:func:`flux_stats.canonical.render_decimal`), so ``0E-300000000`` is
    spelled in three hundred million digits, however little its value is. A
    zero with a positive exponent is the digit ``0``. This is the count by
    which SEAM-FIDELITY-SPEC §2 bounds a declared number, at most
    :data:`~flux_stats.spelling.MOST_DIGITS`. It is read from the decimal's own
    digits and exponent, so no value is formed to count them. The engine's
    reader of a fidelity block, the parameter register that counts its
    numbers, the exact YAML read, the ``--sample`` argument and :func:`exact`,
    the reader of a record, apply this one count. A profile document's plain decimal counts that
    leading ``0`` too (the pattern in ``profile-1.schema.json``, which
    :mod:`flux_stats.spelling` applies), so a value right at the bound can be
    read and still not be published.
    """
    digits = len(number.as_tuple().digits)
    exponent = number.adjusted() - digits + 1
    if not number:
        return max(1, -exponent)  # the scale of a zero, which is spelled; one digit if it has none
    if exponent >= 0:
        return digits + exponent
    return max(digits, -exponent)


#: The smallest ``int`` that needs more than :data:`~flux_stats.spelling.MOST_DIGITS`
#: digits. Comparing against it costs what reading the ``int`` costs; counting its
#: digits through a string would cost far more.
_SMALLEST_TOO_LONG_INT = 10**MOST_DIGITS


def exact(value: Any, subject: str) -> Fraction:
    """Read a number as the exact rational it spells.

    An ``int``, a finite ``Decimal``, or a string that spells a finite
    decimal. Refuses (``FLX-5005``) a ``bool``, a ``float``, a non-finite
    ``Decimal``, a string that spells no number, and any other class.
    Refuses as well a number that needs more than
    :data:`~flux_stats.spelling.MOST_DIGITS` digits written out
    (:func:`digits_written_out`), before its value is formed: ``"1E+99999999"``
    is eleven characters and names a number of a hundred million digits,
    which takes minutes and gigabytes to form. *subject* names the cell in the
    diagnostic, which gives the value's class and never the value, nor its
    length.
    """
    if isinstance(value, bool):
        raise FluxError(FLX_5005, f"{subject}: value class 'bool'")
    if isinstance(value, float):
        raise FluxError(FLX_5005, f"{subject}: value class 'float'")
    if isinstance(value, int):
        if abs(value) >= _SMALLEST_TOO_LONG_INT:
            raise FluxError(FLX_5005, f"{subject}: value class 'over-long number'")
        return Fraction(value)
    if isinstance(value, Decimal):
        return _bounded(value, subject)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise FluxError(FLX_5005, f"{subject}: value class 'non-numeric string'") from None
        return _bounded(parsed, subject)
    raise FluxError(FLX_5005, f"{subject}: value class {type(value).__name__!r}")


def _bounded(number: Decimal, subject: str) -> Fraction:
    """*number* as an exact rational, refused when it is not finite or too long to write out."""
    if not number.is_finite():
        raise FluxError(FLX_5005, f"{subject}: value class 'non-finite Decimal'")
    if digits_written_out(number) > MOST_DIGITS:
        raise FluxError(FLX_5005, f"{subject}: value class 'over-long number'")
    return Fraction(number)


def read_exact(value: Any, column: ValueColumn, *, row_index: int) -> Fraction:
    """Read one non-null value of a ``RANGE`` column as an exact rational.

    A ``NUMBER`` value through :func:`exact`; a ``TIMESTAMP`` value, which
    must be a string (``FLX-5005`` otherwise), as its milliseconds since
    the epoch (:func:`parse_rfc3339_ms`); a ``DATE`` value, a ``YYYY-MM-DD``
    string (or a number of days), as its days since the epoch. This is the value
    :func:`encode_value` checks against the declared range and puts on the
    grid, so a statistic formed over exact values reads the same number.
    """
    subject = f"column {column.name!r} row {row_index}"
    if column.domain is ValueDomain.TIMESTAMP:
        if not isinstance(value, str):
            raise FluxError(FLX_5005, f"{subject}: value class {type(value).__name__!r}")
        return Fraction(parse_rfc3339_ms(value))
    if column.domain is ValueDomain.DATE:
        # A calendar date is read as days. A number is days already, as it was
        # when a DATE column was read as a number, and a string that is no
        # date is a number or is refused by exact().
        if isinstance(value, str) and _DATE.fullmatch(value) is not None:
            try:
                return Fraction(parse_date_days(value))
            except FluxError:
                raise FluxError(FLX_5005, f"{subject}: value is not a calendar date") from None
        if isinstance(value, date) and not isinstance(value, datetime):
            return Fraction(value.toordinal() - _EPOCH_ORDINAL)
    return exact(value, subject)


def encode_value(
    value: Any,
    column: Column,
    grid: Grid,
    *,
    row_index: int,
) -> int:
    """Encode one non-null value of a ``RANGE`` column onto the grid.

    Refuses (``FLX-5005``) a value outside the declared range -- §5's
    fail-closed condition "a value outside its range". The diagnostic
    names the column, the row index and the value's class, never the
    value itself.
    """
    subject = f"column {column.name!r} row {row_index}"
    value_exact = read_exact(value, column, row_index=row_index)
    interval = column.interval
    if not interval.contains(value_exact):
        raise FluxError(FLX_5005, f"{subject}: value outside the declared range")
    return grid.quantise(value_exact, subject=subject)


def category(value: Any, column: NamedColumn, *, row_index: int) -> str:
    """Read one non-null value of a categorical column: the NFC string it holds.

    Refuses (``FLX-5005``) a value that is not a string.
    """
    subject = f"column {column.name!r} row {row_index}"
    if not isinstance(value, str):
        raise FluxError(FLX_5005, f"{subject}: value class {type(value).__name__!r}")
    return nfc(value)


def cell(row: Row, column: NamedColumn, *, row_index: int) -> Any:
    """Return the raw cell for *column*, refusing an absent column.

    §5's fail-closed list names "a column absent" -- absent from the data,
    not from the declaration, so it is ``FLX-5031`` (a column the check
    needs that the rows do not carry) rather than a sample refusal.
    """
    if column.name not in row:
        raise FluxError(
            FLX_5031,
            f"column {column.name!r} is absent from row {row_index}",
        )
    return row[column.name]


def level_cells(column: Column) -> Tuple[str, ...]:
    """The cells a level histogram counts over *column*, in their one order.

    The column's declared support levels, sorted: NFC-normalised strings
    ordered by code point, the order "the sorted committed support" uses.
    A declaration's levels are distinct NFC strings (the engine resolves
    both support spellings to them where it reads the declaration), so the
    cells are distinct. They come from the declaration and never from the
    data, so the shape of a ``levelCounts`` list is fixed by the contract
    the way a histogram's bin count is, and a level the state never holds
    is a cell of count zero rather than an absent one.
    """
    return level_order(column.support_values)


# ---------------------------------------------------------------------------
# Reading a record set: the null policy and the groups
# ---------------------------------------------------------------------------


def group_keys(
    rows: Sequence[Row],
    column: Optional[Column],
    *,
    declared_only: bool = False,
    first_row: int = 0,
) -> List[Tuple[str, List[int]]]:
    """Partition row indices by the value each row holds in *column*.

    ``None`` is an ungrouped property: one group, :data:`UNGROUPED`, of
    every row. Otherwise a group's key is the row's NFC string
    (:func:`category`); a null under ``fail`` refuses the check
    (``FLX-5005``), and a null under ``exclude`` leaves its row out of
    every group. With *declared_only* a key the column's declared levels do
    not name is refused (``FLX-5005``): a level histogram is a table of
    declared cells on both axes, so its groups, like its levels, must be
    levels the declaration names. Other grouped properties group by
    whatever the rows hold.

    Groups are returned in NFC-sorted key order, never in the order the
    rows happened to arrive: two record sets with the same contents and
    different orderings must produce the same statistic, byte for byte.

    The indices returned are positions in *rows*. A refusal names a row by
    its position plus *first_row*, so a caller reading a slice of a larger
    record set, such as one block of a sample, names the row as the whole
    set would.
    """
    if column is None:
        return [(UNGROUPED, list(range(len(rows))))]
    declared = frozenset(column.support_values) if declared_only else None
    buckets: Dict[str, List[int]] = {}
    for index, row in enumerate(rows):
        named = first_row + index
        value = cell(row, column, row_index=named)
        if value is None:
            if column.null_policy is NullPolicy.FAIL:
                raise FluxError(FLX_5005, f"column {column.name!r} row {named}: null is refused")
            continue
        key = category(value, column, row_index=named)
        if declared is not None and key not in declared:
            raise FluxError(
                FLX_5005,
                f"column {column.name!r} row {named}: value outside the declared support",
            )
        buckets.setdefault(key, []).append(index)
    return [(key, buckets[key]) for key in sorted(buckets)]


_Read = TypeVar("_Read")


def read_column(
    rows: Sequence[Row],
    indices: Sequence[int],
    column: Column,
    read: Callable[..., _Read],
    *,
    first_row: int = 0,
) -> List[_Read]:
    """Read one column over *indices* with *read*; return what it read, in row order.

    A null under ``exclude`` drops the record from this property; a null
    under ``fail`` refuses the check. Both are decided by the column's own
    declaration, never by what would make the comparison pass. *read* is
    handed each non-null cell and its ``row_index`` in row order, so the
    first record that refuses is the one a refusal names, whichever rule it
    breaks. A row is named by its index plus *first_row*, as in
    :func:`group_keys`.
    """
    values: List[_Read] = []
    for index in indices:
        named = first_row + index
        value = cell(rows[index], column, row_index=named)
        if value is None:
            if column.null_policy is NullPolicy.FAIL:
                raise FluxError(FLX_5005, f"column {column.name!r} row {named}: null is refused")
            continue
        values.append(read(value, row_index=named))
    return values


def read_pairs(
    rows: Sequence[Row],
    indices: Sequence[int],
    first: Column,
    second: Column,
    grid: Grid,
) -> List[Tuple[int, int]]:
    """Read a correlation's two columns over *indices*: encoded pairs, in row order."""
    pairs: List[Tuple[int, int]] = []
    for index in indices:
        left = cell(rows[index], first, row_index=index)
        right = cell(rows[index], second, row_index=index)
        if left is None or right is None:
            # A correlation reads a pair. A record missing either half
            # contributes to neither sum -- but §5's fail-closed list
            # names "a null in a column declared ``null: fail``", and a
            # row null in BOTH halves is null in each of them. Reading
            # the policy from one column only would let a `null: fail`
            # declaration on the second column never fire whenever the
            # first is `null: exclude`, so both are consulted.
            for missing, half in ((first, left), (second, right)):
                if half is None and missing.null_policy is NullPolicy.FAIL:
                    raise FluxError(
                        FLX_5005, f"column {missing.name!r} row {index}: null is refused"
                    )
            continue
        x = encode_value(left, first, grid, row_index=index)
        y = encode_value(right, second, grid, row_index=index)
        pairs.append((x, y))
    return pairs
