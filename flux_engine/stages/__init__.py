# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The six-step loop's seven stages: validate, plan, apply, run, verify, ship, replay.

Each stage is a pure function of its arguments returning a
:class:`flux_engine.api.stages.StageResult` -- nothing here is a CLI
command in itself; :mod:`flux_engine.cli` and :mod:`flux_engine.engine`
are the two callers, one synchronous and one async, that wrap these for a
terminal and for an in-process consumer respectively. Neither wrapper
re-derives any stage's behaviour: they only decide how its result is
presented.

:mod:`flux_engine.stages.pipeline` is the shared execution core every
stage other than ``ship`` and ``replay`` calls at least once; it is not
itself one of the seven CLI-facing stages.
"""

from __future__ import annotations

from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.replay import replay
from flux_engine.stages.run import run
from flux_engine.stages.ship import ship
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify

__all__ = ["apply", "plan", "replay", "run", "ship", "validate", "verify"]
