# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``format_seconds`` and ``format_rfc3339_ms`` spell instants without ``strftime``.

``strftime`` is a libc call that is far slower in a forked worker than in its parent on
some platforms, and its ``%Y`` pads a year below 1000 on some platforms only. So the text is
formatted from the datetime fields, with a four-digit year everywhere. These tests pin that
spelling to ``strftime``'s for every field but the year, which they pin to four digits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from flux_engine.core import clock
from flux_engine.seam import conform

_FORMAT = "%Y-%m-%dT%H:%M:%S"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_EDGES = [
    datetime(1, 1, 1, 0, 0, 0),
    datetime(999, 12, 31, 23, 59, 59),
    datetime(1000, 1, 1, 0, 0, 0),
    datetime(1969, 12, 31, 23, 59, 59),
    datetime(1970, 1, 1, 0, 0, 0),
    datetime(2000, 2, 29, 12, 30, 15),
    datetime(2024, 2, 29, 23, 59, 59),
    datetime(1900, 2, 28, 1, 2, 3),
    datetime(9999, 12, 31, 23, 59, 59),
]


@pytest.mark.parametrize("moment", _EDGES)
@pytest.mark.parametrize("tz", [None, timezone.utc, timezone(timedelta(hours=5, minutes=30))])
def test_format_seconds_equals_strftime(moment: datetime, tz: object) -> None:
    aware = moment.replace(tzinfo=tz) if tz is not None else moment  # type: ignore[arg-type]
    assert clock.format_seconds(aware) == _spelled(aware)


def _spelled(moment: datetime) -> str:
    """``strftime``'s spelling of *moment*, with the year written in four digits."""
    return f"{moment.year:04d}{moment.strftime(_FORMAT[2:])}"


def test_every_second_of_a_day_equals_strftime() -> None:
    start = datetime(2024, 3, 10)
    for second in range(86400):
        moment = start + timedelta(seconds=second)
        assert clock.format_seconds(moment) == moment.strftime(_FORMAT)


@pytest.mark.parametrize("ms", [0, 1, 999, 1000, -1, -999, -1000, 951782400000, -62135596800000])
def test_rfc3339_ms_equals_the_strftime_spelling(ms: int) -> None:
    moment = _EPOCH + timedelta(milliseconds=ms)
    expected = f"{_spelled(moment)}.{moment.microsecond // 1000:03d}Z"
    assert clock.format_rfc3339_ms(ms) == expected


def test_milliseconds_zero_and_999_keep_three_digits() -> None:
    assert clock.format_rfc3339_ms(0) == "1970-01-01T00:00:00.000Z"
    assert clock.format_rfc3339_ms(999) == "1970-01-01T00:00:00.999Z"


def test_conform_spelling_equals_strftime() -> None:
    moment = datetime(2024, 2, 29, 23, 59, 59, 999000, tzinfo=timezone.utc)
    assert conform._isoformat_z(moment) == moment.strftime(_FORMAT) + ".999Z"


def test_a_year_below_1000_has_four_digits_on_every_platform() -> None:
    # glibc's strftime writes the year 1 as "1", which is no RFC 3339 instant.
    assert clock.format_rfc3339_ms(-62135596800000) == "0001-01-01T00:00:00.000Z"
    assert clock.format_seconds(datetime(999, 12, 31, 23, 59, 59)) == "0999-12-31T23:59:59"
    assert clock.format_seconds(datetime(10, 6, 1, 0, 0, 0)) == "0010-06-01T00:00:00"
    moment = datetime(1, 1, 1, tzinfo=timezone.utc)
    assert conform._isoformat_z(moment) == "0001-01-01T00:00:00.000Z"
