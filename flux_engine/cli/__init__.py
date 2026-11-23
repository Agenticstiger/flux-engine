# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""FLUX Simulation Engine CLI.

Argparse-based, mirroring ``fluid_build.cli``'s shape: a thin shell over
:mod:`flux_engine.stages` with lazy imports so ``fluxsim --help`` (and every
other cold invocation) stays fast -- every ``_cmd_*`` function imports the
stage it calls, so a command that only touches the error catalog (``fluxsim
explain``) never pulls in numpy, cryptography or jsonschema.

Stable exit-code convention: 2 for a usage error (argparse's own
convention via ``parser.error``), 1 for a command that ran and failed,
0 for success -- with one deliberate exception: ``fluxsim gate decide``
returns 3 for a well-formed, successfully-computed policy denial, because
"the policy said no" and "this command could not run" are different facts
a caller needs to tell apart without reading stderr. Every ``_cmd_*``
function returns a :class:`~flux_engine.api.stages.StageResult`'s exit
code directly, and prints its ``stdout``/``stderr`` verbatim -- the CLI
never re-derives a message a stage function already composed. Any
exception that escapes a ``_cmd_*`` function is caught once, centrally, by
:func:`_run_command` and rendered as the same envelope a stage's own
failure produces -- never a Python traceback, and never an empty stdout
under ``--json``. Any failure that carries an ``FLX-nnnn`` code puts it in
``error.code`` -- a ``FluxError``, and the ``flux-expr/1`` family, which
is catalogued but not a ``FluxError`` (see :func:`_classified_code`);
anything else reports ``null`` there and its type and message in
``error.message``. Set ``FLUX_DEBUG=1`` for the traceback.

The envelope covers the two paths that used to escape it: ``--version``
under ``--json`` is a ``stage: "version"`` envelope rather than a bare
string, and a usage error is an ``exitCode: 2`` envelope rather than
argparse's usage block over an empty stdout (see
:class:`_FluxArgumentParser`). ``stage`` is the same discriminator whether
a command succeeded or failed: each subparser declares its own
``stage_name`` (see :func:`_stage_of`), so ``accuracy register`` and
``accuracy score`` stay distinguishable at the moment they fail.

The long-running commands announce their stage boundaries on stderr while
they work -- only on a terminal, never under ``--json``; see
:mod:`flux_engine.cli._progress`. stdout is untouched by it.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from typing import Any, Dict, Optional, Sequence, Tuple

