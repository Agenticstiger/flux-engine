# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""PractRand over the seed tree: the feeders, a runner and an output parser.

Importable (the tests use the feeders and the parser) and runnable::

    python -m tests.accuracy.streams.practrand feed --mode first-words --bytes 2^30 | RNG_test stdin64
    python -m tests.accuracy.streams.practrand run --mode interleave --bytes 2^30 \
        --rng-test /path/to/RNG_test --out practrand-interleave.json

Two feeders, both over sibling generator-row streams under one root:

* ``first-words``: the first raw 64-bit word of the stream at row ``0 .. N-1``, little-endian,
  ``N = bytes / 8``. This is the sequence a column of one-draw cells actually sees. It costs
  one stream derivation per word (about 22 microseconds), so 2^30 bytes is 2^27 derivations
  and the feeder is parallel: contiguous index chunks go to worker processes and are written
  strictly in index order, so the byte stream is identical for any worker count.
* ``interleave``: ``K = 256`` sibling streams, round-robin, one word from each, until the
  byte budget is met. This is what a run sees when many cells advance their streams together.

The seam bytes, simulation id, expose id and column are fixed constants, so the same seed
always gives the same bytes. Only the standard library, numpy and the engine are used, never
scipy, so the module imports in an environment without the accuracy dependency group.

PractRand itself (public domain) is not bundled: ``--rng-test`` or ``PRACTRAND_RNG_TEST``
names the ``RNG_test`` binary. The verdict is *pass iff no FAIL at any length*; ``unusual``
through ``VERY SUSPICIOUS`` results are logged in the summary, not judged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import re
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Sequence

import numpy as np
from numpy.random import PCG64, SeedSequence

from flux_engine.core.seedtree import SeedTreeRoot, derive_root, stream_entropy
from flux_engine.generator.paths import generator_column_path

__all__ = [
    "COLUMN",
    "DEFAULT_TIMEOUT_SECONDS",
    "EVALUATIONS",
    "EXPOSE_ID",
    "INTERLEAVE_STREAMS",
    "MODES",
    "RNG_TEST_ENV",
    "SEAM",
    "SIM_ID",
    "feed",
    "first_word",
    "first_words_bytes",
    "format_length",
    "interleave_bytes",
    "iter_first_words",
    "iter_interleave",
    "make_root",
    "parse_length",
    "parse_practrand_output",
    "row_path",
    "run_practrand",
    "verdict",
]

RNG_TEST_ENV = "PRACTRAND_RNG_TEST"
MODES = ("first-words", "interleave")

#: The seam bytes the feeders root their tree at: a fixed constant, not a real contract digest.
SEAM = hashlib.sha256(b"flux-accuracy/streams/practrand").digest()
SIM_ID = "accuracy"
EXPOSE_ID = "practrand"
COLUMN = "value"

#: How many sibling streams the interleave feeder round-robins over.
INTERLEAVE_STREAMS = 256

_CHUNK_ROWS = 1 << 15  # 256 KiB per chunk of first words
_INTERLEAVE_BLOCK = 1 << 12  # words per stream per interleave block (8 MiB per block)


# ---------------------------------------------------------------------------
# Paths and streams
# ---------------------------------------------------------------------------


def make_root(seed: int) -> SeedTreeRoot:
    """The feeders' root for *seed* (an int64)."""
    return derive_root(SEAM, seed)


def row_path(row: int) -> Sequence[object]:
    """The generator-row path of *row* (SEAM-FIDELITY-SPEC section 1)."""
    return generator_column_path(SIM_ID, EXPOSE_ID, row, COLUMN)


def _bit_generator(root: SeedTreeRoot, row: int) -> PCG64:
    # KERNEL-SPEC section 3: Generator(PCG64(SeedSequence(entropy))). DrawStream.next_raw is
    # PCG64.random_raw, so driving the bit generator directly yields the engine's own words.
    return PCG64(SeedSequence(stream_entropy(root, row_path(row))))


def first_word(root: SeedTreeRoot, row: int) -> int:
    """The first raw 64-bit word of the stream at generator row *row*."""
    return int(_bit_generator(root, row).random_raw())


# ---------------------------------------------------------------------------
# Feeders
# ---------------------------------------------------------------------------


def _first_words_chunk(seed: int, start: int, stop: int) -> bytes:
    root = make_root(seed)
    out = np.empty(stop - start, dtype="<u8")
    for index in range(start, stop):
        out[index - start] = _bit_generator(root, index).random_raw()
    return out.tobytes()


def first_words_bytes(seed: int, rows: int, *, start: int = 0) -> bytes:
    """The first words of rows ``start .. start+rows-1`` as little-endian bytes, in-process."""
    return _first_words_chunk(seed, start, start + rows)


