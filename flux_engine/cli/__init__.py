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
import difflib
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple

from flux_engine._links import ISSUES_URL, command_url, error_url
from flux_engine.cli._progress import progress_enabled, stage_progress

__all__ = ["build_parser", "main"]


def _print_result(result: Any, *, as_json: bool, next_steps: Sequence[str] = ()) -> int:
    """Print one :class:`~flux_engine.api.stages.StageResult` and return its exit code.

    The ``--json`` envelope is the same shape for every command: ``stage``,
    ``exitCode`` and ``ok`` so a caller never has to infer which command
    produced a document or guess its success from a bare exit code (M3);
    ``artifacts``, the stage's own structured output; and ``error``,
    always present (``null`` on success) and always carrying a ``message``
    alongside the ``code`` -- a caller that wants the reason a stage
    failed never has to fall back to regexing stderr (M5), and stderr is
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


def _kv_pair_type(item: str) -> str:
    """``argparse`` ``type=`` callable for ``--committed-slice``: validates the
    ``NAME=VALUE`` shape at parse time, so a malformed value is argparse's own
    usage error (exit 2, ``parser.error``'s own formatting) rather than a
    ``SystemExit`` raised by hand deep inside a command function -- the same
    exit code the module docstring already promises for every other usage
    error.

    Both halves are checked here, not only the ``=``: a value that is not a
    ``file://`` URL used to travel all the way into the slice reader and come
    back as ``FLX-5035`` with exit 1, so two spellings of one mistake in one
    flag exited with two different codes. One flag, one usage-error exit.
    """
    if "=" not in item:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {item!r}")
    _name, _, value = item.partition("=")
    if not value.startswith("file://"):
        raise argparse.ArgumentTypeError(
            f"the committed slice is a file:// URL in v0.1; got {value!r} "
            "(a bare path is not one -- write file:///absolute/path.ndjson)"
        )
    return item


#: What ``--committed-slice``'s file has to hold, said in the help text
#: rather than only in a test helper: one JSON **object** per line, keyed by
#: the expose's own contract column names. The engine's own
#: ``emits/<exposeId>.ndjson`` is the other shape (one array per line) and is
#: refused, so the difference has to be discoverable from the CLI.
_SLICE_FORMAT_HELP = (
    "the file holds one JSON object per line, keyed by the expose's contract "
    'column names (e.g. {"subscriber_id": "s-1", "amount": "12.50"})'
)


def _digest_type(value: str) -> str:
    """``argparse`` ``type=`` callable for ``--assert-digest``: ``sha256:<64 hex>``.

    Accepts the bare hex spelling too and puts the prefix back, because the
    slip this closes is copying the hex out of a JSON field or a log line
    and losing the ``sha256:``. Without this, a digest that was right in
    every byte was reported as ``FLX-2106`` -- a reproducibility failure --
    and the flagship determinism claim raised a false alarm over a typo.
    Anything that is not a digest at all is a usage error (exit 2), never a
    verdict about reproducibility.
    """
    candidate = value.strip()
    algorithm, separator, hexadecimal = candidate.rpartition(":")
    if separator and algorithm != "sha256":
        raise argparse.ArgumentTypeError(
            f"--assert-digest must be sha256:<64 hex chars>; got {value!r}"
        )
    if len(hexadecimal) != 64 or any(
        character not in "0123456789abcdef" for character in hexadecimal
    ):
        raise argparse.ArgumentTypeError(
            f"--assert-digest must be sha256:<64 lowercase hex chars>; got {value!r}"
        )
    return f"sha256:{hexadecimal}"


def _stage_name_for_prog(prog: str) -> str:
    """``"fluxsim expr eval"`` -> ``"expr-eval"``; the bare program -> ``"fluxsim"``.

    The discriminator a usage error reports has to be the same one the
    command reports when it succeeds, and argparse's ``prog`` is the only
    thing a subparser knows about itself at ``error()`` time.
    """
    parts = prog.split()[1:]
    return "-".join(parts) if parts else "fluxsim"


@dataclass(frozen=True)
class Example:
    """One worked invocation, shown under ``examples:`` in ``--help``.

    *command* is the text to type. A line that ends in a backslash continues on
    the next, so a long example stays copy-and-paste; separate lines are
    separate commands. *note* is one line of comment shown above it.
    """

    command: str
    note: str = ""

    def one_line(self) -> str:
        """The first command, continuations joined, as one line."""
        first: List[str] = []
        for line in self.command.split("\n"):
            first.append(line.rstrip("\\").strip())
            if not line.rstrip().endswith("\\"):
                break
        return " ".join(part for part in first if part)


class _HelpFormatter(argparse.HelpFormatter):
    """argparse's formatter, minus the ``<command>`` header over the list of commands.

    The commands are already under a ``commands:`` heading; the metavar line above
    them said the same thing a second time.
    """

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return "".join(
                self._format_action(sub) for sub in action._get_subactions()  # noqa: SLF001
            )
        return super()._format_action(action)


def _render_examples(examples: Sequence[Example]) -> str:
    """The ``examples:`` block, verbatim: argparse's own text wrapping would break a command."""
    lines = ["examples:"]
    for position, example in enumerate(examples):
        if position:
            lines.append("")
        if example.note:
            lines.append(f"  # {example.note}")
        continued = False
        for line in example.command.split("\n"):
            lines.append("      " + line.strip() if continued else "  " + line.rstrip())
            continued = line.rstrip().endswith("\\")
    return "\n".join(lines)


class _FluxArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose usage errors honour ``--json`` too, and whose help leads with examples.

    argparse's own ``error()`` prints a usage block to stderr and exits 2
    with **zero bytes on stdout** -- so a wrapper that always passes
    ``--json`` and always parses stdout crashed on every flag typo, which
    is exactly the case where a structured reason is most useful. The
    envelope this emits is the same shape every other command prints, with
    ``exitCode: 2`` and the stage the failing subparser belongs to.

    ``json_requested`` is set once, in :func:`main`, from the raw argv --
    the parse has by definition not happened yet when ``error()`` runs, so
    the namespace cannot answer the question.

    **Help follows clig.dev.** Examples come first, straight after the
    description, because they are what people read; they are printed verbatim,
    since wrapping a command at the terminal width breaks it. The reference page
    for the command follows the options, and the top level adds where to report
    a problem. A usage error says what to type instead: a near miss on a command
    name names the command, anything else shows the first example.
    """

    #: Set by :func:`main` before parsing; subparsers inherit the class,
    #: so setting it on the class reaches every parser in the tree.
    json_requested: bool = False

    #: Worked invocations, shown first in ``--help``. ``scripts/register_lint.py``
    #: fails a command that has none.
    examples: Tuple[Example, ...] = ()
    #: The reference page for this command, printed at the foot of ``--help``.
    docs_url: Optional[str] = None
    #: Further lines at the very end of ``--help``.
    footer: Tuple[str, ...] = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*args, **kwargs)
        self._positionals.title = "arguments"

    def format_help(self) -> str:
        head = self._get_formatter()
        head.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        head.add_text(self.description)
        sections = [head.format_help().rstrip("\n")]
        if self.examples:
            sections.append(_render_examples(self.examples))
        body = self._get_formatter()
        for group in self._action_groups:
            body.start_section(group.title)
            body.add_text(group.description)
            body.add_arguments(group._group_actions)  # noqa: SLF001 - argparse idiom
            body.end_section()
        sections.append(body.format_help().rstrip("\n"))
        foot = ([f"docs: {self.docs_url}"] if self.docs_url else []) + list(self.footer)
        if foot:
            sections.append("\n".join(foot))
        return "\n\n".join(section for section in sections if section) + "\n"

    def parse_args(  # type: ignore[override]
        self,
        args: Optional[Sequence[str]] = None,
        namespace: Optional[argparse.Namespace] = None,
    ) -> argparse.Namespace:
        """``parse_args``, reporting leftover arguments from the parser they were typed to.

        argparse hands what no parser claimed back to the ROOT, which then
        complained in its own voice: the root usage line, and (once examples
        exist) the root's example, for a flag typo on ``fluxsim validate``. The
        command that was reached is the one whose usage and example help.
        """
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            self._leaf_parser(parsed).error(f"unrecognized arguments: {' '.join(extras)}")
        return parsed

    def _leaf_parser(self, parsed: argparse.Namespace) -> "_FluxArgumentParser":
        """The deepest parser the command line reached."""
        node: _FluxArgumentParser = self
        while True:
            chosen: Optional[argparse.ArgumentParser] = None
            for action in node._actions:  # noqa: SLF001 - argparse introspection idiom
                if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                    chosen = action.choices.get(str(getattr(parsed, action.dest, None)))
                    break
            if not isinstance(chosen, _FluxArgumentParser):
                return node
            node = chosen

    def _suggestion(self, message: str) -> str:
        """What to type instead of what was typed, as lines to append to the error."""
        near = re.search(r"invalid choice: '([^']*)'", message)
        if near and "maybe you meant" not in message:  # Python 3.14 suggests by itself
            names: List[str] = []
            for action in self._actions:
                if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                    names.extend(action.choices)
            close = difflib.get_close_matches(near.group(1), names, n=1)
            if close:
                return f"did you mean `{self.prog} {close[0]}`?\n"
        if self.examples:
            return f"for example: {self.examples[0].one_line()}\n"
        return ""

    def error(self, message: str) -> "NoReturn":
        if type(self).json_requested:
            from flux_engine.api.stages import StageResult

            _print_result(
                StageResult(
                    stage=_stage_name_for_prog(self.prog),
                    exit_code=2,
                    stderr=f"{self.prog}: {message}",
                ),
                as_json=True,
            )
            raise SystemExit(2)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n{self._suggestion(message)}")


def _signing_key_type(path: str) -> bytes:
    """``argparse`` ``type=`` callable for ``--signing-key``: 32 raw ed25519 seed bytes.

    Accepts the raw 32 bytes or their hex spelling, so a key written by
    ``openssl rand -hex 32`` and one written by ``openssl rand 32`` both
    work without the caller having to know which this build wanted.

    Validated here, at parse time, and not by hand inside the command:
    a ``SystemExit`` raised deep inside a command function -- which is
    what this replaced -- is a ``BaseException``, so it sailed straight
    past :func:`_run_command`'s safety net and printed nothing at all on
    stdout under ``--json``, with exit 1 for what this CLI's own
    convention calls a usage error. As an ``ArgumentTypeError`` it is
    ``parser.error``'s exit 2, with the envelope
    :class:`_FluxArgumentParser` prints.
    """
    from pathlib import Path

    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise argparse.ArgumentTypeError(f"{path}: {error.strerror or error}") from error
    if len(raw) == 32:
        return raw
    text = raw.decode("ascii", errors="ignore").strip()
    try:
        decoded = bytes.fromhex(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"{path}: not 32 raw bytes and not hex ({error})"
        ) from error
    if len(decoded) != 32:
        raise argparse.ArgumentTypeError(f"{path}: an ed25519 seed is 32 bytes, got {len(decoded)}")
    return decoded


def _trust_key_type(path: str) -> bytes:
    """``argparse`` ``type=`` callable for ``--trust-key``: 32 raw ed25519 public-key bytes.

    Same file shapes as :func:`_signing_key_type` -- the raw 32 bytes or
    their hex spelling -- because the value a caller has is the ``publicKey``
    hex the fidelity record already prints, and making them convert it would
    be a step the tool can take itself.

    The key this reads is the anchor the gate checks a record's signature
    against. Supplying it is what turns a self-attested record into one
    checked against a key chosen by someone other than the signer, which is
    the whole reason the flag exists.
    """
    from pathlib import Path

    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise argparse.ArgumentTypeError(f"{path}: {error.strerror or error}") from error
    if len(raw) == 32:
        return raw
    text = raw.decode("ascii", errors="ignore").strip()
    try:
        decoded = bytes.fromhex(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"{path}: not 32 raw bytes and not hex ({error})"
        ) from error
    if len(decoded) != 32:
        raise argparse.ArgumentTypeError(
            f"{path}: an ed25519 public key is 32 bytes, got {len(decoded)}"
        )
    return decoded


class _OnceAction(argparse.Action):
    """``--sink``: one sink per run. A second ``--sink`` is a usage error, not a quiet replace."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: Optional[str] = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(
                f"{option_string} was given twice; a run writes to one sink. Run again with "
                "the other sink: the run is the same, and its receipt is added."
            )
        setattr(namespace, self.dest, values)


