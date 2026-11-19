# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Stage results and the failure hierarchy, for in-process callers.

The engine facade returns one of these per stage of the six-step loop
(``validate -> plan -> apply -> run -> verify -> ship``, plus ``replay``).
Shape borrowed from forge-cli's ``fluid_build.engine`` so a process that
imports both engines meets one result shape rather than two.

Three choices worth stating.

**Artifacts are read-only.** :attr:`StageResult.artifacts` hands back a
``MappingProxyType``. A consumer cannot edit engine-owned output in place
and pass the edited version on as though the engine had produced it.
``dict(result.artifacts)`` gives a copy to anyone who wants one.

**The exit code is kept alongside** :attr:`StageResult.success`. Different
failures exit differently, and a caller that needs to tell them apart
should not have to re-parse stderr.

**One failure class per stage.** ``except EngineError`` catches any stage;
``except ShipFailed`` catches the one that matters when a bundle was
withheld. A single class for all seven would force every caller into
string matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional

#: ``__all__`` is the *frozen surface* (see ``flux_engine.api``'s docstring
#: and ``tests/api/test_api_surface.py``): Protocols and frozen records that
#: a third-party implementation targets. :func:`classified_code` and
#: :func:`failure_result` are deliberately outside it -- they carry logic,
#: which this package's own doctrine keeps off the surface, and they exist
#: so the CLI and the async facade classify a failure the same way rather
#: than twice. They are importable from this module by the two entrypoints
#: and are not part of the versioned contract.
__all__ = [
    "ApplyFailed",
    "EngineError",
    "PlanFailed",
    "ReplayFailed",
    "RunFailed",
    "ShipFailed",
    "StageName",
    "StageResult",
    "ValidateFailed",
    "VerifyFailed",
]

#: The seven stages, in loop order. A closed tuple: a typo in a caller is a
#: lookup failure here, not a silently-accepted new stage name.
StageName = (
    "validate",
    "plan",
    "apply",
    "run",
    "verify",
    "ship",
    "replay",
)


@dataclass(frozen=True, repr=False)
class StageResult:
    """What one stage returned.

    Attributes
    ----------
    stage
        Which stage produced this; one of :data:`StageName`. Read by
        :meth:`raise_for_status` to pick the failure class.
    exit_code
        ``0`` on success, non-zero otherwise, mirroring the CLI.
    stdout, stderr
        The stage's captured output, verbatim and untruncated. ``__repr__``
        shortens them so a result stays printable; the attributes do not.
    error_code
        The ``FLX-nnnn`` code of the failure this result reports, or
        ``None`` when the stage succeeded (or failed without a catalogued
        code). A first-class field rather than an ``artifacts["code"]``
        convention: artifacts are a stage's *output*, and a stage whose
        output happens to contain a key called ``code`` (``fluxsim explain``
        reports the code it was asked about) must not have that value read
        back as the reason it failed.
    """

    stage: str
    exit_code: int
    _artifacts: Dict[str, Any] = field(default_factory=dict, repr=False)
    stdout: str = ""
    stderr: str = ""
    error_code: Optional[str] = None

    @property
    def artifacts(self) -> Mapping[str, Any]:
        """Read-only view of the stage's structured outputs.

        Supports lookup, ``get``, ``in``, iteration and ``len``; assignment
        raises. ``dict(result.artifacts)`` for a mutable copy.
        """
        return MappingProxyType(self._artifacts)

    @property
    def success(self) -> bool:
        """Whether the stage exited zero.

        Answers one question about one stage. It is not a verdict about a
        run -- that is :class:`~flux_engine.api.verdict.VerifyVerdict`, and
        it deliberately does not reduce to a single boolean.
        """
        return self.exit_code == 0

    def __repr__(self) -> str:
        head = f"{type(self).__name__}(stage={self.stage!r}, exit_code={self.exit_code}"
        return f"{head}, artifacts={sorted(self._artifacts)!r})"

    def raise_for_status(self) -> None:
        """Raise this stage's :class:`EngineError` subclass if it failed.

        Named after ``requests.Response.raise_for_status`` because it
        behaves the same way: silent on success, typed on failure. Callers
        that would rather branch keep using :attr:`success`.
        """
        if not self.success:
            raise _ERROR_CLASSES.get(self.stage, EngineError)(self.stage, self)