from flux_engine._links import ISSUES_URL, command_url
from flux_engine.cli._args import _jobs_from_environment
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example, _children, _FluxArgumentParser
from flux_engine.cli.commands import COMMANDS
from flux_engine.cli.commands.accuracy import (
    _cmd_accuracy_record,
    _cmd_accuracy_register,
    _cmd_accuracy_score,
)
from flux_engine.cli.commands.accuracy import _require_rfc3339 as _require_rfc3339
from flux_engine.cli.commands.apply import _cmd_apply
from flux_engine.cli.commands.calibrate import _cmd_calibrate, check_fit_options
from flux_engine.cli.commands.describe import _cmd_describe
from flux_engine.cli.commands.explain import _cmd_explain
from flux_engine.cli.commands.expr import _cmd_expr_eval, _cmd_expr_model
from flux_engine.cli.commands.forecast import _cmd_forecast_make, _cmd_forecast_score
from flux_engine.cli.commands.gate import _cmd_gate_conform, _cmd_gate_decide
from flux_engine.cli.commands.init import _cmd_init
from flux_engine.cli.commands.plan import _cmd_plan
from flux_engine.cli.commands.plugins import _cmd_plugins
from flux_engine.cli.commands.replay import _cmd_replay
from flux_engine.cli.commands.run import _cmd_run
from flux_engine.cli.commands.ship import _cmd_ship
from flux_engine.cli.commands.story import _cmd_story
from flux_engine.cli.commands.validate import _cmd_validate
from flux_engine.cli.commands.verify import _cmd_verify, check_sample_options

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Build the full ``fluxsim`` argparse tree.

    Public, and part of this package's surface: ``fluxsim describe --self``
    builds its published command tree from this parser, and the
    register-discipline lint walks it. A leading-underscore name carries no
    stability contract, and the document a host reads to render UI must not
    hang off one.
    """
    parser = _FluxArgumentParser(
        prog="fluxsim",
        description=(
            "FLUX Simulation Engine: runs a FLUX bundle deterministically against the FLUID "
            "contract it references, and packs the evidence."
        ),
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    # `-j` here as well as on every subparser (see `_add_json_flag`): the
    # short form was accepted after the verb and rejected before it, so
    # `fluxsim -j verify ...` was a usage error while `fluxsim --json verify
    # ...` and `fluxsim verify ... -j` both worked. One flag, one spelling,
    # either position.
    parser.add_argument("-j", "--json", action="store_true", help="emit machine-readable JSON")
    sub = parser.add_subparsers(dest="command", title="commands", metavar="<command>")

    # The `stage` discriminator, declared once per subcommand and reached by
    # BOTH the success path and `_run_command`'s exception arms. Those arms
    # used to fall back to `args.command` -- the top-level verb -- so a
    # failing `accuracy register` reported `stage: "accuracy"` while the
    # same command reported `stage: "accuracy-register"` when it worked, and
    # a consumer routing on `stage` could not tell `register` from `score`
    # at precisely the moment routing matters.
    # Each command module's configure() declares it for the commands it adds.
    for command in COMMANDS:
        command.configure(sub)

    _attach_help(parser)
    return parser


#: The worked examples shown first in every ``--help``, keyed by the command path
#: ("" is the program itself). Every command has at least one, and
#: ``tests/cli/test_explain_and_examples.py`` parses each against its own parser,
#: so an example that stops being valid fails a test instead of misleading a reader.
_EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "": (
        Example(
            "fluxsim init demo\ncd demo\nfluxsim run . --verify --ship -o out",
            "start with a small working project",
        ),
        Example("fluxsim explain FLX-3105", "look up what an error code means"),
    ),
    **{path: examples for command in COMMANDS for path, examples in command.EXAMPLES.items()},
}


def _attach_help(root: argparse.ArgumentParser) -> None:
    """Give every parser its examples and every command its reference page.

    A nested command (``accuracy register``) points at the page of the command it
    belongs to, because the reference documents ``accuracy`` as one.
    """

    def attach(parser: argparse.ArgumentParser, path: str, url: str) -> None:
        assert isinstance(parser, _FluxArgumentParser)
        parser.examples = _EXAMPLES.get(path, ())
        parser.docs_url = url
        for name, child in _children(parser).items():
            attach(child, f"{path} {name}".strip(), url)

    assert isinstance(root, _FluxArgumentParser)
    root.examples = _EXAMPLES[""]
    root.docs_url = command_url()
    root.footer = (f"issues: {ISSUES_URL}",)
    for name, child in _children(root).items():
        attach(child, name, command_url(name))


#: The pre-0.2 spelling. Kept as an alias so an in-tree caller that has not
#: moved yet keeps working; new code calls :func:`build_parser`.
_build_parser = build_parser


def _stage_of(args: argparse.Namespace) -> str:
    """The stage discriminator for *args*' subcommand.

    ``stage_name`` is set by every subparser (see :func:`_build_parser`),
    so a failure reports the same discriminator the success path does.
    The fallbacks are for a namespace that never reached a subparser.
    """
    return str(getattr(args, "stage_name", None) or getattr(args, "command", None) or "fluxsim")


def _run_command(fn: Any, args: argparse.Namespace) -> int:
    """Call one ``_cmd_*`` function, rendering any escaped exception as an envelope.

    Every one of the six loop stages already catches ``FluxError`` inside
    the stage function itself and returns a failed :class:`StageResult` --
    but ``calibrate``, ``accuracy``, ``gate`` and ``expr`` call library code
    (``load_bundle``, ``resolve_cast``, the expression compiler, the
    calibrator, ``json.loads`` over a caller-supplied file) directly, with
    no stage boundary of their own to catch anything. This is the one place
    every command funnels through, so it is the one place the safety net
    belongs -- and it has to be wider than ``FluxError``, because a
    malformed ``--request`` file raises ``JSONDecodeError``, an unreadable
    path raises ``OSError``, and both used to reach the terminal as a
    traceback with an empty stdout.

    ``FLX-2117`` and ``FLX-2118`` (a run file or state directory that is a link,
    :mod:`flux_engine.safeio`) can also come out of a stage function as a raised
    ``FluxError``: the stage refuses before it has a result to return. They reach
    this boundary like any other catalogued code.
    """
    from flux_engine.api.stages import failure_result

    stage = _stage_of(args)
    try:
        return fn(args)
    except Exception as error:  # noqa: BLE001 - the CLI's outermost boundary
        # A FluxError is not the only way a command can fail: a malformed
        # --request file, an unreadable path, a bug. Every one of those used
        # to escape as a raw Python traceback with ZERO bytes on stdout,
        # breaking both this module's "never a Python traceback" promise and
        # the envelope contract a --json caller reads. The traceback is still
        # available behind FLUX_DEBUG=1, the way gh and kubectl do it.
        if os.environ.get("FLUX_DEBUG"):
            traceback.print_exc()
        # One classification, shared with the async facade
        # (`flux_engine.api.stages.failure_result`): a FluxError reports its
        # own code, the FLX-6nnn expression family reports the code it
        # carries despite not being a FluxError, and anything else reports
        # its type and message. A CLI caller and a library host get the same
        # envelope for the same failure.
        as_json = getattr(args, "json", False)
        result = failure_result(stage, error)
        status = _print_result(result, as_json=as_json)
        if not as_json and not result.error_code and not os.environ.get("FLUX_DEBUG"):
            # Not catalogued, so there is no page to point at. Say how to see
            # more and where to report it; a mistyped path lands here too, which
            # is why this does not claim a bug.
            print(
                f"more: run again with FLUX_DEBUG=1 for the traceback, or report it at {ISSUES_URL}",
                file=sys.stderr,
            )
        return status


def _check_options(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Refuse the options of ``verify`` and ``calibrate`` that do not go together."""
    if args.command == "verify":
        check_sample_options(args, _children(parser)["verify"])
    if args.command == "calibrate":
        check_fit_options(args, _children(parser)["calibrate"])


