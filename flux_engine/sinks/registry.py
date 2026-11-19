# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which sinks exist, and how ``--sink NAME:TARGET`` becomes one.

A sink is named on the command line as ``NAME:TARGET`` -- ``parquet:out/``,
``adbc:duckdb`` -- with its settings as repeated ``--sink-opt KEY=VALUE``.
The name picks a *factory*, a callable ``(target, options) -> SinkTarget``;
the :class:`SinkTarget` it returns checks its settings at once and then
hands out one sink per stream (:meth:`SinkTarget.sink_for`). Checking first
means a misspelled option fails before the simulation runs, not after.

**Built-in sinks are names, not imports.** The table below maps each
built-in to a ``module:attribute`` string, imported only when that sink is
selected -- the lazy registry fsspec keeps for its filesystems -- so listing
sinks, and every run without one, loads neither pyarrow nor ADBC.

**Third parties register through an entry point**, without touching the
engine::

    [project.entry-points."flux_engine.sinks"]
    mysink = "my_package.sink:target"

``my_package.sink.target(target: str, options: Mapping[str, str])`` returns
an object with a ``name`` and a ``sink_for(context)`` method; each sink it
returns implements :class:`flux_engine.api.emit.EmitSink` and sets
``receipt`` to a :class:`~flux_engine.sinks.receipts.SinkReceipt` when it
publishes a whole stream (``None`` when it does not). A third-party entry
point is loaded only when its name is selected; ``fluxsim plugins`` lists
it without importing it. A built-in name always wins, and a third-party
entry point that reuses one is listed as shadowed.

Standard library only.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from flux_engine.api.emit import EmitSink
from flux_engine.core.errors import FluxError
from flux_engine.sinks._optional import is_available
from flux_engine.sinks.errors import FLX_3504, FLX_3507
from flux_engine.sinks.receipts import SinkReceipt, StreamContext

__all__ = [
    "BUILTINS",
    "ENTRY_POINT_GROUP",
    "ReceiptSink",
    "SinkFactory",
    "SinkInfo",
    "SinkTarget",
    "available",
    "load_factory",
    "open_target",
    "parse_spec",
]

#: The entry-point group third-party sinks register under.
ENTRY_POINT_GROUP = "flux_engine.sinks"

#: A sink name: what an entry point may be called, short of the ``:`` that
#: separates the name from its target.
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


class ReceiptSink(EmitSink, Protocol):
    """An :class:`~flux_engine.api.emit.EmitSink` that reports what it published."""

    receipt: Optional[SinkReceipt]


class SinkTarget(Protocol):
    """A configured destination: checked settings, and a sink for each stream."""

    name: str

    def sink_for(self, context: StreamContext) -> ReceiptSink:
        """A fresh sink for one stream."""


SinkFactory = Callable[[str, Mapping[str, str]], SinkTarget]


@dataclass(frozen=True)
class _Builtin:
    value: str
    extra: str
    modules: Tuple[str, ...]


#: The sinks this engine ships: name -> where its factory lives, the extra
#: that installs what it needs, and the modules that extra provides.
BUILTINS: Mapping[str, _Builtin] = {
    "parquet": _Builtin("flux_engine.sinks.parquet:target", "arrow", ("pyarrow",)),
    "arrow-ipc": _Builtin("flux_engine.sinks.arrow_ipc:target", "arrow", ("pyarrow",)),
    "adbc": _Builtin("flux_engine.sinks.adbc:target", "adbc", ("pyarrow", "adbc_driver_manager")),
}


@dataclass(frozen=True)
class SinkInfo:
    """One sink ``fluxsim plugins`` lists, found without importing it."""

    name: str
    value: str
    source: str
    extra: str = ""
    installed: bool = True
    shadowed: bool = False

    def as_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "source": self.source,
            "installed": self.installed,
        }
        if self.extra:
            document["extra"] = f"flux-sim-engine[{self.extra}]"
        if self.shadowed:
            document["shadowed"] = True
        return document


def _entry_points() -> Sequence[Any]:
    return tuple(metadata.entry_points(group=ENTRY_POINT_GROUP))


def _source_of(entry_point: Any) -> str:
    dist = getattr(entry_point, "dist", None)
    if dist is None:
        return "entry point"
    return f"{dist.metadata['Name']} {dist.version}"


def available() -> Tuple[SinkInfo, ...]:
    """Every sink that can be named here, built-ins first, each list sorted."""
    sinks: List[SinkInfo] = [
        SinkInfo(
            name=name,
            value=entry.value,
            source="built-in",
            extra=entry.extra,
            installed=all(is_available(module) for module in entry.modules),
        )
        for name, entry in sorted(BUILTINS.items())
    ]
    for entry_point in sorted(_entry_points(), key=lambda point: point.name):
        sinks.append(
            SinkInfo(
                name=entry_point.name,
                value=entry_point.value,
                source=_source_of(entry_point),
                shadowed=entry_point.name in BUILTINS,
            )
        )
    return tuple(sinks)


def _known_names() -> List[str]:
    return sorted({*BUILTINS, *(point.name for point in _entry_points())})


def parse_spec(spec: str) -> Tuple[str, str]:
    """Split ``NAME[:TARGET]`` at its first colon; the target may hold more of them."""
    name, _, target = spec.partition(":")
    if not _NAME.match(name):
        raise FluxError(
            FLX_3507,
            f"{spec!r} does not start with a sink name; write NAME:TARGET, "
            f"for example parquet:out/ (sinks: {', '.join(_known_names())})",
        )
    return name, target


def load_factory(name: str) -> SinkFactory:
    """The factory for sink *name*: a built-in, else the entry point of that name."""
    builtin = BUILTINS.get(name)
    if builtin is not None:
        module_name, _, attribute = builtin.value.partition(":")
        factory: SinkFactory = getattr(importlib.import_module(module_name), attribute)
        return factory
    for entry_point in _entry_points():
        if entry_point.name == name:
            loaded: SinkFactory = entry_point.load()
            return loaded
    raise FluxError(
        FLX_3507,
        f"there is no sink called {name!r}; this environment has {', '.join(_known_names())}",
    )


def open_target(spec: str, options: Optional[Mapping[str, str]] = None) -> SinkTarget:
    """Resolve ``NAME[:TARGET]`` and check its options; nothing is written yet."""
    name, target = parse_spec(spec)
    factory = load_factory(name)
    result = factory(target, dict(options or {}))
    if not hasattr(result, "sink_for"):
        raise FluxError(FLX_3504, f"the {name} sink's factory returned no target")
    return result
