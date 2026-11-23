# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The size of a run: what ``--scale F`` and ``--shard I/N`` may say, checked once.

Light on purpose: the command line checks these flags where it parses them
(:mod:`flux_engine.cli._args`), before any engine code is loaded, and the stages check
the same values again for a caller that is not the command line. See
:mod:`flux_engine.stages.scaling` for what they do to a run.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Tuple

from flux_engine.core.errors import ErrorCode, FluxError, register

__all__ = [
    "FLX_3404",
    "FLX_3405",
    "MOST_ROWS",
    "MOST_SCALE",
    "optional_scale",
    "parse_scale",
    "parse_shard",
    "scale_text",
    "share_of",
]

FLX_3404: ErrorCode = register(
    "FLX-3404",
    "--shard is not a share of a run",
    "`--shard I/N` asks for the I-th of N shares of every emit's rows, and both are whole "
    "numbers with N at least 1 and I from 1 to N: `--shard 2/4` is the second quarter. "
    "A share is a contiguous range of row indexes, so the shares joined in order are the "
    "bytes of one run. Anything else (a fraction, a zero, an I past N, text) names no share, "
    "and is refused before any row is made. Sharding is a feature of kernel 0.2.0, as scaling "
    "is: a plan under kernel 0.1.0 refuses it.",
)

FLX_3405: ErrorCode = register(
    "FLX-3405",
    "--scale is not a usable size",
    "`--scale F` multiplies every emit's rows and key spans, and F is a plain decimal number "
    "above zero and at most 1000000, such as `2` or `0.5`. A negative number, zero, `nan`, "
    "`inf`, an exponent or text is refused before any row is made, as is a scale that leaves "
    "an emit with more rows than a run can address (10^10). A scale below 1 is refused too when "
    "it leaves a key column fewer than two values, or fewer values than the bins it declares; "
    "the message names the column and the scale to raise. Scaling is a feature of kernel "
    "0.2.0, whose draws it was built with: a plan under kernel 0.1.0, whose draws are frozen, "
    "refuses it. Plan with kernel 0.2.0, which a new plan takes unless it names `--kernel 0.1.0`.",
)

#: The largest F, and the most rows an emit may have once scaled.
MOST_SCALE = Decimal(1_000_000)
MOST_ROWS = 10**10
_PLAIN = re.compile(r"[0-9]{1,18}(\.[0-9]{1,18})?")


def parse_scale(value: Any) -> Decimal:
    """*value* as the scale it names, or ``FLX-3405``. A number or its plain decimal text."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise FluxError(FLX_3405, f"{value!r} is not a number")
    if isinstance(value, str):
        text = value.strip()
    else:
        if isinstance(value, float) and not math.isfinite(value):
            raise FluxError(FLX_3405, f"{value!r} is not a finite number")
        text = format(Decimal(repr(value) if isinstance(value, float) else value), "f")
    if _PLAIN.fullmatch(text) is None:
        raise FluxError(FLX_3405, f"{value!r} is not a plain decimal number above zero")
    try:
        scale = Decimal(text)
    except InvalidOperation as error:  # pragma: no cover - the pattern admits only digits
        raise FluxError(FLX_3405, f"{value!r} is not a number") from error
    if scale <= 0 or scale > MOST_SCALE:
        raise FluxError(FLX_3405, f"a scale is above 0 and at most {MOST_SCALE}, and {text} is not")
    return scale


def scale_text(scale: Decimal) -> str:
    """*scale* as the text ``plan.json`` records: plain digits, no trailing zeros."""
    text = format(scale.normalize(), "f")
    return text


def parse_shard(value: Any) -> Tuple[int, int]:
    """``I/N`` (text, or an ``(I, N)`` pair) as ``(I, N)``, or ``FLX-3404``."""
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]{1,9})/([0-9]{1,9})", value.strip())
        if match is None:
            raise FluxError(FLX_3404, f"{value!r} is not I/N with whole numbers, as 2/4")
        index, count = int(match.group(1)), int(match.group(2))
    else:
        try:
            index, count = value
        except (TypeError, ValueError) as error:
            raise FluxError(FLX_3404, f"{value!r} is not an (I, N) pair") from error
        if (
            isinstance(index, bool)
            or isinstance(count, bool)
            or not isinstance(index, int)
            or not isinstance(count, int)
        ):
            raise FluxError(FLX_3404, f"{value!r} is not two whole numbers")
    if count < 1 or not 1 <= index <= count:
        raise FluxError(FLX_3404, f"{index}/{count} is not a share: N is at least 1, I is 1 to N")
    return index, count


def share_of(rows: int, shard: Tuple[int, int]) -> Tuple[int, int]:
    """The ``[start, stop)`` row range of share ``I`` of ``N`` of an emit of *rows* rows.

    Contiguous, in order, and covering every row once: share ``I`` starts where share
    ``I - 1`` stops, and the first share starts at row 0 and the last stops at *rows*.
    """
    index, count = shard
    return rows * (index - 1) // count, rows * index // count


def optional_scale(value: Optional[Any]) -> Decimal:
    """The scale a stage was given, or 1 when it was given none."""
    return Decimal(1) if value is None else parse_scale(value)