def _run_forecast(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Run ``forecast make`` or ``forecast score``; a missing subcommand is a usage error."""
    if args.forecast_command == "make":
        return _run_command(_cmd_forecast_make, args)
    if args.forecast_command == "score":
        return _run_command(_cmd_forecast_score, args)
    _children(parser)["forecast"].error("forecast requires a subcommand: make, score")


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        # Run as the program itself (the `fluxsim` script, `python -m
        # flux_engine.cli`), so this process is the CLI's alone: every Python
        # process it starts, the --jobs workers and a resource tracker that
        # multiprocessing has to restart, leaves the current directory off its
        # import path, and the setting never changes while threads run (see
        # flux_engine.stages._parallel). A caller of main(argv) keeps its own
        # environment. An empty value counts as unset, as Python reads it.
        if not os.environ.get("PYTHONSAFEPATH"):
            os.environ["PYTHONSAFEPATH"] = "1"
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Set before the parse, because a usage error happens DURING it: the
    # namespace does not exist yet when `parser.error` has to decide
    # whether the caller wanted an envelope.
    _FluxArgumentParser.json_requested = "--json" in raw_argv or "-j" in raw_argv
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from flux_engine import __version__
        from flux_engine.api.stages import StageResult

        # `--version` is a command like any other under `--json`: it used to
        # print a bare version string, so a wrapper that always passes
        # `--json` and always parses stdout crashed on the one call it makes
        # first.
        return _print_result(
            StageResult(
                stage="version",
                exit_code=0,
                _artifacts={"version": __version__},
                stdout=__version__,
            ),
            as_json=args.json,
        )

    dispatch = {
        "init": _cmd_init,
        "validate": _cmd_validate,
        "plan": _cmd_plan,
        "apply": _cmd_apply,
        "run": _cmd_run,
        "verify": _cmd_verify,
        "ship": _cmd_ship,
        "replay": _cmd_replay,
        "story": _cmd_story,
        "calibrate": _cmd_calibrate,
        "describe": _cmd_describe,
        "plugins": _cmd_plugins,
        "explain": _cmd_explain,
    }
    if getattr(args, "jobs", "absent") is None:
        args.jobs = _jobs_from_environment(_children(parser)[args.command])
    if args.command in dispatch:
        _check_options(args, parser)
        if args.command == "describe" and not args.self_:
            _children(parser)["describe"].error("describe requires --self")
        return _run_command(dispatch[args.command], args)

    if args.command == "accuracy":
        accuracy_dispatch = {
            "register": _cmd_accuracy_register,
            "score": _cmd_accuracy_score,
            "record": _cmd_accuracy_record,
        }
        if args.accuracy_command in accuracy_dispatch:
            return _run_command(accuracy_dispatch[args.accuracy_command], args)
        _children(parser)["accuracy"].error(
            "accuracy requires a subcommand: register, score, record"
        )

    if args.command == "forecast":
        return _run_forecast(args, parser)

    if args.command == "gate":
        if args.gate_command == "conform":
            return _run_command(_cmd_gate_conform, args)
        if args.gate_command == "decide":
            return _run_command(_cmd_gate_decide, args)
        _children(parser)["gate"].error("gate requires a subcommand: decide, conform")

    if args.command == "expr":
        if args.expr_command == "eval":
            return _run_command(_cmd_expr_eval, args)
        if args.expr_command == "model":
            return _run_command(_cmd_expr_model, args)
        _children(parser)["expr"].error("expr requires a subcommand: eval, model")

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
