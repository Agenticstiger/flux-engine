# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``format_seconds`` and ``format_rfc3339_ms`` spell instants without ``strftime``.

``strftime`` is a libc call that is far slower in a forked worker than in its parent on
some platforms, so a four-digit year is formatted from the datetime fields. These tests
pin that spelling to ``strftime`` itself and count the calls that still reach it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

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
    assert clock.format_seconds(aware) == aware.strftime(_FORMAT)


def test_every_second_of_a_day_equals_strftime() -> None:
    start = datetime(2024, 3, 10)
    for second in range(86400):
        moment = start + timedelta(seconds=second)
        assert clock.format_seconds(moment) == moment.strftime(_FORMAT)


@pytest.mark.parametrize("ms", [0, 1, 999, 1000, -1, -999, -1000, 951782400000, -62135596800000])
def test_rfc3339_ms_equals_the_strftime_spelling(ms: int) -> None:
    moment = _EPOCH + timedelta(milliseconds=ms)
    expected = f"{moment.strftime(_FORMAT)}.{moment.microsecond // 1000:03d}Z"
    assert clock.format_rfc3339_ms(ms) == expected


def test_milliseconds_zero_and_999_keep_three_digits() -> None:
    assert clock.format_rfc3339_ms(0) == "1970-01-01T00:00:00.000Z"
    assert clock.format_rfc3339_ms(999) == "1970-01-01T00:00:00.999Z"


def test_conform_spelling_equals_strftime() -> None:
    moment = datetime(2024, 2, 29, 23, 59, 59, 999000, tzinfo=timezone.utc)
    assert conform._isoformat_z(moment) == moment.strftime(_FORMAT) + ".999Z"


def test_strftime_is_not_called_for_a_four_digit_year(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[datetime] = []
    real = clock._strftime_seconds

    def counting(moment: datetime) -> str:
        calls.append(moment)
        return real(moment)

    monkeypatch.setattr(clock, "_strftime_seconds", counting)
    for ms in (0, 999, 951782400000, -2208988800000, 253402300799999):
        clock.format_rfc3339_ms(ms)
    conform._isoformat_z(datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert calls == []
    clock.format_rfc3339_ms(-62135596800000)
    assert len(calls) == 1
