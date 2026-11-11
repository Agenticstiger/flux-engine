# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim replay --assert-digest``: re-execute a recorded plan, prove the digest.

Re-runs :func:`flux_engine.stages.apply.apply` against the same bundle
directory and the same recorded plan, into a *fresh* run id derived from
the same ``planDigest`` -- since the run id is a pure function of the plan
(:func:`flux_engine.stages.layout.run_id_for`), a replay of an unmoved
bundle lands in the exact same run directory apply already wrote, and
overwrites it with byte-identical artifacts. Comparing the resulting
``runDigest`` against ``--assert-digest`` is therefore also a check that
"replay" and "the original run" are the same word for the same bytes,
not two similar processes that happen to agree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from flux_engine.api.stages import StageResult
from flux_engine.stages.apply import apply as run_apply
from flux_engine.stages.errors import ReplayDigestMismatch

__all__ = ["replay"]


def replay(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    assert_digest: str,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
) -> StageResult:
    """Re-apply the plan at *run_id* and check the resulting ``runDigest``."""
    applied = run_apply(
        bundle_directory,
        run_id=run_id,
        simulation_id=simulation_id,
        state_dir=state_dir,
        zone=zone,
        auto_plan=False,
    )
    if not applied.success:
        return StageResult(
            stage="replay",
            exit_code=applied.exit_code,
            _artifacts=dict(applied.artifacts),
            stderr=applied.stderr,
            # The envelope's `error.code` is the field the --json envelope
            # offers a caller so it never has to regex stderr, and this branch
            # was the one stage that dropped it: `fluxsim replay
            # --assert-digest` is the command most likely to be wired into
            # a CI gate, and it reported `null` for failures that carried
            # FLX-2101, FLX-2102 or FLX-2107.
            error_code=applied.error_code,
        )

    replayed_digest = applied.artifacts["run_digest"]
    if replayed_digest != assert_digest:
        error = ReplayDigestMismatch(asserted=assert_digest, replayed=replayed_digest)
        return StageResult(
            stage="replay",
            exit_code=1,
            _artifacts={
                "code": error.code.code,
                "asserted": assert_digest,
                "replayed": replayed_digest,
            },
            stderr=str(error),
            error_code=error.code.code,
        )

    return StageResult(
        stage="replay",
        exit_code=0,
        _artifacts={"run_digest": replayed_digest, "run_id": run_id},
        stdout=f"replay of {run_id} matches asserted digest {assert_digest}",
    )
