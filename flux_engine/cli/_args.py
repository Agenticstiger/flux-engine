# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Argument types, shared flags and help strings, used by more than one command."""

from __future__ import annotations

import argparse
import os
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Dict, Optional, Sequence

from flux_engine.core.target_label import target_label


def _kv_pair_type(item: str) -> str:
    """``argparse`` ``type=`` callable for ``--committed-slice``: validates the
    ``NAME=VALUE`` shape at parse time, so a malformed value is argparse's own
    usage error (exit 2, ``parser.error``'s own formatting) rather than a
    ``SystemExit`` raised by hand deep inside a command function -- the same
    exit code the :mod:`flux_engine.cli` docstring already promises for every
    other usage error.

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
        raise argparse.ArgumentTypeError(
            f"expected KEY=VALUE, got {target_label('sink-opt', item)!r}"
        )
    return item


def _kv_pairs(items: Optional[Sequence[str]]) -> Dict[str, str]:
    """Fold already-validated ``NAME=VALUE`` pairs (see :func:`_kv_pair_type`) into a dict."""
    result: Dict[str, str] = {}
    for item in items or ():
        name, _, value = item.partition("=")
        result[name] = value
    return result


def _add_state_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default=None, help="run-state directory (default: ./.flux)")


def _add_profile(
    parser: argparse.ArgumentParser,
    *,
    fresh_plan_only: bool = False,
    scores: bool = False,
    run: bool = False,
) -> None:
    if scores:
        help_text = (
            "the profile document this run's plan pins (docs/spec/PROFILE-SPEC.md): every "
            "declared fidelity property with no --committed-slice is checked against it, and "
            "the run is scored per property, table, dimension and overall "
            "(docs/spec/SCORECARD-SPEC.md); refused when its digest is not the pinned one, "
            "and when the run pins none"
        )
    else:
        help_text = (
            "the profile document the Simulation pins under "
            "extensions['com.agentics.flux/1'].profile (docs/spec/PROFILE-SPEC.md); refused "
            "when its digest is not the pinned one, and when the Simulation pins none"
            + (
                "; with --run-id, read only with --sink, so a sink's external audience can be "
                "graded from it, and refused without"
                if fresh_plan_only
                else ""
            )
            + (
                "; --verify and --ship score the run against it (docs/spec/SCORECARD-SPEC.md)"
                if run
                else ""
            )
        )
    parser.add_argument("--profile", default=None, metavar="PATH", help=help_text)


def _sample_fraction_type(text: str) -> Fraction:
    """``argparse`` ``type=`` callable for ``--sample``: a decimal in (0, 1], read exactly."""
    from flux_stats.reading import digits_written_out
    from flux_stats.spelling import MOST_DIGITS

    try:
        number = Decimal(text.strip())
        # Finite and of the length SEAM-FIDELITY-SPEC §2 bounds a number to, first: Fraction
        # would spell out every digit of 1e-99999999, and form 10**99999999 for 1e99999999,
        # before the range check refused it.
        bounded = number.is_finite() and digits_written_out(number) <= MOST_DIGITS
        exact = Fraction(number) if bounded else None
    except (InvalidOperation, ValueError, OverflowError):
        exact = None
    if exact is None or not 0 < exact <= 1:
        raise argparse.ArgumentTypeError(
            f"expected a decimal above 0 and at most 1, such as 0.1, got {text!r}"
        )
    return exact


def _whole_number_type(minimum: int) -> Any:
    """``argparse`` ``type=`` callable for a whole number not below *minimum*."""

    def parse(text: str) -> int:
        value = int(text) if text.strip().isdecimal() else minimum - 1
        if value < minimum:
            raise argparse.ArgumentTypeError(
                f"expected a whole number of at least {minimum}, got {text!r}"
            )
        return value

    return parse


def _memory_size_type(text: str) -> int:
    """``argparse`` ``type=`` callable for ``--max-query-memory``: a size such as ``2048MiB`` or ``4GiB``, in bytes.

    Only the shape is read here. Whether the size is one the engine lets a flag raise a ceiling to
    is the stage's refusal, with its own code (``FLX-3700``).
    """
    match = re.fullmatch(r"([1-9][0-9]{0,5})(MiB|GiB)", text.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"expected a size such as 2048MiB or 4GiB, got {text!r}")
    return int(match.group(1)) << (20 if match.group(2) == "MiB" else 30)


def _add_query_flags(parser: argparse.ArgumentParser) -> None:
    """``--held-out`` and the two ceiling flags of the commands that count a bundle's consumer queries."""
    parser.add_argument(
        "--held-out",
        default=None,
        metavar="FILE",
        help=(
            "the held-out profile: a profile document whose queries section holds the targets of "
            "the bundle's held-out consumer queries, which the pinned profile may not name; it is "
            "graded beside the pinned profile and read by nothing else"
        ),
    )
    parser.add_argument(
        "--max-query-seconds",
        default=None,
        type=_whole_number_type(1),
        metavar="N",
        help=(
            "raise the seconds one consumer query may take above the engine's 30, up to 600, for "
            "a bundle that declares no limits.seconds; a bundle's own value is kept and may only "
            "lower the ceiling"
        ),
    )
    parser.add_argument(
        "--max-query-memory",
        default=None,
        type=_memory_size_type,
        metavar="SIZE",
        help=(
            "raise the memory DuckDB may use for the consumer queries above the engine's 1GiB, "
            "up to 16GiB, such as 4GiB, for a bundle that declares no limits.memory; a bundle's "
            "own value is kept and may only lower the ceiling"
        ),
    )