def iter_first_words(
    seed: int,
    total_bytes: int,
    *,
    workers: int = 1,
    chunk_rows: int = _CHUNK_ROWS,
) -> Iterator[bytes]:
    """Yield the ``first-words`` byte stream in index order, *workers* processes wide.

    ``total_bytes`` is rounded down to a whole number of words. At most ``2 * workers``
    chunks are in flight, so memory stays bounded when the consumer is slower than the
    producers.
    """
    rows = total_bytes // 8
    spans = [(lo, min(lo + chunk_rows, rows)) for lo in range(0, rows, chunk_rows)]
    if workers <= 1:
        for lo, hi in spans:
            yield _first_words_chunk(seed, lo, hi)
        return
    context = multiprocessing.get_context("spawn")
    pool = context.Pool(processes=workers)
    try:
        pending: deque = deque()
        spans_iter = iter(spans)
        window = 2 * workers
        for lo, hi in spans_iter:
            pending.append(pool.apply_async(_first_words_chunk, (seed, lo, hi)))
            if len(pending) >= window:
                break
        while pending:
            yield pending.popleft().get()
            nxt = next(spans_iter, None)
            if nxt is not None:
                pending.append(pool.apply_async(_first_words_chunk, (seed, nxt[0], nxt[1])))
    finally:
        pool.terminate()
        pool.join()


def iter_interleave(
    seed: int,
    total_bytes: int,
    *,
    streams: int = INTERLEAVE_STREAMS,
    block: int = _INTERLEAVE_BLOCK,
) -> Iterator[bytes]:
    """Yield the ``interleave`` byte stream: *streams* sibling streams, round-robin.

    Word ``j`` of the output is word ``j // streams`` of stream (row) ``j % streams``.
    The stream is cut at exactly *total_bytes* (rounded down to a whole word).
    """
    words_left = total_bytes // 8
    root = make_root(seed)
    generators = [_bit_generator(root, row) for row in range(streams)]
    while words_left > 0:
        # (streams, block) block of each stream's next words; the transpose reads round-robin.
        matrix = np.stack([g.random_raw(block) for g in generators])
        flat = np.ascontiguousarray(matrix.T).reshape(-1).astype("<u8", copy=False)
        if flat.size > words_left:
            flat = flat[:words_left]
        words_left -= flat.size
        yield flat.tobytes()


def interleave_bytes(seed: int, total_bytes: int, *, streams: int = INTERLEAVE_STREAMS) -> bytes:
    """The ``interleave`` byte stream in memory (for tests; keep *total_bytes* small)."""
    return b"".join(iter_interleave(seed, total_bytes, streams=streams, block=64))


def feed(mode: str, seed: int, total_bytes: int, out: BinaryIO, *, workers: int = 1) -> int:
    """Write a feeder's bytes to *out*. Returns the number of bytes written."""
    if mode == "first-words":
        chunks = iter_first_words(seed, total_bytes, workers=workers)
    elif mode == "interleave":
        chunks = iter_interleave(seed, total_bytes)
    else:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    written = 0
    try:
        for chunk in chunks:
            out.write(chunk)
            written += len(chunk)
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()
    return written


# ---------------------------------------------------------------------------
# Lengths and the parser
# ---------------------------------------------------------------------------

_UNITS = (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10))
_UNIT_WORDS = {
    "byte": 1,
    "kilobyte": 1 << 10,
    "megabyte": 1 << 20,
    "gigabyte": 1 << 30,
    "terabyte": 1 << 40,
    "petabyte": 1 << 50,
}

#: PractRand's evaluation words, mildest first. ``FAIL`` is the only one that fails a run.
EVALUATIONS = (
    "unusual",
    "mildly suspicious",
    "suspicious",
    "very suspicious",
    "VERY SUSPICIOUS",
    "FAIL",
)


def parse_length(text: str) -> int:
    """A byte count from ``1048576``, ``2^20``, ``256MB`` or ``1GB`` (binary units)."""
    cleaned = text.strip()
    power = re.fullmatch(r"2\^(\d+)", cleaned)
    if power:
        return 1 << int(power.group(1))
    unit = re.fullmatch(r"(\d+)\s*([KMGT]B)", cleaned, re.IGNORECASE)
    if unit:
        return int(unit.group(1)) * dict(_UNITS)[unit.group(2).upper()]
    return int(cleaned)


def format_length(total_bytes: int) -> str:
    """The ``-tlmax`` spelling of *total_bytes*: ``1GB`` for 2^30, a bare integer otherwise."""
    for suffix, size in _UNITS:
        if total_bytes >= size and total_bytes % size == 0:
            return f"{total_bytes // size}{suffix}"
    return str(total_bytes)


