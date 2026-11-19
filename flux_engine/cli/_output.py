# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How a command's result reaches the terminal: the JSON envelope or the human text.

Every command ends in :func:`_print_result` (or :func:`_finish`, which adds the next
steps a person at a terminal gets), so the envelope's shape is decided in one place.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from flux_engine._links import error_url
from flux_engine.cli._progress import progress_enabled


def _print_result(result: Any, *, as_json: bool, next_steps: Sequence[str] = ()) -> int:
    """Print one :class:`~flux_engine.api.stages.StageResult` and return its exit code.

    The ``--json`` envelope is the same shape for every command: ``stage``,
    ``exitCode`` and ``ok`` so a caller never has to infer which command
    produced a document or guess its success from a bare exit code;
    ``artifacts``, the stage's own structured output; and ``error``,
    always present (``null`` on success) and always carrying a ``message``
    alongside the ``code`` -- a caller that wants the reason a stage
    failed never has to fall back to regexing stderr, and stderr is
    always still printed too so a human reading the terminal is not left
    staring at a JSON blob for the one line that matters.
    """
    artifacts = _json_safe(dict(result.artifacts))
    if as_json:
        error: Optional[Dict[str, Any]] = None
        if not result.success:
            # `result.error_code`, never `artifacts["code"]`: artifacts are a
            # stage's OUTPUT, and `fluxsim explain <unregistered code>` puts it
            # in artifacts -- scraping that reported the asked-about code as the
            # reason the command failed. A failure with no catalogued code
            # reports null rather than borrowing an unrelated value.
            error = {
                "code": getattr(result, "error_code", None),
                "message": (result.stderr or "").strip() or None,
            }
        envelope = {
            "stage": result.stage,
            "exitCode": result.exit_code,
            "ok": result.success,
            "artifacts": artifacts,
            "error": error,
        }
        print(json.dumps(envelope, indent=2, sort_keys=True, default=str))
    elif result.stdout:
        print(result.stdout)
    if result.stderr:
        # Flush stdout FIRST. stdout is block-buffered the moment it is
        # redirected while stderr never is, so `fluxsim run ... 2>&1 | tee log`
        # -- the normal way this output is read after the fact -- put the
        # stderr line ABOVE the stdout lines that explain it, reading as if
        # the run had failed before it started. The call order was already
        # right; the buffering was not.
        sys.stdout.flush()
        print(result.stderr, file=sys.stderr)
        pointer = None if as_json or result.success else _failure_pointer(result)
        if pointer:
            print(pointer, file=sys.stderr)
    if next_steps and result.success and progress_enabled(as_json=as_json):
        # Courtesy lines for a person at a terminal (see `_hints`): stderr only,
        # never under --json, silenced by FLUX_NO_PROGRESS, like progress.
        sys.stdout.flush()
        print("", file=sys.stderr)
        for step in next_steps:
            print(f"next: {step}", file=sys.stderr)
    return result.exit_code


def _failure_pointer(result: Any) -> Optional[str]:
    """The line after a failure that says where to read more, or ``None``.

    A catalogued failure gets its code's explanation twice over: the offline
    command, and the page that is the same text with its neighbours. The URL is
    built from the code alone (:func:`flux_engine._links.error_url`), so it is
    the same one the catalog's own page lives at.
    """
    code = getattr(result, "error_code", None)
    if not code:
        return None
    return f"more: fluxsim explain {code}  {error_url(code)}"


def _json_safe(value: Any) -> Any:
    """Drop the one artifact value that is not JSON-safe: a live ``PipelineResult``."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items() if k != "pipeline"}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _finish(args: argparse.Namespace, result: Any) -> int:
    """Print *result*, adding the next-step lines a person at a terminal gets."""
    steps: List[str] = []
    if result.success and not args.json:
        from flux_engine.cli._hints import next_steps

        steps = next_steps(result.stage, args, result.artifacts)
    return _print_result(result, as_json=args.json, next_steps=steps)
