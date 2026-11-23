# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The append-only trace log."""

from __future__ import annotations

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.trace import COMPONENT_OPS, TraceOp
from flux_engine.world.tracing import TraceLog

pytestmark = pytest.mark.unit


def test_records_keep_emission_order() -> None:
    log = TraceLog()
    for ordinal in (2, 0, 1):
        log.record(TraceOp.MARKET, ordinal=ordinal, market="mkt-a")
    assert [record["ordinal"] for record in log.records] == [2, 0, 1]
    assert len(log) == 3
    assert [record["ordinal"] for record in log] == [2, 0, 1]


def test_a_wrong_field_set_is_refused_at_the_record_not_at_the_digest() -> None:
    log = TraceLog()
    with pytest.raises(FluxError) as raised:
        log.record(TraceOp.MARKET, ordinal=0)
    assert "FLX-2104" in str(raised.value)
    assert len(log) == 0


def test_an_op_outside_the_closed_vocabulary_is_refused() -> None:
    log = TraceLog()
    with pytest.raises(FluxError) as raised:
        log.record("invented", ordinal=0)
    assert "FLX-2103" in str(raised.value)


def test_of_selects_one_op() -> None:
    log = TraceLog()
    log.record(TraceOp.MARKET, ordinal=0, market="a")
    log.record(TraceOp.COHORT, worldId="w", lifecycle="new", count=1)
    assert len(log.of(TraceOp.MARKET)) == 1
    assert len(log.of(TraceOp.COHORT)) == 1
    assert log.of(TraceOp.TRAIT) == ()


def test_component_digests_cover_every_component_the_spec_names() -> None:
    log = TraceLog()
    log.record(TraceOp.COHORT, worldId="w", lifecycle="new", count=1)
    digests = log.component_digests()
    assert set(digests) == set(COMPONENT_OPS)


def test_a_digest_moves_when_a_record_moves() -> None:
    first, second = TraceLog(), TraceLog()
    for log, order in ((first, (0, 1)), (second, (1, 0))):
        for ordinal in order:
            log.record(TraceOp.MARKET, ordinal=ordinal, market="mkt-a")
    assert first.component_digests()["population"] != second.component_digests()["population"]


def test_the_log_offers_no_way_to_remove_a_record() -> None:
    """A component digest recomputed over an edited log would prove nothing."""
    log = TraceLog()
    assert not [name for name in dir(log) if name in {"pop", "clear", "remove", "__delitem__"}]