_HEADER = re.compile(
    r"^\s*length=\s*(?P<num>[\d.]+)\s*(?P<unit>[A-Za-z]+)?"
    r"(?:\s*\((?:2\^(?P<exp>[\d.]+)|(?P<exact>\d+))\s*bytes?\))?"
    r"\s*,\s*time=\s*(?P<time>[\d.]+)\s*seconds"
)
_NO_ANOMALIES = re.compile(r"^\s*no anomalies in (?P<n>\d+) test result\(s\)")
_REST_CLEAN = re.compile(r"^\s*\.\.\.and (?P<n>\d+) test result\(s\) without anomalies")
_FAIL_WORD = re.compile(r"\bFAIL\b")
_ANOMALY = re.compile(
    # PractRand prints R= normally, but R>+99999 / R<-99999 for extreme values (the lines that
    # carry the longest FAIL !!!! runs), p= or p~=, and pads the name to 34 columns, so a name of
    # 34+ characters runs straight into the R.
    r"^\s+(?P<test>\S.*?)\s*R\s*[=<>~]+\s*(?P<raw>[+-]?\d\S*)\s+p\s*[=<>~]+\s*(?P<p>\S+)\s+"
    r"(?P<evaluation>VERY SUSPICIOUS|very suspicious|mildly suspicious|suspicious|unusual|FAIL)"
    r"(?P<marks>\s*!*)\s*$"
)


def _block_length(match: "re.Match[str]") -> int:
    if match.group("exp") is not None:
        return int(round(2 ** float(match.group("exp"))))
    if match.group("exact") is not None:
        return int(match.group("exact"))
    unit = (match.group("unit") or "bytes").lower().rstrip("s")
    return int(round(float(match.group("num")) * _UNIT_WORDS.get(unit, 1)))


def parse_practrand_output(text: str) -> Dict[str, Any]:
    """Parse the text ``RNG_test`` prints into a JSON-ready summary.

    Returns ``{"lengths": [...], "max_length_bytes", "anomalies", "evaluations", "fail"}``.
    Each entry of ``lengths`` is ``{"length_bytes", "length_text", "seconds",
    "without_anomalies", "anomalies": [{"test", "raw", "p", "evaluation"}]}``, where
    ``without_anomalies`` is the count of results PractRand reported clean at that length
    (``no anomalies in N test result(s)`` or ``...and N test result(s) without anomalies``).
    ``anomalies`` at the top level is every anomaly line with its length; ``evaluations``
    counts them by evaluation; ``fail`` is true iff any length has a ``FAIL``.
    """
    lengths: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            current = {
                "length_bytes": _block_length(header),
                "length_text": line.strip().split(", time=")[0],
                "seconds": float(header.group("time")),
                "without_anomalies": 0,
                "anomalies": [],
            }
            lengths.append(current)
            continue
        if current is None:
            if _FAIL_WORD.search(line):
                current = {
                    "length_bytes": 0,
                    "length_text": "(before any length header)",
                    "seconds": 0.0,
                    "without_anomalies": 0,
                    "anomalies": [],
                }
                lengths.append(current)
                current["anomalies"].append(
                    {
                        "test": line.strip(),
                        "raw": None,
                        "p": None,
                        "evaluation": "FAIL",
                        "unparsed": True,
                    }
                )
                current = None
            continue
        clean = _NO_ANOMALIES.match(line) or _REST_CLEAN.match(line)
        if clean:
            current["without_anomalies"] = int(clean.group("n"))
            continue
        found = _ANOMALY.match(line)
        if found:
            current["anomalies"].append(
                {
                    "test": found.group("test"),
                    "raw": found.group("raw"),
                    "p": found.group("p"),
                    "evaluation": found.group("evaluation"),
                }
            )
        elif _FAIL_WORD.search(line):
            # A FAIL line this parser does not understand is still a failure.
            current["anomalies"].append(
                {
                    "test": line.strip(),
                    "raw": None,
                    "p": None,
                    "evaluation": "FAIL",
                    "unparsed": True,
                }
            )
    anomalies = [
        {**item, "length_bytes": block["length_bytes"]}
        for block in lengths
        for item in block["anomalies"]
    ]
    counts = {name: 0 for name in EVALUATIONS}
    for item in anomalies:
        counts[item["evaluation"]] += 1
    return {
        "lengths": lengths,
        "max_length_bytes": max((b["length_bytes"] for b in lengths), default=0),
        "anomalies": anomalies,
        "evaluations": counts,
        "fail": counts["FAIL"] > 0,
    }


