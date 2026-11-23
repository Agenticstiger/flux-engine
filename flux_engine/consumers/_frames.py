# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The framing both ends of the query process pipe use (ADR 0028 section 3).

A frame is a 4-byte big-endian length and then that many bytes. A message is a
JSON header frame, UTF-8, and, for a table, one more frame of Arrow IPC stream
bytes. Nothing is pickled, and nothing is read that the length did not announce:
a length past the bound the reader was given ends the read before a byte of
the frame is taken.

The header is a JSON object. A float or a ``NaN`` in it is refused, so a count
is a whole number on both sides.

This module imports the standard library only, so the child can load it before
it imports DuckDB.
"""

from __future__ import annotations

import json
import struct
from typing import IO, Any, Dict, Optional

__all__ = [
    "HEADER_LIMIT",
    "REPLY_LIMIT",
    "TABLE_LIMIT",
    "FrameError",
    "decode_header",
    "encode_header",
    "read_frame",
    "read_header",
    "write_frame",
    "write_header",
]

#: The most bytes of a JSON header the parent sends to the child: a request holds one canonical text
#: or two, and the registry bounds a query's text.
HEADER_LIMIT = 4 << 20

#: The most bytes of a reply the child sends: whole numbers, plus the levels of a grouped count.
REPLY_LIMIT = 16 << 20

#: The most bytes of one table's Arrow IPC stream.
TABLE_LIMIT = 1 << 30

_LENGTH = struct.Struct(">I")


class FrameError(ValueError):
    """A frame that is too large, a header that is not a JSON object of whole numbers and text."""


def _refuse_number(text: str) -> Any:
    raise FrameError("a header holds a float or a non-finite number")


def encode_header(document: Dict[str, Any]) -> bytes:
    """*document* as the UTF-8 JSON bytes of a header."""
    try:
        return json.dumps(
            document, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise FrameError(f"a header cannot be written: {type(error).__name__}") from error


def decode_header(data: bytes) -> Dict[str, Any]:
    """The JSON object in *data*, or a :class:`FrameError`."""
    try:
        document = json.loads(
            data.decode("utf-8"), parse_float=_refuse_number, parse_constant=_refuse_number
        )
    except FrameError:
        raise
    except (ValueError, RecursionError) as error:
        raise FrameError(f"a header is not JSON in UTF-8: {type(error).__name__}") from error
    if not isinstance(document, dict):
        raise FrameError("a header is not a JSON object")
    return document


def _read_exactly(stream: IO[bytes], size: int) -> bytes:
    parts = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("the pipe closed inside a frame")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def read_frame(stream: IO[bytes], limit: int) -> bytes:
    """One frame of at most *limit* bytes. ``EOFError`` when the pipe is closed."""
    prefix = stream.read(_LENGTH.size)
    if not prefix:
        raise EOFError("the pipe closed")
    if len(prefix) < _LENGTH.size:
        prefix += _read_exactly(stream, _LENGTH.size - len(prefix))
    (length,) = _LENGTH.unpack(prefix)
    if length > limit:
        raise FrameError(f"a frame announces {length} bytes; the bound is {limit}")
    return _read_exactly(stream, length) if length else b""


def write_frame(stream: IO[bytes], data: bytes, limit: int) -> None:
    """Write one frame of at most *limit* bytes and flush."""
    if len(data) > limit:
        raise FrameError(f"a frame holds {len(data)} bytes; the bound is {limit}")
    stream.write(_LENGTH.pack(len(data)) + data)
    stream.flush()


def read_header(stream: IO[bytes], limit: int) -> Dict[str, Any]:
    """The next frame, decoded as a header."""
    return decode_header(read_frame(stream, limit))


def write_header(
    stream: IO[bytes], document: Dict[str, Any], limit: int, payload: Optional[bytes] = None
) -> None:
    """Write a header frame, then *payload* as a frame of its own when there is one."""
    write_frame(stream, encode_header(document), limit)
    if payload is not None:
        write_frame(stream, payload, TABLE_LIMIT)