def classified_code(error: BaseException) -> Optional[str]:
    """The ``FLX-nnnn`` code *error* carries, or ``None``.

    Two families carry one. A ``FluxError`` holds its catalog entry
    directly; the ``flux-expr/1`` family derives from ``Exception`` rather
    than from ``FluxError``, so ``FLX-6001``..``FLX-6005`` would otherwise
    be reported as an unclassified internal failure -- forcing a consumer
    to read the message text for a code the catalog explains in full.

    The module check comes first so the expression package is imported only
    when the exception actually came from it: a cold command must not pull
    the parser and the compiler onto its failure path.
    """
    from flux_engine.core.errors import FluxError

    if isinstance(error, FluxError):
        return error.code.code
    if not type(error).__module__.startswith("flux_engine.expr"):
        return None
    from flux_engine.expr.errors import FluxExprError

    if isinstance(error, FluxExprError):
        return error.code
    return None


def failure_result(stage: str, error: BaseException) -> StageResult:
    """Render an escaped exception as the failed :class:`StageResult` for *stage*.

    One classification, used by both entrypoints. The CLI already funnelled
    every escaped exception into an envelope; the async facade had no such
    boundary, so one failure was a typed result for a CLI caller and a raw
    Python exception in an in-process host's event loop. A host integrating
    the engine as a library gets what the command line gets.

    A catalogued failure reports its code and its own message; anything
    else reports ``None`` and prefixes the message with the exception type,
    which is all a reader has when the failure is not one the engine
    catalogues.
    """
    code = classified_code(error)
    return StageResult(
        stage=stage,
        exit_code=1,
        stderr=str(error) if code else f"{type(error).__name__}: {error}",
        error_code=code,
    )


class EngineError(RuntimeError):
    """A stage exited non-zero. Carries the :class:`StageResult`.

    The message quotes the first 200 characters of the stage's stderr so a
    log line is useful on its own; the whole result stays reachable at
    :attr:`result` for anyone who needs the rest.
    """

    def __init__(self, stage: str, result: StageResult) -> None:
        detail = (result.stderr or "").strip()[:200]
        super().__init__(f"flux_engine stage {stage!r} exited {result.exit_code}: {detail}")
        self.stage = stage
        self.result = result


class ValidateFailed(EngineError):
    """``fluxsim validate`` exited non-zero."""


class PlanFailed(EngineError):
    """``fluxsim plan`` exited non-zero."""


class ApplyFailed(EngineError):
    """``fluxsim apply`` exited non-zero."""


class RunFailed(EngineError):
    """``fluxsim run`` exited non-zero."""


class VerifyFailed(EngineError):
    """``fluxsim verify`` exited non-zero.

    Reports that the verify stage could not complete. It is not the place
    a verify *readout* is delivered -- that is
    :class:`~flux_engine.api.verdict.VerifyVerdict`, which reports on two
    questions and answers a failure to reproduce without raising.
    """


class ShipFailed(EngineError):
    """``fluxsim ship`` exited non-zero; no bundle was written."""


class ReplayFailed(EngineError):
    """``fluxsim replay`` exited non-zero."""


#: Consulted by :meth:`StageResult.raise_for_status`. An unrecognised stage
#: falls through to the base class rather than passing silently.
_ERROR_CLASSES: Dict[str, type[EngineError]] = {
    "validate": ValidateFailed,
    "plan": PlanFailed,
    "apply": ApplyFailed,
    "run": RunFailed,
    "verify": VerifyFailed,
    "ship": ShipFailed,
    "replay": ReplayFailed,
}