def _sink_opt_type(item: str) -> str:
    """``argparse`` ``type=`` callable for ``--sink-opt``: the ``KEY=VALUE`` shape.

    Only the shape is checked here; which keys a sink takes, and what their
    values may be, is the sink's to say (``FLX-3504``), and it says so before
    the simulation runs.
    """
    key, separator, _value = item.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {item!r}")
    return item


def _kv_pairs(items: Optional[Sequence[str]]) -> Dict[str, str]:
    """Fold already-validated ``NAME=VALUE`` pairs (see :func:`_kv_pair_type`) into a dict."""
    result: Dict[str, str] = {}
    for item in items or ():
        name, _, value = item.partition("=")
        result[name] = value
    return result


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_state_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default=None, help="run-state directory (default: ./.flux)")


#: The environment variable that sets ``--jobs`` when the flag is not given.
JOBS_ENV_VAR = "FLUX_JOBS"


def _jobs_type(text: str) -> int:
    """``argparse`` ``type=`` callable for ``--jobs`` (and ``FLUX_JOBS``): a count or ``auto``.

    ``auto`` is one job per CPU this process may run on.
    """
    value = text.strip().lower()
    if value == "auto":
        from flux_engine.stages._parallel import cpu_jobs

        return cpu_jobs()
    jobs = int(value) if value.isdecimal() else 0
    if jobs < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer or 'auto', got {text!r}")
    return jobs


def _add_jobs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--jobs",
        default=None,
        type=_jobs_type,
        metavar="N",
        help=(
            "the most processes that may generate the rows of an emit declared by "
            "statistics: a number (never more than one per CPU) or auto for one per CPU; "
            "the run summary says how many it used, and the rows and every digest are the "
            f"same whatever the number (default: {JOBS_ENV_VAR}, else 1)"
        ),
    )


