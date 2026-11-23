# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim replay --assert-digest``: re-execute a recorded plan, prove the digest.

Re-runs :func:`flux_engine.stages.apply.apply` against the same bundle
directory and the same recorded plan, into a *fresh* run id derived from
the same ``planDigest`` -- since the run id is a pure function of the plan
(:func:`flux_engine.stages.layout.run_id_for`), a replay of an unmoved
bundle lands in the exact same run directory apply already wrote, and
overwrites it with byte-identical artifacts. (A bundle that declares a generator plugin is the
exception: its bytes are stored, and its replay generates into a scratch directory instead.)
Comparing the resulting ``runDigest`` against ``--assert-digest`` is therefore also a check that
"replay" and "the original run" are the same word for the same bytes,
not two similar processes that happen to agree.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.api.stages import StageResult
from flux_engine.core.errors import FluxError
from flux_engine.generators import environment as _environment
from flux_engine.generators.errors import FLX_3123
from flux_engine.generators.loading import forbidden_roots
from flux_engine.generators.registry import resolve
from flux_engine.safeio import read_bytes_nofollow, require_real_directory, write_bytes_atomic
from flux_engine.seam.coverage import printable
from flux_engine.seam.generators import GeneratorDeclaration
from flux_engine.stages.apply import apply as run_apply
from flux_engine.stages.apply import load_recorded_plan, plan_kernel
from flux_engine.stages.errors import ReplayDigestMismatch
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import GENERATORS_PLAN_KEY
from flux_engine.stages.stored import declared_for_run, require_record

