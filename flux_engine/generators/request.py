# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a generator plugin is handed: the request, and the statement it must give back (ADR 0032).

A plugin is a plain object with two methods::

    class Generator:
        def statement(self, request) -> Mapping[str, Any]: ...
        def generate(self, request, start, stop) -> Sequence[Sequence[Any]]: ...

``statement`` says, before any row is drawn, what the run rests on. It returns a mapping shaped
as the bundle declares it::

    {
        "grade": "P4",
        "privacy": {"unit": "...", "epsilon": "3", "delta": "0.00001", "accounting": "..."},
        "model": {"digest": "sha256:..."},
        "reproducibility": "bytes",
    }

``privacy`` is the account of a ``P4`` plugin (its loss is ``epsilon`` and ``delta``, or ``rho``),
and ``model`` is present when the bundle declares a model digest. ``generate`` returns one list
of values per requested column, in the order of ``request.columns``, each of length
``stop - start``.

The request is the same object for both calls. It holds only plain data and never a path of the
run: a plugin does not know where its values will be written.

This module imports nothing beyond the standard library, so a plugin may be written without the
engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Tuple

__all__ = ["ColumnRequest", "Request", "SEED_BYTES"]

#: The length of the seed a request carries, in bytes.
SEED_BYTES = 32


@dataclass(frozen=True)
class ColumnRequest:
    """One column the plugin generates: what a value of it may be.

    ``type`` is the closed set's spelling of the column's type: ``STRING``, ``INT``,
    ``DECIMAL(p,s)``, ``BOOL``, ``DATE``, ``TIMESTAMP``, ``JSON`` or ``BYTES``. A value is a
    ``str``, an ``int``, a :class:`decimal.Decimal`, a ``bool``, a :class:`datetime.date`, a
    :class:`datetime.datetime`, a JSON value or ``bytes`` as the type says, and ``None`` only
    when ``nullable``. ``constraints`` holds what the bundle declares for the column and the
    engine holds the values to: ``range`` (``{"min": ..., "max": ...}`` as written in the
    bundle), ``levels`` (the values the column may take, as text), and ``length`` (the number of
    characters of a string).
    """

    name: str
    type: str
    nullable: bool
    constraints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Request:
    """The expose a plugin generates a part of, and which rows of it.

    ``n`` is the number of rows of the whole expose, and ``start`` and ``stop`` the rows this call
    covers (``0 <= start <= stop <= n``). ``seed`` is 32 bytes derived from the run's root, the
    simulation and the expose, so it moves when the run's seed does. ``options`` is a copy of the
    bundle's plain data for the plugin.
    """

    expose_id: str
    columns: Tuple[ColumnRequest, ...]
    n: int
    start: int
    stop: int
    seed: bytes
    options: Mapping[str, Any] = field(default_factory=dict)

    def covering(self, start: int, stop: int) -> "Request":
        """This request for the rows *start* to *stop* of the same expose."""
        return replace(self, start=start, stop=stop)
