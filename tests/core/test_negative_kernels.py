# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §14: the deliberately broken kernels this area owns.

Six kernels are named in §14; two of them are defects in the code covered
by KERNEL-SPEC §2/§8/§10, and those two are registered here:

* **sorted-key (object) row serialiser** -- emitting each row as a JSON
  object keyed by column name instead of as an array aligned to the
  header's declared column order. JCS sorts object keys, so the declared
  order is erased and two exposes that differ only in column order
  produce identical bytes (KERNEL-SPEC §8).
* **wall-clock timestamp in a traced op** -- adding a field to a trace
  record that is read from a clock rather than derived from the run, so
  two runs over identical inputs disagree (KERNEL-SPEC §10, §7's "no
  wall clock").

Each is a real implementation carrying one defect, and each MUST be
caught by the property stated next to it. The tests assert the detection,
and a matching control asserts that the correct implementation passes the
same property -- a property that everything fails would prove nothing.

The other four §14 kernels (spawn-ordered seed tree, float-accumulating
LRM, set-iterating scheduler, uuid4 event id) belong to the seed-tree,
LRM and scheduler modules and are registered alongside those.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.digest import digest_of_stream
from flux_engine.core.errors import FluxError
from flux_engine.core.trace import TraceOp, component_digest, trace_record


class _StubClock:
    """A stand-in for the wall clock: every read returns a new value.

    Deliberately not ``time.time()``. Two runs of a fast test can land in
    the same millisecond, which would make a wall-clock defect go
    undetected and this suite flaky in the direction that matters least
    -- passing. A counter reproduces what a clock does across two runs
    (a different reading) with none of the timing luck.
    """

    def __init__(self) -> None:
        self._reading = 0

    def read_ms(self) -> int:
        self._reading += 1
        return self._reading


# --- row serialisers ----------------------------------------------------

Row = Mapping[str, Any]


def emit_rows_as_arrays(columns: Sequence[str], rows: Sequence[Row]) -> bytes:
    """The correct serialiser (KERNEL-SPEC §8): one JCS array per row."""
    return b"".join(canonical_bytes([row[column] for column in columns]) + b"\n" for row in rows)


def emit_rows_as_objects(columns: Sequence[str], rows: Sequence[Row]) -> bytes:
    """The broken serialiser: one JCS **object** per row.

    Reads naturally, round-trips to a dict, and silently discards the
    declared column order -- which is the whole reason the emit stream is
    array-shaped.
    """
    return b"".join(
        canonical_bytes({column: row[column] for column in columns}) + b"\n" for row in rows
    )


_ROWS: Tuple[Row, ...] = (
    {"subscriber_id": "s-1", "market": "mkt-a"},
    {"subscriber_id": "s-2", "market": "mkt-b"},
)


def _column_order_is_observable(
    serialise: Callable[[Sequence[str], Sequence[Row]], bytes],
) -> bool:
    """Does the stream digest distinguish two declared column orders?

    KERNEL-SPEC §8's property: the emit stream is aligned to "the
    expose's declared column order", so an expose that declares the same
    columns in a different order is a different stream.
    """
    forward = digest_of_stream(serialise(["subscriber_id", "market"], _ROWS), subject="forward")
    reversed_order = digest_of_stream(
        serialise(["market", "subscriber_id"], _ROWS), subject="reversed"
    )
    return forward != reversed_order


# --- trace emitters -----------------------------------------------------

TraceEmitter = Callable[[int], List[Dict[str, Any]]]


def emit_trace_from_the_run(ordinal: int) -> List[Dict[str, Any]]:
    """The correct emitter: every field is derived from the run."""
    return [trace_record(TraceOp.MARKET, ordinal=ordinal, market="mkt-a")]


_CLOCK = _StubClock()


def emit_trace_with_a_wall_clock_field(ordinal: int) -> List[Dict[str, Any]]:
    """The broken emitter: one field is a clock reading.

    Written the way it would really be written -- bypassing
    :func:`trace_record`, because that function's exact-field-set check
    (``FLX-2104``) refuses the extra field. That refusal is the engine's
    first line of defence; the property below is the second, and this
    kernel exists to keep both honest.
    """
    return [
        {
            "op": TraceOp.MARKET.value,
            "ordinal": ordinal,
            "market": "mkt-a",
            "emittedAtMs": _CLOCK.read_ms(),
        }
    ]


def _two_runs_agree(emit: TraceEmitter) -> bool:
    """KERNEL-SPEC §10's determinism property, stated over one component.

    The same bundle, the same seed, the same ordinal: the population
    component digest of two runs must be one digest.
    """
    first = component_digest("population", emit(0))
    second = component_digest("population", emit(0))
    return first == second


# --- the registry -------------------------------------------------------


@dataclass(frozen=True)
class NegativeKernel:
    """One deliberately broken kernel and the property that must catch it."""

    name: str
    defect: str
    spec: str
    property_name: str
    #: Returns True when the property HOLDS. A negative kernel must make
    #: it return False.
    holds: Callable[[], bool]


CORE_NEGATIVE_KERNELS: Tuple[NegativeKernel, ...] = (
    NegativeKernel(
        name="sorted-key-object-row-serialiser",
        defect="rows emitted as JCS objects, so key sorting erases the declared column order",
        spec="KERNEL-SPEC §8",
        property_name="the declared column order is observable in the stream digest",
        holds=lambda: _column_order_is_observable(emit_rows_as_objects),
    ),
    NegativeKernel(
        name="wall-clock-field-in-a-traced-op",
        defect="a trace record carries a clock reading, so two identical runs disagree",
        spec="KERNEL-SPEC §10",
        property_name="two runs over identical inputs produce one component digest",
        holds=lambda: _two_runs_agree(emit_trace_with_a_wall_clock_field),
    ),
)

CORRECT_KERNELS: Tuple[NegativeKernel, ...] = (
    NegativeKernel(
        name="array-row-serialiser",
        defect="none -- the control for the row serialiser",
        spec="KERNEL-SPEC §8",
        property_name="the declared column order is observable in the stream digest",
        holds=lambda: _column_order_is_observable(emit_rows_as_arrays),
    ),
    NegativeKernel(
        name="run-derived-trace-emitter",
        defect="none -- the control for the trace emitter",
        spec="KERNEL-SPEC §10",
        property_name="two runs over identical inputs produce one component digest",
        holds=lambda: _two_runs_agree(emit_trace_from_the_run),
    ),
)


@pytest.mark.property
@pytest.mark.parametrize(
    "kernel", CORE_NEGATIVE_KERNELS, ids=[kernel.name for kernel in CORE_NEGATIVE_KERNELS]
)
def test_the_broken_kernel_fails_its_property(kernel: NegativeKernel) -> None:
    """Each §14 kernel in this area MUST fail the property it breaks."""
    assert not kernel.holds(), (
        f"{kernel.name} ({kernel.spec}) carries the defect "
        f"{kernel.defect!r} but the property {kernel.property_name!r} still "
        "holds: the property has stopped detecting it"
    )


@pytest.mark.property
@pytest.mark.parametrize("kernel", CORRECT_KERNELS, ids=[kernel.name for kernel in CORRECT_KERNELS])
def test_the_correct_kernel_passes_the_same_property(kernel: NegativeKernel) -> None:
    """The control: a property nothing can pass would detect nothing."""
    assert kernel.holds(), f"{kernel.name} ({kernel.spec}) fails {kernel.property_name!r}"


@pytest.mark.unit
def test_the_object_serialiser_really_does_collide() -> None:
    """The defect, shown directly rather than only through the property."""
    forward = emit_rows_as_objects(["subscriber_id", "market"], _ROWS)
    backward = emit_rows_as_objects(["market", "subscriber_id"], _ROWS)
    assert forward == backward
    assert emit_rows_as_arrays(["subscriber_id", "market"], _ROWS) != emit_rows_as_arrays(
        ["market", "subscriber_id"], _ROWS
    )


@pytest.mark.unit
def test_the_wall_clock_field_is_also_refused_at_the_record_boundary() -> None:
    """Defence in depth: the field set is checked before anything is hashed."""
    with pytest.raises(FluxError) as excinfo:
        trace_record(TraceOp.MARKET, ordinal=0, market="mkt-a", emittedAtMs=1)
    assert excinfo.value.code.code == "FLX-2104"


@pytest.mark.unit
def test_the_stub_clock_returns_a_new_reading_every_time() -> None:
    """If the stand-in clock ever repeated, the kernel above would pass."""
    clock = _StubClock()
    readings = [clock.read_ms() for _ in range(3)]
    assert len(set(readings)) == len(readings)