def _jobs_from_environment(parser: argparse.ArgumentParser) -> int:
    """``FLUX_JOBS`` for a command given no ``--jobs``: 1 when unset or blank.

    Read after parsing rather than as the option's default, so that the
    parser, and the tree ``fluxsim describe`` prints from it, never depend on
    the environment. A bad value is a usage error of *parser*'s command.
    """
    text = os.environ.get(JOBS_ENV_VAR, "").strip()
    if not text:
        return 1
    try:
        return _jobs_type(text)
    except argparse.ArgumentTypeError as error:
        parser.error(f"{JOBS_ENV_VAR}: {error}")


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Add ``-j/--json`` to *parser*, honoured after the subcommand as well as before it.

    ``default=argparse.SUPPRESS`` is the load-bearing part: when the flag
    is not given on this parser, no attribute is written to the namespace
    at all, so the root parser's own ``--json`` (parsed first, into the
    same ``json`` dest) is left standing. Giving the flag either place --
    ``fluxsim --json verify ...`` or ``fluxsim verify ... --json`` -- reaches the
    same ``args.json`` (B1/M1: every documented comparator CLI in
    CLAUDE.md's table accepts a global flag after the verb).
    """
    parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit machine-readable JSON",
    )


#: One worked invocation per stage, printed under the subcommand's own
#: ``--help``. Every comparator CLI in this project's north-star table
#: (``gh``, ``stripe``, ``kubectl``) answers "what does a real call look
#: like?" in the help text itself rather than only in the manual.
_BUNDLE_HELP = "path to the FLUX bundle directory"
_RUN_ID_HELP = "the run id to act on (as reported by `fluxsim plan`/`fluxsim run`)"
_SIMULATION_ID_HELP = "which Simulation document to resolve (default: the bundle's only one)"
_ZONE_HELP = "declared run zone, for sovereignty checks"
_SLICE_HELP = (
    "the committed real-data slice one expose's fidelity check is measured "
    "against, as EXPOSE_ID=file://PATH; repeatable, once per expose; " + _SLICE_FORMAT_HELP
)
_TRUST_KEY_HELP = (
    "file holding the 32-byte raw ed25519 PUBLIC key (raw or hex -- the "
    "`publicKey` a fidelity record prints) the gate checks the record's "
    "signature against; without it the record is checked against the public "
    "half of the key that signed it and says it is self-attested"
)
_OUT_DIR_HELP = "directory the evidence archive and its statements are written to"


def _read_signing_key(args: argparse.Namespace) -> Optional[bytes]:
    """The already-validated ``--signing-key`` seed, or ``None``.

    The reading and the validation happen at parse time, in
    :func:`_signing_key_type`; by the time a command function asks, the
    namespace holds either the 32 seed bytes or nothing.
    """
    key = getattr(args, "signing_key", None)
    return key if isinstance(key, bytes) else None


def _read_trust_key(args: argparse.Namespace) -> Optional[bytes]:
    """The already-validated ``--trust-key`` public key, or ``None``.

    Read and validated at parse time by :func:`_trust_key_type`, for the
    same reason ``--signing-key`` is.
    """
    key = getattr(args, "trust_key", None)
    return key if isinstance(key, bytes) else None


def build_parser() -> argparse.ArgumentParser:
    """Build the full ``fluxsim`` argparse tree.

    Public, and part of this package's surface: ``fluxsim describe --self``
    builds its published command tree from this parser, and the
    register-discipline lint walks it. A leading-underscore name carries no
    stability contract, and the document a host reads to render UI must not
    hang off one.
    """
    from flux_engine.cli._documents import (
        GATE_REQUEST_FIELDS,
        PREDICTION_FIELDS,
        field_list_help,
        readout_fields,
    )

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

    # First in the list because it is first in time: the command a newcomer
    # types before any bundle of their own exists.
    init_p = sub.add_parser(
        "init",
        help="write a small working project into a new directory",
        description=(
            "Write a small FLUX bundle and its FLUID contract into a directory, so a first "
            "run needs nothing else checked out. The directory must not exist yet, or must "
            "be empty: nothing is ever overwritten."
        ),
    )
    init_p.add_argument("directory", help="where to write the project (created if missing)")
    _add_json_flag(init_p)

    validate_p = sub.add_parser(
        "validate",
        help="validate a FLUX bundle",
        description=(
            "Check a bundle against the FLUX schema and its cross-document rules. "
            "Reads only; writes nothing."
        ),
    )
    validate_p.add_argument("bundle", help=_BUNDLE_HELP)
    _add_json_flag(validate_p)

    plan_p = sub.add_parser(
        "plan",
        help="execute the loop once and record plan.json",
        description=(
            "Execute the whole pipeline once, in memory, and write down what it saw: "
            "inputDigest, the seed-tree paths it materialised, the per-emit seam "
            "digests. Writes plan.json and "
            "seed-paths.ndjson under the run directory."
        ),
    )
    plan_p.add_argument("bundle", help=_BUNDLE_HELP)
    plan_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    plan_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    _add_jobs(plan_p)
    _add_state_dir(plan_p)
    _add_json_flag(plan_p)

    apply_p = sub.add_parser(
        "apply",
        help="apply a recorded plan and write its run directory",
        description=(
            "Re-execute a recorded plan and write every run artifact: the population, "
            "the events, the emit streams and their seam attestations, the run digest "
            "and the receipt. Refuses when the plan or the bundle has moved since the "
            "plan was recorded."
        ),
    )
    apply_p.add_argument("bundle", help=_BUNDLE_HELP)
    apply_p.add_argument(
        "--run-id", default=None, help=f"{_RUN_ID_HELP} (default: take a fresh plan first)"
    )
    apply_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    apply_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    _add_jobs(apply_p)
    _add_state_dir(apply_p)
    _add_json_flag(apply_p)

    run_p = sub.add_parser(
        "run",
        help="plan + apply, with --verify/--sink/--ship sugar",
        description=(
            "Plan and apply in one call, then optionally verify, write every stream to a "
            "sink, and ship. Stops at the first stage that exits non-zero; the artifacts "
            "of every stage that did run are still returned under that stage's own key."
        ),
    )
    run_p.add_argument("bundle", help=_BUNDLE_HELP)
    run_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    run_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    run_p.add_argument(
        "--verify", action="store_true", help="run `fluxsim verify` on the applied run"
    )
    run_p.add_argument(
        "--ship", action="store_true", help="run `fluxsim ship` on the applied run; requires -o"
    )
    run_p.add_argument(
        "-o", "--out-dir", default=None, help=f"{_OUT_DIR_HELP} (required with --ship)"
    )
    run_p.add_argument(
        "--sink",
        default=None,
        action=_OnceAction,
        metavar="NAME:TARGET",
        help=(
            "also write every stream to a sink, after verify and before ship: "
            "parquet:DIR, arrow-ipc:DIR or adbc:DRIVER (`fluxsim plugins` lists them)"
        ),
    )
    run_p.add_argument(
        "--sink-opt",
        action="append",
        type=_sink_opt_type,
        metavar="KEY=VALUE",
        help=(
            "a --sink setting, repeatable, for example row-group-size=100000 or "
            "uri=sim.duckdb; never a password: pass uri-env=VAR instead"
        ),
    )
    run_p.add_argument(
        "--committed-slice",
        action="append",
        type=_kv_pair_type,
        metavar="EXPOSE_ID=file://PATH",
        help=_SLICE_HELP,
    )
    run_p.add_argument(
        "--update-golden",
        action="store_true",
        help="record this run as the golden baseline (requires --reason)",
    )
    run_p.add_argument("--reason", default=None, help="why the golden baseline is being updated")
    run_p.add_argument(
        "--allow-declared-baseline-drift",
        action="store_true",
        help=(
            "exit 0 even when the bundle's declared golden.baselineDigest does not "
            "match this run's runDigest"
        ),
    )
    run_p.add_argument(
        "--signing-key",
        default=None,
        type=_signing_key_type,
        help=(
            "file holding a 32-byte ed25519 seed (raw or hex) to sign fidelity records "
            "with; without it ship signs with a per-invocation key attributable to nobody"
        ),
    )
    run_p.add_argument(
        "--trust-key",
        default=None,
        type=_trust_key_type,
        metavar="PATH",
        help=_TRUST_KEY_HELP,
    )
    _add_jobs(run_p)
    _add_state_dir(run_p)
    _add_json_flag(run_p)

    verify_p = sub.add_parser(
        "verify",
        help="golden regression + row conformance + RFC-04 + fidelity",
        description=(
            "Re-check an applied run on five independent legs: the run's own artifacts "
            "against its recorded digest, the golden regression, row conformance "
            "against the referenced contract, the RFC-04 enforcement vectors, and "
            "every declared fidelity check. One line per leg, with the flag that turns "
            "a `not checked` leg into a result."
        ),
    )
    verify_p.add_argument("bundle", help=_BUNDLE_HELP)
    verify_p.add_argument("--run-id", required=True, help=_RUN_ID_HELP)
    verify_p.add_argument(
        "--update-golden",
        action="store_true",
        help="record this run as the golden baseline (requires --reason)",
    )
    verify_p.add_argument("--reason", default=None, help="why the golden baseline is being updated")
    verify_p.add_argument(
        "--committed-slice",
        action="append",
        type=_kv_pair_type,
        metavar="EXPOSE_ID=file://PATH",
        help=_SLICE_HELP,
    )
    verify_p.add_argument("--vectors", default=None, help="path to an RFC-04 vectors file")
    verify_p.add_argument(
        "--allow-declared-baseline-drift",
        action="store_true",
        help=(
            "exit 0 even when the bundle's declared golden.baselineDigest does not "
            "match this run's runDigest"
        ),
    )
    verify_p.add_argument(
        "--signing-key",
        default=None,
        type=_signing_key_type,
        help=(
            "file holding the 32-byte ed25519 seed a prior `fluxsim ship` signed with; "
            "its public half becomes the trust anchor, and the check says it is "
            "self-attested"
        ),
    )
    verify_p.add_argument(
        "--trust-key",
        default=None,
        type=_trust_key_type,
        metavar="PATH",
        # Not `_TRUST_KEY_HELP`: that text ends "without it the record is
        # checked against the public half of the key that signed it",
        # which is true of `ship` (it holds the key it just signed with)
        # and false here. `verify` signs nothing, so with no key there is
        # no anchor, and the check says so.
        help=(
            "file holding the 32-byte raw ed25519 PUBLIC key (raw or hex -- the "
            "`publicKey` a fidelity record prints) any fidelity record this run "
            "already holds is checked against; without it, and without "
            "--signing-key, the signature is reported as not checked"
        ),
    )
    _add_state_dir(verify_p)
    _add_json_flag(verify_p)

    ship_p = sub.add_parser(
        "ship",
        help="the fidelity gate, then the evidence pack",
        description=(
            "Run the fidelity gate over every emit that declares a fidelity block, and "
            "only if none is withheld, pack the bundle and the run into one evidence "
            "archive with its in-toto statements."
        ),
    )
    ship_p.add_argument("bundle", help=_BUNDLE_HELP)
    ship_p.add_argument("--run-id", required=True, help=_RUN_ID_HELP)
    ship_p.add_argument("-o", "--out-dir", required=True, help=_OUT_DIR_HELP)
    ship_p.add_argument(
        "--committed-slice",
        action="append",
        type=_kv_pair_type,
        metavar="EXPOSE_ID=file://PATH",
        help=_SLICE_HELP,
    )
    ship_p.add_argument(
        "--signing-key",
        default=None,
        type=_signing_key_type,
        help=(
            "file holding a 32-byte ed25519 seed (raw or hex) to sign fidelity records "
            "with; without it the record is signed by a per-invocation key and says so"
        ),
    )
    ship_p.add_argument(
        "--trust-key",
        default=None,
        type=_trust_key_type,
        metavar="PATH",
        help=_TRUST_KEY_HELP,
    )
    _add_state_dir(ship_p)
    _add_json_flag(ship_p)

    replay_p = sub.add_parser(
        "replay",
        help="re-execute a recorded plan; assert its digest",
        description=(
            "Re-execute a recorded plan and refuse unless the run digest it produces is "
            "the one asserted. The determinism claim, as a command."
        ),
    )
    replay_p.add_argument("bundle", help=_BUNDLE_HELP)
    replay_p.add_argument("--run-id", required=True, help=_RUN_ID_HELP)
    replay_p.add_argument(
        "--assert-digest",
        required=True,
        type=_digest_type,
        metavar="sha256:HEX",
        help=(
            "the runDigest this replay must reproduce, as sha256:<64 hex chars>; "
            "the bare hex is accepted and the prefix put back"
        ),
    )
    replay_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    replay_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    _add_jobs(replay_p)
    _add_state_dir(replay_p)
    _add_json_flag(replay_p)

    calibrate_p = sub.add_parser(
        "calibrate",
        help="declared-scorecard calibration for a Playback",
        description=(
            "Read a Playback's declared scorecard and report the credibility and grade "
            "it carries. Declared calibration only: no fitting is performed."
        ),
    )
    calibrate_p.add_argument("bundle", help=_BUNDLE_HELP)
    calibrate_p.add_argument(
        "--playback-id", default=None, help="default: the bundle's only Playback"
    )
    calibrate_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print the scorecard without writing calibration-provenance.json (default)",
    )
    calibrate_p.add_argument(
        "--write",
        dest="dry_run",
        action="store_false",
        help="write calibration-provenance.json into the run directory",
    )
    calibrate_p.add_argument("--run-id", default=None, help="required without --dry-run")
    _add_state_dir(calibrate_p)
    _add_json_flag(calibrate_p)

    accuracy_p = sub.add_parser(
        "accuracy",
        help="outcome-accuracy gate: register, score, record",
        description=(
            "Seal a prediction before its horizon, score it against an observed readout "
            "afterwards, and publish the outcomes-only accuracy record."
        ),
    )
    accuracy_sub = accuracy_p.add_subparsers(
        dest="accuracy_command", title="commands", metavar="<command>"
    )
    reg_p = accuracy_sub.add_parser(
        "register",
        help="seal a prediction before its horizon",
        description=("Seal a PredictionRecord so it cannot be edited after the outcome is known."),
    )
    reg_p.add_argument(
        "--file",
        required=True,
        help=f"a PredictionRecord as JSON -- {field_list_help(PREDICTION_FIELDS)}",
    )
    _add_state_dir(reg_p)
    _add_json_flag(reg_p)
    score_p = accuracy_sub.add_parser(
        "score",
        help="score a registered prediction against a readout",
        description="Bind an ObservedReadout to a sealed prediction and record the verdict.",
    )
    score_p.add_argument(
        "--prediction-id", required=True, help="the id the prediction was registered under"
    )
    score_p.add_argument(
        "--readout",
        required=True,
        help=f"an ObservedReadout as JSON -- {field_list_help(readout_fields())}",
    )
    _add_state_dir(score_p)
    _add_json_flag(score_p)
    record_p = accuracy_sub.add_parser(
        "record",
        help="publish one registration's outcomes-only record",
        description=(
            "Publish the outcomes-only accuracy record for everything registered at one "
            "registration timestamp."
        ),
    )
    record_p.add_argument(
        "--registered-at",
        required=True,
        help=(
            "the registration timestamp to publish, as RFC 3339; only the "
            "predictions whose registered_at is exactly this are covered"
        ),
    )
    record_p.add_argument("--out", required=True, help="path the accuracy record is written to")
    _add_state_dir(record_p)
    _add_json_flag(record_p)

    gate_p = sub.add_parser(
        "gate",
        help="RFC-04 enforcement gate: decide, conform",
        description="Decide one AI-call request against a policy, or run the conformance vectors.",
    )
    gate_sub = gate_p.add_subparsers(dest="gate_command", title="commands", metavar="<command>")
    decide_p = gate_sub.add_parser(
        "decide",
        help="decide one AI-call request against a policy",
        description=(
            "Decide one request against one policy. Exit 0 allowed, 3 denied, 1 the "
            "request could not be decided at all."
        ),
    )
    decide_p.add_argument(
        "--request",
        required=True,
        help=f"the request to decide, as JSON -- {field_list_help(GATE_REQUEST_FIELDS)}",
    )
    _add_json_flag(decide_p)
    conform_p = gate_sub.add_parser(
        "conform",
        help="run the RFC-04 conformance vectors against the enforcement gate",
        description="Run the published RFC-04 conformance vectors and report the pass count.",
    )
    conform_p.add_argument(
        "--vectors",
        default=None,
        help="path to an RFC-04 vectors file (default: the published set)",
    )
    _add_json_flag(conform_p)

    expr_p = sub.add_parser(
        "expr",
        help="flux-expr/1: evaluate an expression, print an attribute model",
        description="Parse, check and evaluate one flux-expr/1 expression, or print the model.",
    )
    expr_sub = expr_p.add_subparsers(dest="expr_command", title="commands", metavar="<command>")
    eval_p = expr_sub.add_parser(
        "eval",
        help="parse, check and evaluate one expression",
        description="Evaluate one expression against a subject view supplied as JSON.",
        # `tenure_months`, the spelling this example shipped with, is not an
        # attribute of any bundle in this repository: the one worked example
        # the expression language ships exited 1 against the project's own
        # example bundle. The attribute is `tenureMonths`. The "Model your
        # customers" how-to (docs/how-to/model-customers.md) has a worked
        # `expr eval` call against a bundle that exists.
    )
    eval_p.add_argument("source", help="the expression source")
    eval_p.add_argument("--bundle", required=True, help=_BUNDLE_HELP)
    eval_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    eval_p.add_argument("--values", required=True, help="a subject view as JSON")
    _add_json_flag(eval_p)
    model_p = expr_sub.add_parser(
        "model",
        help="print the attrmodel/1 document for a bundle",
        description="Print the attribute model an expression in this bundle is checked against.",
    )
    model_p.add_argument("--bundle", required=True, help=_BUNDLE_HELP)
    model_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    _add_json_flag(model_p)

    describe_p = sub.add_parser(
        "describe",
        help="self-describe this installation",
        description=(
            "Report this installation's identity, engine capabilities and full command "
            "tree, so a consumer can render UI without lagging the CLI."
        ),
    )
    describe_p.add_argument(
        "--self",
        dest="self_",
        action="store_true",
        help="describe this installation (currently the only mode)",
    )
    _add_json_flag(describe_p)

    plugins_p = sub.add_parser(
        "plugins",
        help="list installed vertical packs and sinks",
        description=(
            "List the vertical packs and the output sinks available in this environment, "
            "with the extra each sink needs and whether it is installed. Nothing is "
            "imported to find them."
        ),
    )
    _add_json_flag(plugins_p)

    explain_p = sub.add_parser(
        "explain",
        help="print the catalog entry for one FLX-nnnn code, or list them all",
        description=(
            "Print the title and operator-facing explanation of one FLX-nnnn code. "
            "With --list, print every registered code instead, so the catalog is "
            "discoverable without grepping the source."
        ),
    )
    explain_p.add_argument(
        "code",
        nargs="?",
        default=None,
        help="the FLX-nnnn code to explain (case-insensitive); omit it with --list",
    )
    explain_p.add_argument(
        "--list",
        dest="list_codes",
        action="store_true",
        help="print every registered code as `code  [family]  title`",
    )
    _add_json_flag(explain_p)

    # The `stage` discriminator, declared once per subcommand and reached by
    # BOTH the success path and `_run_command`'s exception arms. Those arms
    # used to fall back to `args.command` -- the top-level verb -- so a
    # failing `accuracy register` reported `stage: "accuracy"` while the
    # same command reported `stage: "accuracy-register"` when it worked, and
    # a consumer routing on `stage` could not tell `register` from `score`
    # at precisely the moment routing matters.
    for subparser, stage_name in (
        (init_p, "init"),
        (validate_p, "validate"),
        (plan_p, "plan"),
        (apply_p, "apply"),
        (run_p, "run"),
        (verify_p, "verify"),
        (ship_p, "ship"),
        (replay_p, "replay"),
        (calibrate_p, "calibrate"),
        (reg_p, "accuracy-register"),
        (score_p, "accuracy-score"),
        (record_p, "accuracy-record"),
        (decide_p, "gate-decide"),
        (conform_p, "gate-conform"),
        (eval_p, "expr-eval"),
        (model_p, "expr-model"),
        (describe_p, "describe"),
        (plugins_p, "plugins"),
        (explain_p, "explain"),
    ):
        subparser.set_defaults(stage_name=stage_name)

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
    "init": (
        Example("fluxsim init demo", "write a project into a new directory"),
        Example("fluxsim init .", "or into the current directory, if it is empty"),
    ),
    "validate": (Example("fluxsim validate ./bundle", "check a bundle; nothing is written"),),
    "plan": (
        Example("fluxsim plan ./bundle", "record what a run would do"),
        Example("fluxsim plan ./bundle --state-dir ./.flux", "keep the run state somewhere else"),
    ),
    "apply": (
        Example(
            "fluxsim apply ./bundle --run-id de72fa4b22711c52",
            "apply the plan recorded under that id (`plan` prints it)",
        ),
    ),
    "run": (
        Example(
            "fluxsim run ./bundle --verify --ship -o ./out",
            "plan, apply, verify and ship in one command",
        ),
        Example("fluxsim run ./bundle", "plan and apply only"),
        Example(
            "fluxsim run ./bundle --sink parquet:./out/parquet",
            "plan, apply and write every stream as Parquet",
        ),
        Example(
            "fluxsim run ./bundle --sink adbc:duckdb --sink-opt uri=./sim.duckdb",
            "plan, apply and load every stream into a DuckDB table",
        ),
    ),
    "verify": (
        Example(
            "fluxsim verify ./bundle --run-id de72fa4b22711c52",
            "re-check an applied run on five independent legs",
        ),
    ),
    "ship": (
        Example(
            "fluxsim ship ./bundle --run-id de72fa4b22711c52 -o ./out",
            "pack the evidence archive",
        ),
        Example(
            "fluxsim ship ./bundle --run-id de72fa4b22711c52 -o ./out \\\n"
            "    --committed-slice payment_recovery_moment=file:///data/slice.ndjson",
            "a bundle that declares a fidelity block needs the slice it is measured against",
        ),
    ),
    "replay": (
        Example(
            "fluxsim replay ./bundle --run-id de72fa4b22711c52 \\\n"
            "    --assert-digest sha256:42d923fd6f4b8c0a1e7d59b2c8a4f36e0d15927acb3e8f6410d2b7c95e8a3f01",
            "re-execute the plan; refuse unless the run digest is the one asserted",
        ),
    ),
    "calibrate": (
        Example("fluxsim calibrate ./bundle", "print the declared scorecard; nothing is written"),
        Example(
            "fluxsim calibrate ./bundle --write --run-id de72fa4b22711c52",
            "record it in the run directory",
        ),
    ),
    "accuracy": (
        Example("fluxsim accuracy register --file prediction.json", "seal a prediction first"),
    ),
    "accuracy register": (
        Example("fluxsim accuracy register --file prediction.json", "seal a prediction"),
    ),
    "accuracy score": (
        Example(
            "fluxsim accuracy score --prediction-id p-1 --readout readout.json",
            "score it once the outcome is known",
        ),
    ),
    "accuracy record": (
        Example(
            "fluxsim accuracy record --registered-at 2026-01-01T00:00:00Z --out accuracy.json",
            "publish the outcomes-only record",
        ),
    ),
    "gate": (Example("fluxsim gate conform", "run the published conformance vectors"),),
    "gate decide": (
        Example("fluxsim gate decide --request request.json", "exit 0 allowed, 3 denied"),
    ),
    "gate conform": (Example("fluxsim gate conform", "run the published conformance vectors"),),
    "expr": (Example("fluxsim expr model --bundle ./bundle", "every attribute in scope"),),
    "expr eval": (
        Example(
            'fluxsim expr eval "tenureMonths > 12" --bundle ./bundle \\\n'
            "    --values '{\"tenureMonths\": 24}'",
            "check an expression and evaluate it against one subject",
        ),
    ),
    "expr model": (Example("fluxsim expr model --bundle ./bundle", "every attribute in scope"),),
    "describe": (
        Example("fluxsim describe --self --json", "identity, capabilities and the command tree"),
    ),
    "plugins": (
        Example("fluxsim plugins", "list the vertical packs and the sinks"),
        Example("fluxsim plugins --json", "the same, as JSON"),
    ),
    "explain": (
        Example("fluxsim explain FLX-1001", "print one code's explanation"),
        Example("fluxsim explain --list", "list every code"),
    ),
}


def _children(parser: argparse.ArgumentParser) -> Dict[str, argparse.ArgumentParser]:
    """The subparsers of *parser*, by name; empty when it has none."""
    found: Dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:  # noqa: SLF001 - argparse introspection idiom
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            found.update(action.choices)
    return found


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


# ---------------------------------------------------------------------------
# Six-step loop
# ---------------------------------------------------------------------------


def _finish(args: argparse.Namespace, result: Any) -> int:
    """Print *result*, adding the next-step lines a person at a terminal gets."""
    steps: List[str] = []
    if result.success and not args.json:
        from flux_engine.cli._hints import next_steps

        steps = next_steps(result.stage, args, result.artifacts)
    return _print_result(result, as_json=args.json, next_steps=steps)


def _cmd_init(args: argparse.Namespace) -> int:
    from flux_engine.cli._init import run_init

    return _print_result(run_init(args.directory), as_json=args.json)


def _cmd_validate(args: argparse.Namespace) -> int:
    from flux_engine.stages.validate import validate

    return _finish(args, validate(args.bundle))


def _cmd_plan(args: argparse.Namespace) -> int:
    from flux_engine.stages.plan import plan

    with stage_progress(f"planning {args.bundle}", as_json=args.json):
        result = plan(
            args.bundle,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            zone=args.zone,
            jobs=args.jobs,
        )
    return _finish(args, result)


def _cmd_apply(args: argparse.Namespace) -> int:
    from flux_engine.stages.apply import apply

    with stage_progress(f"applying {args.bundle}", as_json=args.json):
        result = apply(
            args.bundle,
            run_id=args.run_id,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            zone=args.zone,
            jobs=args.jobs,
        )
    return _finish(args, result)


def _cmd_run(args: argparse.Namespace) -> int:
    from flux_engine.stages.run import run

    # No early return: `stages.run.run` already refuses this case with the
    # right StageResult (exit 2, the same message), and returning here
    # instead printed a bare stderr line and NO --json envelope at all --
    # the one shape this CLI promises every command emits.
    signing_key = _read_signing_key(args)
    with stage_progress(f"running {args.bundle}", as_json=args.json):
        result = run(
            args.bundle,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            zone=args.zone,
            verify=args.verify,
            ship=args.ship,
            out_dir=args.out_dir,
            committed_slice=_kv_pairs(args.committed_slice),
            update_golden=args.update_golden,
            reason=args.reason,
            allow_declared_baseline_drift=args.allow_declared_baseline_drift,
            signing_key=signing_key,
            trust_key=_read_trust_key(args),
            sink=args.sink,
            sink_options=_kv_pairs(args.sink_opt),
            jobs=args.jobs,
        )
    return _finish(args, result)


def _cmd_verify(args: argparse.Namespace) -> int:
    from flux_engine.stages.verify import verify

    with stage_progress(f"verifying {args.bundle}", as_json=args.json):
        result = verify(
            args.bundle,
            run_id=args.run_id,
            state_dir=args.state_dir,
            update_golden=args.update_golden,
            reason=args.reason,
            committed_slice=_kv_pairs(args.committed_slice),
            vectors_path=args.vectors,
            allow_declared_baseline_drift=args.allow_declared_baseline_drift,
            signing_key=_read_signing_key(args),
            trust_key=_read_trust_key(args),
        )
    return _finish(args, result)


def _cmd_ship(args: argparse.Namespace) -> int:
    from flux_engine.stages.ship import ship

    with stage_progress(f"shipping {args.bundle}", as_json=args.json):
        result = ship(
            args.bundle,
            run_id=args.run_id,
            out_dir=args.out_dir,
            state_dir=args.state_dir,
            committed_slice=_kv_pairs(args.committed_slice),
            signing_key=_read_signing_key(args),
            trust_key=_read_trust_key(args),
        )
    return _finish(args, result)


def _cmd_replay(args: argparse.Namespace) -> int:
    from flux_engine.stages.replay import replay

    with stage_progress(f"replaying {args.bundle}", as_json=args.json):
        result = replay(
            args.bundle,
            run_id=args.run_id,
            assert_digest=args.assert_digest,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            zone=args.zone,
            jobs=args.jobs,
        )
    return _print_result(result, as_json=args.json)


# ---------------------------------------------------------------------------
# calibrate / accuracy / gate / expr
# ---------------------------------------------------------------------------


def _scorecard_document(scorecard: Any) -> Dict[str, Any]:
    """A JSON-safe, camelCase view of a :class:`~flux_engine.api.calibration.Scorecard`.

    Not ``scorecard.__dict__``: a dataclass's internal attribute dict is an
    implementation detail (snake_case field names, ``Decimal``/``DigestRef``
    values that are not themselves JSON-safe), not a wire contract this CLI
    should let leak unreviewed.
    """
    return {
        "provenance": scorecard.provenance.value,
        "credibility": scorecard.credibility.value,
        "methodId": scorecard.method_id,
        "grade": str(scorecard.grade) if scorecard.grade is not None else None,
        "intervalCoverage": (
            str(scorecard.interval_coverage) if scorecard.interval_coverage is not None else None
        ),
        "backtestWindow": scorecard.backtest_window,
        "evidenceDigest": (
            scorecard.evidence_digest.value if scorecard.evidence_digest is not None else None
        ),
    }


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from flux_engine.api.stages import StageResult
    from flux_engine.calibrate.declared import DeclaredCalibrator
    from flux_engine.calibrate.provenance import write_calibration_provenance
    from flux_engine.seam.resolve import load_bundle

    loaded = load_bundle(args.bundle)
    playbacks = {
        doc_id: doc
        for doc_id, doc in loaded.flux_documents.items()
        if doc.get("kind") == "Playback"
    }
    if not playbacks:
        return _print_result(
            StageResult(
                stage="calibrate", exit_code=1, stderr=f"{args.bundle}: no Playback document"
            ),
            as_json=args.json,
        )
    playback_id = args.playback_id or sorted(playbacks)[0]
    if playback_id not in playbacks:
        return _print_result(
            StageResult(
                stage="calibrate",
                exit_code=1,
                stderr=f"{playback_id!r} is not a Playback in this bundle",
            ),
            as_json=args.json,
        )

    calibrator = DeclaredCalibrator(playback=playbacks[playback_id])
    _dials, scorecard = calibrator.calibrate({}, {})

    if args.dry_run:
        return _print_result(
            StageResult(
                stage="calibrate",
                exit_code=0,
                _artifacts={"playbackId": playback_id, "scorecard": _scorecard_document(scorecard)},
                stdout=(
                    # Not `key=value` for one half and an English phrase with a
                    # space in it for the other: one register per line. The JSON
                    # side carries the machine-readable spelling.
                    f"{playback_id}: credibility {scorecard.credibility.value}, "
                    f"{f'grade {scorecard.grade}' if scorecard.grade is not None else 'not graded'}"
                ),
            ),
            as_json=args.json,
        )

    if not args.run_id:
        return _print_result(
            StageResult(
                stage="calibrate", exit_code=2, stderr="calibrate --write requires --run-id"
            ),
            as_json=args.json,
        )
    from datetime import datetime, timezone

    from flux_engine.stages.layout import RunLayout

    world_ref = str(playbacks[playback_id].get("spec", {}).get("worldRef", playback_id))
    layout = RunLayout.for_state_dir(Path(args.state_dir or ".flux"), args.run_id).ensure()
    path = write_calibration_provenance(
        layout.run_dir / "calibration-provenance.json",
        world_ref=world_ref,
        dials={},
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return _print_result(
        StageResult(
            stage="calibrate", exit_code=0, _artifacts={"path": str(path)}, stdout=f"wrote {path}"
        ),
        as_json=args.json,
    )


def _require_rfc3339(value: str, *, flag: str) -> str:
    """Return *value* when it is an RFC 3339 instant, else raise ``FLX-7005``.

    An unchecked ``--registered-at`` was stamped into the published record
    verbatim, so a bare date -- or any string at all -- became the
    registration window an auditor reads. The instant is parsed here and
    the caller's own spelling is what travels, because that spelling is
    what the sealed predictions carry.
    """
    from flux_engine.accuracy.records import FLX_7005
    from flux_engine.core.clock import parse_rfc3339_ms
    from flux_engine.core.errors import FluxError

    try:
        parse_rfc3339_ms(value)
    except (FluxError, ValueError) as error:
        raise FluxError(
            FLX_7005,
            f"{flag}={value!r} is not an RFC 3339 instant with an offset "
            "(e.g. 2026-01-01T00:00:00Z)",
        ) from error
    return value


def _accuracy_document(
    path: str, *, what: str, fields: "Sequence[Any]", flag: str
) -> Dict[str, Any]:
    """Read one caller-supplied accuracy document, or raise the right code.

    Every problem the document has is reported in one message -- each
    missing field, each field of the wrong type, and the legal values of
    every closed set. What reached the terminal before this was one raw
    ``KeyError`` per run-and-crash cycle, and a ``ValueError`` that
    withheld the three values it had just checked against.

    A file that is not there is ``FLX-7006``, not ``FLX-7005``: it has no
    shape to be wrong about, and the code and the detail must not
    contradict each other in one line. The refusal names the flag that
    supplied the path, the way ``FLX-2109`` names the next command.
    """
    from flux_engine.accuracy.records import FLX_7005, FLX_7006
    from flux_engine.cli._documents import DocumentProblems, MissingDocument, read_json_object
    from flux_engine.core.errors import FluxError

    try:
        return read_json_object(path, what=what, fields=fields)
    except MissingDocument:
        raise FluxError(
            FLX_7006, f"no {what} at {path}; {flag} names the JSON document to read"
        ) from None
    except DocumentProblems as problems:
        raise FluxError(FLX_7005, str(problems)) from None


def _cmd_accuracy_register(args: argparse.Namespace) -> int:
    from decimal import Decimal

    from flux_engine.accuracy.store import AccuracyStore
    from flux_engine.api.accuracy import PredictionRecord
    from flux_engine.api.stages import StageResult
    from flux_engine.cli._documents import PREDICTION_FIELDS
    from flux_engine.core.digest import DigestDomain, DigestRef

    document = _accuracy_document(
        args.file, what="PredictionRecord", fields=PREDICTION_FIELDS, flag="--file"
    )
    prediction = PredictionRecord(
        prediction_id=document["prediction_id"],
        run_digest=DigestRef(
            value=document["run_digest"], domain=DigestDomain.JCS_DOCUMENT, subject="runDigest"
        ),
        metric_id=document["metric_id"],
        statement=document["statement"],
        predicted=Decimal(str(document["predicted"])),
        horizon_end=document["horizon_end"],
        registered_at=document["registered_at"],
        interval_low=Decimal(str(document["interval_low"])) if "interval_low" in document else None,
        interval_high=(
            Decimal(str(document["interval_high"])) if "interval_high" in document else None
        ),
    )
    record_path, digest = AccuracyStore.for_state_dir(args.state_dir).register(prediction, document)
    return _print_result(
        StageResult(
            stage="accuracy-register",
            exit_code=0,
            _artifacts={
                "predictionId": prediction.prediction_id,
                "sealDigest": digest.value,
                "path": str(record_path),
            },
            stdout=f"registered {prediction.prediction_id} ({digest.value})",
        ),
        as_json=args.json,
    )


def _cmd_accuracy_score(args: argparse.Namespace) -> int:
    from decimal import Decimal

    from flux_engine.accuracy.records import bind_readout, score
    from flux_engine.accuracy.store import AccuracyStore
    from flux_engine.api.accuracy import ObservedReadout, PredictionRecord
    from flux_engine.api.claims import ClaimBasis
    from flux_engine.api.stages import StageResult
    from flux_engine.cli._documents import readout_fields
    from flux_engine.core.digest import DigestDomain, DigestRef

    store = AccuracyStore.for_state_dir(args.state_dir)
    document = store.read_prediction(args.prediction_id)
    if document is None:
        return _print_result(
            StageResult(
                stage="accuracy-score",
                exit_code=1,
                stderr=f"no registered prediction {args.prediction_id!r}",
            ),
            as_json=args.json,
        )
    prediction = PredictionRecord(
        prediction_id=document["prediction_id"],
        run_digest=DigestRef(
            value=document["run_digest"], domain=DigestDomain.JCS_DOCUMENT, subject="runDigest"
        ),
        metric_id=document["metric_id"],
        statement=document["statement"],
        predicted=Decimal(str(document["predicted"])),
        horizon_end=document["horizon_end"],
        registered_at=document["registered_at"],
        # Presence, not truthiness: the store keeps the submitted document
        # verbatim and a decimal field may be the JSON number 0, which is
        # falsy. Mirrors `accuracy register`, which reads the same keys.
        interval_low=(
            Decimal(str(document["interval_low"])) if "interval_low" in document else None
        ),
        interval_high=(
            Decimal(str(document["interval_high"])) if "interval_high" in document else None
        ),
    )
    readout_document = _accuracy_document(
        args.readout, what="ObservedReadout", fields=readout_fields(), flag="--readout"
    )
    readout = ObservedReadout(
        prediction_id=readout_document["prediction_id"],
        metric_id=readout_document["metric_id"],
        observed=Decimal(str(readout_document["observed"])),
        sample_size=int(readout_document["sample_size"]),
        basis=ClaimBasis(readout_document["basis"]),
        observed_at=readout_document["observed_at"],
    )
    bind_readout(prediction, readout)
    verdict = score(prediction, readout)
    verdict_path = store.write_verdict(verdict)
    return _print_result(
        StageResult(
            stage="accuracy-score",
            exit_code=0,
            _artifacts={
                "predictionId": verdict.prediction_id,
                "metricId": verdict.metric_id,
                "outcome": verdict.outcome.value,
                "basis": verdict.basis.value,
                "path": str(verdict_path),
            },
            stdout=f"{verdict.prediction_id}: {verdict.outcome.value}",
        ),
        as_json=args.json,
    )


def _cmd_accuracy_record(args: argparse.Namespace) -> int:
    from flux_engine.accuracy.store import AccuracyStore
    from flux_engine.api.stages import StageResult

    store = AccuracyStore.for_state_dir(args.state_dir)
    predictions_dir = store.predictions_dir
    if not predictions_dir.is_dir():
        return _print_result(
            StageResult(
                stage="accuracy-record",
                exit_code=1,
                stderr=f"no registered predictions under {predictions_dir}",
            ),
            as_json=args.json,
        )

    from datetime import datetime, timezone
    from decimal import Decimal

    from flux_engine.accuracy.accuracy_record import write_accuracy_record
    from flux_engine.accuracy.records import FLX_7004
    from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict, PredictionRecord
    from flux_engine.api.claims import ClaimBasis
    from flux_engine.core.digest import DigestDomain, DigestRef
    from flux_engine.core.errors import FluxError

    registered_at = _require_rfc3339(args.registered_at, flag="--registered-at")

    predictions = []
    verdicts = []
    # THE filter, now owned by the store (`documents_registered_at`).
    # Without it this command globbed every prediction ever registered,
    # stamped the caller's `--registered-at` on the result verbatim, and
    # published a record that made a false statement about which
    # predictions it covers.
    for document in store.documents_registered_at(registered_at):
        predictions.append(
            PredictionRecord(
                prediction_id=document["prediction_id"],
                run_digest=DigestRef(
                    value=document["run_digest"],
                    domain=DigestDomain.JCS_DOCUMENT,
                    subject="runDigest",
                ),
                metric_id=document["metric_id"],
                statement=document["statement"],
                predicted=Decimal(str(document["predicted"])),
                horizon_end=document["horizon_end"],
                registered_at=document["registered_at"],
                # The interval is part of what register sealed. Rebuilding
                # the prediction without it published a digest that no seal
                # matches.
                interval_low=(
                    Decimal(str(document["interval_low"])) if "interval_low" in document else None
                ),
                interval_high=(
                    Decimal(str(document["interval_high"])) if "interval_high" in document else None
                ),
            )
        )
        verdict_doc = store.read_verdict(document["prediction_id"])
        if verdict_doc is not None:
            verdicts.append(
                AccuracyVerdict(
                    prediction_id=verdict_doc["prediction_id"],
                    metric_id=verdict_doc["metric_id"],
                    outcome=AccuracyOutcome(verdict_doc["outcome"]),
                    basis=ClaimBasis(verdict_doc["basis"]),
                )
            )

    if not predictions:
        # An empty-but-stamped record is the false claim this refusal
        # exists to prevent: it would name a registration window and cover
        # nothing that was registered in it.
        raise FluxError(
            FLX_7004,
            f"no prediction under {predictions_dir} is registered at {registered_at!r}",
        )

    out_path = write_accuracy_record(
        args.out,
        registered_at=registered_at,
        targets=[p.metric_id for p in predictions],
        predictions=predictions,
        verdicts=verdicts,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return _print_result(
        StageResult(
            stage="accuracy-record",
            exit_code=0,
            _artifacts={
                "path": str(out_path),
                "registeredAt": registered_at,
                "predictions": len(predictions),
            },
            stdout=f"wrote {out_path}",
        ),
        as_json=args.json,
    )


#: Distinct from the usage-error/run-failure convention (2/1/0): a
#: `gate decide` decision is a *policy answer*, not a command outcome --
#: "the policy said no" and "this command could not run" are different
#: facts, and conflating them under exit 1 (S3 note) would make a caller
#: unable to tell a denial from a crash without reading stderr. `fluxsim gate
#: decide` reserves exit code 3 for a well-formed, successfully-computed
#: denial; 1 stays for a command that could not compute an answer at all
#: (a malformed request document, a policy shape error).
_GATE_DECIDE_DENIED_EXIT_CODE = 3


def _cmd_gate_decide(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.cli._documents import (
        GATE_REQUEST_FIELDS,
        DocumentProblems,
        read_json_object,
    )
    from flux_engine.governance.gate import decide

    try:
        document = read_json_object(
            args.request, what="the gate request", fields=GATE_REQUEST_FIELDS
        )
    except DocumentProblems as problems:
        # No FLX code yet: the governance band's codes are owned by
        # `flux_engine.governance.gate`, which this CLI does not write. The
        # message is the fix that matters -- every problem at once, and the
        # required field list is in `--request`'s own --help.
        return _print_result(
            StageResult(stage="gate-decide", exit_code=1, stderr=str(problems)),
            as_json=args.json,
        )
    decision = decide(dict(document["policy"]), dict(document["request"]), document.get("skill"))
    allowed = bool(decision.get("allow"))
    return _print_result(
        StageResult(
            stage="gate-decide",
            exit_code=0 if allowed else _GATE_DECIDE_DENIED_EXIT_CODE,
            _artifacts=dict(decision),
            stdout=json.dumps(decision, indent=2, sort_keys=True),
        ),
        as_json=args.json,
    )


def _cmd_gate_conform(args: argparse.Namespace) -> int:
    from pathlib import Path

    from flux_engine.api.stages import StageResult
    from flux_engine.governance.gate import run_conformance_vectors
    from flux_engine.spec import default_vectors_path

    vectors_path = Path(args.vectors) if args.vectors else default_vectors_path()
    total = len(json.loads(vectors_path.read_text())["vectors"])
    failures, messages = run_conformance_vectors(vectors_path)

    if failures:
        return _print_result(
            StageResult(
                stage="gate-conform",
                exit_code=1,
                _artifacts={"total": total, "failures": failures, "messages": messages},
                stderr="; ".join(
                    [
                        *(f"FAIL: {m}" for m in messages),
                        f"{total - failures}/{total} RFC-04 conformance vectors passed",
                    ]
                ),
            ),
            as_json=args.json,
        )

    return _print_result(
        StageResult(
            stage="gate-conform",
            exit_code=0,
            _artifacts={"total": total, "failures": 0, "messages": []},
            stdout=f"{total}/{total} RFC-04 conformance vectors passed",
        ),
        as_json=args.json,
    )


def _cmd_expr_eval(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.expr import build_attribute_model, check, compile_expr, parse
    from flux_engine.expr.compile import make_view
    from flux_engine.expr.errors import UnknownPathError, with_known_attributes
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.resolve import load_bundle
    from flux_engine.verticals import get_pack

    loaded = load_bundle(args.bundle)
    cast = resolve_cast(loaded.flux_documents, simulation_id=args.simulation_id)
    world = cast.world
    pack = get_pack(str(world.get("spec", {}).get("vertical", "")))
    model = build_attribute_model(
        type("BundleView", (), {"flux_docs": loaded.flux_documents})(),
        pack,
        simulation_id=cast.simulation_id,
    )
    # --values first: a JSON typo in it was being reported as an error in
    # the EXPRESSION, because the expression was checked before the values
    # were parsed at all.
    try:
        values = json.loads(args.values)
    except json.JSONDecodeError as error:
        return _print_result(
            StageResult(
                stage="expr-eval",
                exit_code=1,
                stderr=(
                    f"--values is not valid JSON (line {error.lineno}, "
                    f"column {error.colno}): a subject view is a JSON object, "
                    "e.g. '{\"tenureMonths\": 24}'"
                ),
            ),
            as_json=args.json,
        )
    node = parse(args.source)
    # The engine holds the whole attribute model in memory here; an
    # FLX-6004 that does not print it is withholding the one list that
    # fixes the error in a keystroke.
    try:
        check(node, model.subject)
    except UnknownPathError as error:
        raise with_known_attributes(error, model.subject.aliases) from None
    predicate = compile_expr(node, model.subject)
    view = make_view(model.subject, values)
    result = predicate(view)
    return _print_result(
        StageResult(
            stage="expr-eval",
            exit_code=0,
            _artifacts={"result": result},
            stdout=json.dumps(result),
        ),
        as_json=args.json,
    )


def _cmd_expr_model(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.expr import build_attribute_model
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.resolve import load_bundle
    from flux_engine.verticals import get_pack

    loaded = load_bundle(args.bundle)
    cast = resolve_cast(loaded.flux_documents, simulation_id=args.simulation_id)
    world = cast.world
    pack = get_pack(str(world.get("spec", {}).get("vertical", "")))
    model = build_attribute_model(
        type("BundleView", (), {"flux_docs": loaded.flux_documents})(),
        pack,
        simulation_id=cast.simulation_id,
    )
    document = model.to_document()
    return _print_result(
        StageResult(
            stage="expr-model",
            exit_code=0,
            _artifacts=document,
            stdout=json.dumps(document, indent=2, sort_keys=True),
        ),
        as_json=args.json,
    )


def _describe_human_lines(result: Dict[str, Any]) -> str:
    """Render :func:`~flux_engine.describe.self_describe`'s result for a terminal.

    Hand-rendered, one line per top-level key -- never ``f"{key}: {value}"``
    over the raw dict, which for ``capabilities`` (a ``dict[str, bool]``)
    printed a Python ``repr`` (``{'agentic': False, ...}``) and for
    ``commands`` (the full argparse tree, M8) would print an unreadable
    wall of nested dicts. camelCase keys throughout, matching every other
    JSON surface this CLI emits (M7/M4).
    """
    capabilities = result.get("capabilities", {})
    enabled = sorted(name for name, on in capabilities.items() if on)
    # The absent capabilities are the whole point of the README's "What it
    # is not" section, and printing only the true ones kept the engine's
    # say-what-is-absent posture for machines and dropped it for people: a
    # human could not see that `agentic`, `calibrate_abc` and
    # `lineage_transport` are off without re-running with --json.
    absent = sorted(name for name, on in capabilities.items() if not on)
    lines = [
        f"component: {result.get('component')}",
        f"version: {result.get('version')}",
        f"pythonVersion: {result.get('python_version')}",
        f"specSource: {result.get('spec_source')}",
        f"specVersion: {result.get('spec_version')}",
        f"capabilities: {', '.join(enabled) if enabled else '(none enabled)'}",
    ]
    if absent:
        lines.append(f"not available: {', '.join(absent)}")
    commands = result.get("commands", {})
    subcommands = sorted(commands.get("subcommands", {})) if isinstance(commands, dict) else []
    if subcommands:
        lines.append(f"commands: {', '.join(subcommands)} (pass --json for the full tree)")
    return "\n".join(lines)


def _cmd_describe(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.describe import self_describe

    result = self_describe()
    return _print_result(
        StageResult(
            stage="describe", exit_code=0, _artifacts=result, stdout=_describe_human_lines(result)
        ),
        as_json=args.json,
    )


def _cmd_plugins(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.sinks.registry import available
    from flux_engine.verticals import pack_names

    names = list(pack_names())
    sinks = available()
    lines = ["vertical packs:"]
    lines.extend(f"  {name}" for name in names)
    lines.append("sinks (fluxsim run --sink NAME:TARGET):")
    width = max((len(sink.name) for sink in sinks), default=0)
    for sink in sinks:
        if sink.extra:
            state = "installed" if sink.installed else "not installed"
            needs = f"needs flux-sim-engine[{sink.extra}] ({state})"
        else:
            needs = "shadowed by the built-in of that name" if sink.shadowed else sink.value
        lines.append(f"  {sink.name.ljust(width)}  {sink.source}  {needs}")
    return _print_result(
        StageResult(
            stage="plugins",
            exit_code=0,
            _artifacts={
                "verticalPacks": names,
                "sinks": [sink.as_document() for sink in sinks],
            },
            stdout="\n".join(lines),
        ),
        as_json=args.json,
    )


def _cmd_explain(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.core.errors import (
        catalog,
        ensure_core_codes_loaded,
        explain,
        is_registered,
    )

    ensure_core_codes_loaded()
    if args.list_codes:
        entries = catalog()
        return _print_result(
            StageResult(
                stage="explain",
                exit_code=0,
                _artifacts={
                    "codes": [
                        {"code": entry.code, "family": entry.category, "title": entry.title}
                        for entry in entries
                    ]
                },
                stdout="\n".join(
                    f"{entry.code}  [{entry.category}]  {entry.title}" for entry in entries
                ),
            ),
            as_json=args.json,
        )
    if args.code is None:
        return _print_result(
            StageResult(
                stage="explain",
                exit_code=2,
                stderr=(
                    "explain takes an FLX-nnnn code, or --list for every registered one; "
                    "for example `fluxsim explain FLX-3105`"
                ),
            ),
            as_json=args.json,
        )
    # Upper-cased and stripped before the lookup: `flx-3105` is the same
    # code as `FLX-3105`, and answering "not a registered FLX code" to a
    # code the catalog holds sent readers hunting for a code that exists.
    code = args.code.strip().upper()
    if not is_registered(code):
        return _print_result(
            StageResult(
                stage="explain",
                exit_code=1,
                # `requestedCode`, not `code`: the code the user asked about
                # is this command's INPUT. Reporting it as `code` made a
                # machine consumer record the unregistered code as the reason
                # that same code could not be explained.
                _artifacts={"requestedCode": args.code},
                stderr=(
                    f"{args.code} is not a registered FLX code; "
                    "see `fluxsim explain --list` for every one that is"
                ),
            ),
            as_json=args.json,
        )
    return _print_result(
        StageResult(
            stage="explain",
            exit_code=0,
            _artifacts={"code": code},
            stdout=f"{explain(code)}\n\n{error_url(code)}",
        ),
        as_json=args.json,
    )


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
        "calibrate": _cmd_calibrate,
        "describe": _cmd_describe,
        "plugins": _cmd_plugins,
        "explain": _cmd_explain,
    }
    if getattr(args, "jobs", "absent") is None:
        args.jobs = _jobs_from_environment(_children(parser)[args.command])
    if args.command in dispatch:
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