__all__ = ["replay"]

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def replay(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    assert_digest: str,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    jobs: int = 1,
    allow_generators: Sequence[str] = (),
) -> StageResult:
    """Re-apply the plan at *run_id* and check the resulting ``runDigest``.

    A replay generates the rows again, so a bundle that declares a generator plugin needs
    *allow_generators* to name it (``--allow-generator``, ADR 0032 section 3).

    A plugin's bytes are stored (ADR 0032 section 6), and generating them again is only expected to
    give them when the bundle declares the plugin ``bytes``, or ``environment`` and the environment
    the engine measures now has the digest the plan recorded, and the install is not editable. A
    replay that cannot generate a plugin's rows again is refused (``FLX-3123``) and says that the
    bytes are stored and it verified nothing: it never reports success from the stored run, and it
    does not compare ``--assert-digest`` with the stored receipt. No plugin is imported, so no
    ``--allow-generator`` is needed for the refusal.

    A run whose plugin bytes are stored is the run of record, and a replay of it never writes into
    its directory: the plugin's rows are generated into a scratch directory under the state
    directory, which holds a copy of ``plan.json`` and nothing else of the run, and which is removed
    afterwards, so a plugin that gives other rows the second time is reported as a digest mismatch
    and the stored stream, receipt, plan and stage are as they were. A bundle that declares no
    plugin generates the engine's own rows, a pure function of the plan, and is replayed over its
    own run directory as before.
    """
    refused, stored = _refuse_stored(
        bundle_directory,
        run_id=run_id,
        simulation_id=simulation_id,
        state_dir=state_dir,
        allowed=allow_generators,
    )
    if refused is not None:
        return refused
    if stored:
        state = Path(state_dir) if state_dir is not None else Path(".flux")
        # The mkdtemp name is fresh and 0700; it sits in the state directory, next to runs/ and
        # never inside it, so nothing that lists the runs sees it, and it is gone when we leave.
        try:
            require_real_directory(state, create=True)
            scratch = Path(tempfile.mkdtemp(prefix=".replay-", dir=state))
        except FluxError as error:
            return _refused(error)
        except OSError as failure:
            return _refused(
                FluxError(FLX_3123, f"replay could not make a scratch directory: {failure}")
            )
        try:
            return _verify(
                bundle_directory,
                run_id=run_id,
                assert_digest=assert_digest,
                simulation_id=simulation_id,
                state_dir=scratch,
                recorded=RunLayout.for_state_dir(state, run_id),
                zone=zone,
                jobs=jobs,
                allow_generators=allow_generators,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return _verify(
        bundle_directory,
        run_id=run_id,
        assert_digest=assert_digest,
        simulation_id=simulation_id,
        state_dir=state_dir,
        recorded=None,
        zone=zone,
        jobs=jobs,
        allow_generators=allow_generators,
    )


def _verify(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    assert_digest: str,
    simulation_id: Optional[str],
    state_dir: Union[str, Path, None],
    recorded: Optional[RunLayout],
    zone: Optional[str],
    jobs: int,
    allow_generators: Sequence[str],
) -> StageResult:
    """Apply the recorded plan again into *state_dir* and compare the run digest with the asserted one.

    *recorded* is the stored run whose ``plan.json`` is copied into the scratch *state_dir* first
    (read and written without following a link); ``None`` when *state_dir* is the run's own.
    """
    if recorded is not None:
        try:
            recorded.require_real_dirs()
            plan_bytes = read_bytes_nofollow(recorded.plan_path)
            assert state_dir is not None
            scratch = RunLayout.for_state_dir(state_dir, run_id).ensure()
            write_bytes_atomic(scratch.plan_path, plan_bytes)
        except FluxError as refusal:
            return _refused(refusal)
    applied = run_apply(
        bundle_directory,
        run_id=run_id,
        simulation_id=simulation_id,
        state_dir=state_dir,
        zone=zone,
        auto_plan=False,
        jobs=jobs,
        use_stage=False,
        allow_generators=allow_generators,
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


def _not_regenerated(
    declared: Sequence[GeneratorDeclaration],
    plan_document: Mapping[str, Any],
    *,
    directory: Path,
    state_dir: Union[str, Path, None],
    allowed: Sequence[str],
) -> List[Tuple[str, str]]:
    """The exposes of a plugin whose rows a replay cannot generate again, each with the reason.

    The reproducibility is the bundle's declaration, the install's editable state is what
    :func:`~flux_engine.generators.registry.resolve` finds (it imports nothing), and the
    environment digest is measured now and compared with the recorded one, which must be well
    formed. ``bytes`` can be generated again; ``environment`` only where the digest measured now is
    the recorded one; ``none`` and an editable install never. A plugin the command did not allow
    is left to ``apply``, which refuses it (``FLX-3119``) before anything is looked up.
    """
    recorded = plan_document.get(GENERATORS_PLAN_KEY) or {}
    roots = forbidden_roots(directory, Path(state_dir) if state_dir is not None else None)
    measured: Optional[str] = None
    found: List[Tuple[str, str]] = []
    for item in sorted(declared, key=lambda declaration: declaration.expose_id):
        if item.reproducibility == "none":
            found.append((item.expose_id, "declared none"))
            continue
        if item.plugin not in allowed:
            continue
        if resolve(item.plugin, item.distribution, item.version, forbidden_roots=roots).editable:
            found.append(
                (item.expose_id, "an editable install, whose code can change without a new version")
            )
            continue
        if item.reproducibility == "bytes":
            continue
        entry = recorded.get(item.expose_id)
        digest = entry.get("environmentDigest") if isinstance(entry, Mapping) else None
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise FluxError(
                FLX_3123,
                "the environment digest this run's plan.json records for a generator plugin is "
                "not well formed; a plan.json that was changed is not trusted; plan the run again",
            )
        if measured is None:
            measured = _environment.environment_digest()
        if measured != digest:
            found.append(
                (
                    item.expose_id,
                    "declared environment, and this environment's digest is not the recorded one",
                )
            )
    return found


def _refused(error: FluxError) -> StageResult:
    return StageResult(
        stage="replay",
        exit_code=1,
        _artifacts={"code": error.code.code},
        stderr=str(error),
        error_code=error.code.code,
    )


def _cannot_generate(run_id: str, reasons: Sequence[Tuple[str, str]]) -> FluxError:
    """The refusal of a replay that cannot generate *reasons*' rows again: it verified nothing.

    An expose id is the bundle's text, so it is written through :func:`printable`.
    """
    named = "; ".join(f"{printable(expose)} ({why})" for expose, why in reasons)
    return FluxError(
        FLX_3123,
        f"replay cannot generate the rows of {named} again, so it verified nothing: the bytes "
        f"of run {run_id} are stored, and the stored bytes are the run of record "
        "(fluxsim verify checks them against the run's digests); a replay never reports "
        "success from the stored run",
    )


def _refuse_stored(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    simulation_id: Optional[str],
    state_dir: Union[str, Path, None],
    allowed: Sequence[str],
) -> Tuple[Optional[StageResult], bool]:
    """The refusal of a replay that cannot generate a plugin's rows again, or ``None`` to go on.

    Also whether the bundle declares a plugin, whose bytes are stored: the replay then generates
    into a scratch directory and leaves the run directory alone.

    A replay verifies by generating again, so one that cannot says so and exits non-zero: it never
    reports success from the stored run. What the plugin is, and what it promises, are the bundle's
    declaration and what the engine measures now. ``plan.json``'s record may only agree with the
    declaration (:func:`~flux_engine.stages.stored.require_record`), so a forged record can make a
    replay refuse and never pass. ``None`` too when the plan or the bundle cannot be read: the apply
    that follows reports it, as it always did.
    """
    layout = RunLayout.for_state_dir(
        Path(state_dir) if state_dir is not None else Path(".flux"), run_id
    )
    try:
        layout.require_real_dirs()
        plan_document = load_recorded_plan(layout)
        kernel = plan_kernel(plan_document)
    except FluxError:
        return None, False
    try:
        declared = declared_for_run(Path(bundle_directory), plan_document, kernel, simulation_id)
        if declared is None:
            return None, False
        require_record(declared, plan_document)
        reasons = _not_regenerated(
            declared,
            plan_document,
            directory=Path(bundle_directory),
            state_dir=state_dir,
            allowed=allowed,
        )
    except FluxError as error:
        return _refused(error), False
    if not reasons:
        return None, bool(declared)
    return _refused(_cannot_generate(run_id, reasons)), False
