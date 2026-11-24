# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Plan, apply and verify a bundle at several job counts, for the tests that compare the runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify

Digests = Tuple[Any, Dict[str, Any]]


def plan_apply_verify(
    bundle: Path, tmp_path: Path, jobs_list: Sequence[int] = (1, 4)
) -> Tuple[List[Digests], List[Dict[str, Any]]]:
    """Each job count's plan digest and apply digests, and the verified rules of every emit."""
    digests: List[Digests] = []
    emits: List[Dict[str, Any]] = []
    for jobs in jobs_list:
        state = tmp_path / f"state-{jobs}"
        planned = plan(bundle, state_dir=state, jobs=jobs)
        assert planned.success, planned.stderr
        applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
        assert applied.success, applied.stderr
        layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
        verified = run_verify(
            bundle,
            run_id=layout.run_id,
            state_dir=state,
            allow_declared_baseline_drift=True,
            jobs=jobs,
        )
        assert verified.success, verified.stderr
        digests.append(
            (
                planned.artifacts.get("plan_digest"),
                {
                    k: v
                    for k, v in applied.artifacts.items()
                    if "digest" in k.lower() and k != "run_id"
                },
            )
        )
        emits.append(verified.artifacts["rules"]["emits"])
    return digests, emits