def verdict(summary: Dict[str, Any], *, required_bytes: Optional[int] = None) -> str:
    """``pass`` iff no FAIL at any length and (when given) PractRand reached *required_bytes*."""
    if summary["fail"]:
        return "fail"
    if not summary["lengths"]:
        return "fail"
    if required_bytes is not None and summary["max_length_bytes"] < required_bytes:
        return "fail"
    return "pass"


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


#: Wall-clock cap on one PractRand run: generous (2^30 bytes takes minutes to an hour or two).
DEFAULT_TIMEOUT_SECONDS = 6 * 3600.0


def run_practrand(
    rng_test: "str | Sequence[str]",
    mode: str,
    total_bytes: int,
    seed: int,
    *,
    workers: int = 1,
    echo: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Feed *mode* into ``RNG_test stdin64`` and return the parsed summary.

    *rng_test* is the binary, or a launcher argument list (``[python, script]``). After
    *timeout* seconds PractRand is killed and the run fails with ``timed_out`` true.
    The summary also carries ``mode``, ``seed``, ``bytes_requested``, ``command``,
    ``returncode``, ``timed_out`` and ``verdict``. No shell is involved: the command is an argument list.
    """
    launcher = [rng_test] if isinstance(rng_test, str) else list(rng_test)
    command = [
        *map(str, launcher),
        "stdin64",
        "-tlmax",
        format_length(total_bytes),
        "-multithreaded",
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    captured: List[bytes] = []

    def drain() -> None:
        assert process.stdout is not None
        for raw in iter(process.stdout.readline, b""):
            captured.append(raw)
            if echo:
                sys.stderr.write(raw.decode("utf-8", "replace"))
                sys.stderr.flush()

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        process.kill()

    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    watchdog.start()
    assert process.stdin is not None
    try:
        try:
            feed(mode, seed, total_bytes, process.stdin, workers=workers)
        except BrokenPipeError:
            pass  # PractRand reached its own -tlmax (or died) and closed its input
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        returncode = process.wait()
    except BaseException:
        # A feeder failure or KeyboardInterrupt: never leave PractRand or the reader behind.
        process.kill()
        process.wait()
        raise
    finally:
        watchdog.cancel()
        reader.join(timeout=10)
    output = b"".join(captured).decode("utf-8", "replace")
    summary = parse_practrand_output(output)
    summary.update(
        mode=mode,
        seed=seed,
        bytes_requested=total_bytes,
        command=command,
        returncode=returncode,
        timed_out=timed_out.is_set(),
    )
    ok = returncode == 0 and not timed_out.is_set()
    summary["verdict"] = verdict(summary, required_bytes=total_bytes) if ok else "fail"
    return summary


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _default_seed(mode: str) -> int:
    # Imported here so the feeders do not need pytest. The same seed the tests use, so a CLI
    # run reproduces a pytest run: FLUX_ACCURACY_TIER / FLUX_ACCURACY_SEED select the base.
    from tests.accuracy.harness.tier import current_tier

    return current_tier().seed("streams", "practrand", mode)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tests.accuracy.streams.practrand")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, doc in (("feed", "write a feeder's bytes to stdout"), ("run", "run PractRand")):
        sp = sub.add_parser(name, help=doc)
        sp.add_argument("--mode", choices=MODES, required=True)
        sp.add_argument("--bytes", default="2^30", help="byte budget: 2^30, 1GB, or an integer")
        sp.add_argument("--seed", type=int, default=None, help="int64 root seed")
        sp.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    run = sub.choices["run"]
    run.add_argument("--rng-test", default=os.environ.get(RNG_TEST_ENV))
    run.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="kill PractRand and fail after this long",
    )
    run.add_argument("--out", required=True, help="path of the JSON summary")
    run.add_argument("--quiet", action="store_true", help="do not echo PractRand's output")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    total = parse_length(args.bytes)
    seed = args.seed if args.seed is not None else _default_seed(args.mode)
    if args.command == "feed":
        try:
            feed(args.mode, seed, total, sys.stdout.buffer, workers=args.workers)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            # The consumer (head, RNG_test) closed early: not an error. Point stdout at
            # /dev/null so the interpreter's exit-time flush cannot raise again.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    if not args.rng_test:
        print(
            f"--rng-test or {RNG_TEST_ENV} must name the PractRand RNG_test binary", file=sys.stderr
        )
        return 2
    summary = run_practrand(
        args.rng_test,
        args.mode,
        total,
        seed,
        workers=args.workers,
        echo=not args.quiet,
        timeout=args.timeout_seconds,
    )
    Path(args.out).write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"practrand {args.mode}: {summary['verdict']} ({summary['evaluations']})", file=sys.stderr
    )
    return 0 if summary["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
