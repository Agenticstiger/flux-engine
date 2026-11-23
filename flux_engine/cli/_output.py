# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How a command's result reaches the terminal: the JSON envelope or the human text.

Every command ends in :func:`_print_result` (or :func:`_finish`, which adds the next
steps a person at a terminal gets), so the envelope's shape is decided in one place.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flux_engine._links import error_url
from flux_engine.cli._progress import progress_enabled
from flux_engine.core.credentials import names_environment_variable
from flux_engine.core.target_label import WITHHELD, target_label
from flux_engine.sinks._options import PLAIN_OPTIONS

#: The status of a command whose work a closed pipe cut short: 128 plus SIGPIPE's 13.
BROKEN_PIPE_EXIT = 141


def _detach_stdout() -> None:
    """Point stdout at the null device after the reader of it has gone.

    The pattern is the "Note on SIGPIPE" in the ``signal`` module's documentation: without
    it the interpreter's own flush at shutdown raises ``BrokenPipeError`` a second time and
    turns a quiet exit into a traceback. SIGPIPE keeps the interpreter's default of being
    ignored, so a write to the closed pipe raises instead of killing the process.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        # A stdout with no descriptor (a test double, a closed stream) has nothing to redirect.
        pass


def _result_text(result: Any, *, as_json: bool) -> Optional[str]:
    """What a result prints on stdout: its ``--json`` envelope, or its own stdout."""
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
            "artifacts": _json_safe(dict(result.artifacts)),
            "error": error,
        }
        return json.dumps(envelope, indent=2, sort_keys=True, default=str)
    return result.stdout or None


def _write_stdout(text: Optional[str]) -> bool:
    """Print *text* and flush stdout; ``False`` when its reader has gone.

    Flushed here so a closed pipe shows up now, while the status is still the command's own,
    and not at interpreter shutdown. When the reader has gone, stdout then points at the null
    device (:func:`_detach_stdout`).
    """
    try:
        if text is not None:
            print(text)
        sys.stdout.flush()
    except BrokenPipeError:
        _detach_stdout()
        return False
    return True


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
    if not _write_stdout(_result_text(result, as_json=as_json)):
        # The work is done and only the reader left: stop writing, keep the command's status.
        return int(result.exit_code)
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


def _republish_hint(args: argparse.Namespace, result: Any) -> Optional[str]:
    """What to tell a person whose delivery failed after the run was committed.

    The sinks run once the run is written, so a failed delivery leaves the run on disk;
    the command prints how to publish it again to the same target. The target and the
    options are shown without credentials.
    """
    sink = getattr(args, "sink", None)
    sinks = result.artifacts.get("sinks")
    if (
        result.success
        or sink is None
        or not isinstance(sinks, dict)
        or sinks.get("ok") is not False
    ):
        return None
    plan = result.artifacts.get("plan")
    run_id = (
        getattr(args, "run_id", None)
        or (plan.get("run_id") if isinstance(plan, dict) else None)
        or result.artifacts.get("run_id")
    )
    if not run_id:
        return None
    from flux_engine.sinks._secrets import scrub

    sink_word, withheld = _sink_word(str(sink))
    words = ["fluxsim", "apply", str(args.bundle), "--run-id", str(run_id), "--sink", sink_word]
    for option in getattr(args, "sink_opt", None) or ():
        option_word, option_withheld = _option_word(str(option))
        words += ["--sink-opt", option_word]
        withheld = withheld or option_withheld
    if getattr(args, "state_dir", None):
        words += ["--state-dir", str(args.state_dir)]
    fill = (
        f" after replacing each {WITHHELD} with the part that was withheld from it,"
        if withheld
        else ""
    )
    # The words are labels: a target shows its non-secret parts and an option shows its value
    # only when its key never holds a secret. The scrub is for a credential held in the
    # environment that reached a word another way.
    return scrub(
        f"the run {run_id} was kept; once the cause is fixed, publish it again{fill} with: "
        + " ".join(shlex.quote(word) for word in words)
    )


def _sink_word(spec: str) -> Tuple[str, bool]:
    """The ``--sink`` word for *spec* (``NAME:TARGET``), with the target as its label, and whether anything was withheld."""
    name, colon, target = spec.partition(":")
    label = target_label(name, target)
    return f"{name}{colon}{label}", label != target


def _option_word(option: str) -> Tuple[str, bool]:
    """The ``--sink-opt`` word for *option*: the value whole when its key never holds a secret, else ``…``."""
    key, equals, value = option.partition("=")
    if not equals:
        return target_label("sink-opt", option), False
    if key in PLAIN_OPTIONS or names_environment_variable(key):
        return option, False
    return f"{key}={WITHHELD}", True


def _finish(args: argparse.Namespace, result: Any) -> int:
    """Print *result*, adding the next-step lines a person at a terminal gets."""
    hint = _republish_hint(args, result)
    if hint is not None:
        result = dataclasses.replace(result, stderr=f"{result.stderr}\n{hint}")
    steps: List[str] = []
    if result.success and not args.json:
        from flux_engine.cli._hints import next_steps

        steps = next_steps(result.stage, args, result.artifacts)
    return _print_result(result, as_json=args.json, next_steps=steps)
