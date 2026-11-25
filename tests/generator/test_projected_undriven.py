# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected plan's columns are produced by the history, so none is refused as undriven.

``build_plan(projected=True)`` marks a snapshot's plan (ADR 0027 section 3): its columns are its
history's last versions and none is drawn. The related-tables check that refuses a key column
nothing produces (``FLX-3403``) skips such a plan's columns and keeps refusing every other plan's.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator.relations import _Resolver
from flux_engine.generator.rows import GeneratorPlan, build_plan
from flux_engine.seam.contract import compile_expose

from .test_relations import _column, _expose, _h, _primary

pytestmark = pytest.mark.unit

EMIT = "snapshot"


def _plan(*, projected: bool) -> GeneratorPlan:
    """A plan whose column ``label`` is a key column (related) that declares no statistics."""
    expose = _expose(EMIT, [_column("k", "INT"), _column("label", "STRING")], _primary("e", "k"))
    return build_plan(
        compile_expose(expose),
        sim_id="sim",
        seam_digest=_h(EMIT),
        rows=4,
        fidelity={"columns": {"k": {"range": {"min": "1", "max": "9"}}}},
        related=frozenset({"label"}),
        projected=projected,
    )


def _check(plan: GeneratorPlan) -> None:
    resolver: Any = SimpleNamespace()
    _Resolver._check_undriven(resolver, SimpleNamespace(plan=plan), {})  # type: ignore[arg-type]


def test_build_plan_records_whether_the_plan_is_projected() -> None:
    assert _plan(projected=True).projected and not _plan(projected=False).projected


def test_the_flag_is_no_part_of_the_plan_document() -> None:
    assert _plan(projected=True).digest() == _plan(projected=False).digest()


def test_a_projected_plans_undriven_column_is_not_refused() -> None:
    _check(_plan(projected=True))


def test_any_other_plans_undriven_key_column_is_still_flx_3403() -> None:
    with pytest.raises(FluxError) as raised:
        _check(_plan(projected=False))
    assert raised.value.code.code == "FLX-3403" and "'label'" in str(raised.value)