#: The environment variable that sets ``--jobs`` when the flag is not given.
JOBS_ENV_VAR = "FLUX_JOBS"


def _jobs_type(text: str) -> int:
    """``argparse`` ``type=`` callable for ``--jobs`` (and ``FLUX_JOBS``): a count or ``auto``.

    ``auto`` is one job per CPU this process may run on.
    """
    value = text.strip().lower()
    if value == "auto":
        from flux_engine.stages._prewarm import cpu_jobs

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


def _scale_type(text: str) -> str:
    """``argparse`` ``type=`` callable for ``--scale``: a plain decimal above zero, bounded (FLX-3405)."""
    from flux_engine.core.errors import FluxError
    from flux_engine.core.sizing import parse_scale

    try:
        parse_scale(text)
    except FluxError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return text


def _shard_type(text: str) -> str:
    """``argparse`` ``type=`` callable for ``--shard``: ``I/N`` with 1 <= I <= N (FLX-3404)."""
    from flux_engine.core.errors import FluxError
    from flux_engine.core.sizing import parse_shard

    try:
        parse_shard(text)
    except FluxError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return text


def _add_sink(parser: argparse.ArgumentParser, *, when: str) -> None:
    """``--sink`` and ``--sink-opt``, as ``run`` and ``apply`` both take them; *when* says where it lands."""
    parser.add_argument(
        "--sink",
        default=None,
        action=_OnceAction,
        metavar="NAME:TARGET",
        help=(
            f"also write every stream to a sink, {when}: "
            "parquet:DIR or arrow-ipc:DIR (a directory, or an s3://, gs://, az:// or abfs:// "
            "prefix), adbc:DRIVER, iceberg:CATALOG/NAMESPACE or delta:DIR (a directory or an "
            "object store URL) or kafka:HOST:PORT[,HOST:PORT...] (`fluxsim plugins` lists them)"
        ),
    )
    parser.add_argument(
        "--sink-opt",
        action="append",
        type=_sink_opt_type,
        metavar="KEY=VALUE",
        help=(
            "a --sink setting, repeatable, for example row-group-size=100000 or "
            "uri=sim.duckdb; never a password: pass uri-env=VAR instead"
        ),
    )


#: ``--max-rows``' default and its ceiling: :data:`flux_engine.generator.periods.DEFAULT_PERIODIC_ROWS`
#: and :data:`flux_engine.core.sizing.MOST_ROWS`, written out so that building the parser imports
#: no generator code. ``tests/stages/test_periodic_rows_budget.py`` holds them to each other.
DEFAULT_MAX_ROWS = 50_000_000
MOST_MAX_ROWS = 10**10


def _max_rows_type(text: str) -> int:
    """``argparse`` ``type=`` callable for ``--max-rows``: a whole number from 1 to :data:`MOST_MAX_ROWS`."""
    value = _whole_number_type(1)(text)
    if value > MOST_MAX_ROWS:
        raise argparse.ArgumentTypeError(f"expected at most {MOST_MAX_ROWS}, got {text!r}")
    return value


def _add_max_rows(parser: argparse.ArgumentParser, *, fresh_plan_only: bool = False) -> None:
    """``--max-rows``: the periodic rows' budget (ADR 0027 section 5, the amendment of 2026-11-25)."""
    note = (
        "; read only when a fresh plan is taken, since the plan of --run-id states its rows"
        if fresh_plan_only
        else ""
    )
    parser.add_argument(
        "--max-rows",
        type=_max_rows_type,
        default=None,
        metavar="N",
        help=(
            "the rows each periodic emit may be estimated to derive when the plan is made, "
            f"checked before its periods are laid out (default {DEFAULT_MAX_ROWS}, at most "
            f"{MOST_MAX_ROWS}). A plan above it is refused (FLX-3403); plan.json records another "
            "budget as maxRows" + note
        ),
    )


def _add_scale(parser: argparse.ArgumentParser, *, fresh_plan_only: bool = False) -> None:
    note = (
        "; read only when a fresh plan is taken, since the plan of --run-id records its own"
        if fresh_plan_only
        else ""
    )
    parser.add_argument(
        "--scale",
        default=None,
        type=_scale_type,
        metavar="F",
        help=(
            "multiply every emit's rows, the records bounds of its fidelity block and the "
            "span of every integer or decimal key column that declares a range, by F (above 0, "
            "at most 1000000); the plan records it and apply reads it back, and 1 changes "
            "nothing; a table built from the run's events is left as it is" + note
        ),
    )


#: What ``--allow-generator`` takes: an entry-point name, never a module path or a file. The same
#: pattern :data:`flux_engine.generators.registry` reads a name with, written out here so that
#: building the parser imports none of it.
_GENERATOR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")


def _generator_name_type(text: str) -> str:
    """``argparse`` ``type=`` callable for ``--allow-generator``: an entry-point name."""
    if _GENERATOR_NAME.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(
            "expected the name of a generator entry point: a letter, then letters, digits, "
            f"'_', '.' or '-', never a module path or a file; got {text!r}"
        )
    return text


def _add_allow_generator(parser: argparse.ArgumentParser, *, when: str) -> None:
    """``--allow-generator NAME``, repeatable, as every command that would import a plugin takes it."""
    parser.add_argument(
        "--allow-generator",
        action="append",
        default=None,
        type=_generator_name_type,
        metavar="NAME",
        help=(
            "allow the generator plugin of that entry-point name to be imported and run, "
            f"{when}; repeat it for each plugin the bundle declares under generators (without "
            "it the command is refused, FLX-3119, before anything is imported); a plugin is "
            "trusted code that runs with your rights, so name only plugins you have reviewed; "
            "no environment variable and no bundle key allows one"
        ),
    )


def _add_timings(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timings",
        action="store_true",
        help=(
            "print where the time went (load, compile, each emit, framing, attestation); the "
            "numbers are wall-clock seconds, reported and recorded nowhere in a run's files"
        ),
    )


def _add_strict_coverage(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--strict-coverage",
        action="store_true",
        help=(
            "refuse the run when a column falls back to a type default (an optional column "
            "nothing maps onto, a DATE or TIMESTAMP column with no declaration of its own), "
            "with a table of every such column (FLX-3304), or when the contract declares what "
            "the run does not generate, such as a check without generates or, on kernel "
            "0.1.0 or in a Simulation that does not declare copies: true, sharedColumns or "
            "sharedAttribute (FLX-3305), before any row is drawn; a "
            "Simulation can ask "
            "for the same with strictCoverage: true in its extensions; without either the plan "
            "names those columns and goes on, and with this flag the rows and every digest are "
            "the same either way"
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
    same ``args.json``, as in the comparable CLIs that accept a global
    flag after the verb.
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
