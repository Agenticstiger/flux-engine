# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 5: PractRand over the seed tree, and the machinery that feeds and reads it.

Three parts:

* the parser of ``RNG_test``'s output, on sample outputs written to match PractRand's format
  (pull-request tier, no binary needed);
* the feeders: the bytes written for a small ``N`` are exactly the engine's own words
  (``stream_entropy`` then ``DrawStream.from_entropy(...).next_raw()``), and the parallel
  path writes the same bytes as the serial one;
* the real run (nightly only, and only when ``PRACTRAND_RNG_TEST`` names the PractRand
  ``RNG_test`` binary): 2**30 bytes of each feeder.

Pass criterion of the real run: no ``FAIL`` at any length, and PractRand reached the full
length. ``unusual`` through ``VERY SUSPICIOUS`` results are recorded, not judged. See
``practrand.py`` for the command lines.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import io  # noqa: E402
import os  # noqa: E402
import struct  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from flux_engine.core.rng import DrawStream  # noqa: E402
from flux_engine.core.seedtree import stream_entropy  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.streams import practrand  # noqa: E402

FAMILY = "streams"

CLEAN_BLOCKS = """\
RNG_test using PractRand version 0.94
RNG = RNG_stdin64, seed = unknown
test set = core, folding = standard(unknown format)

rng=RNG_stdin64, seed=unknown
length= 128 megabytes (2^27 bytes), time= 1.4 seconds
  no anomalies in 189 test result(s)

rng=RNG_stdin64, seed=unknown
length= 256 megabytes (2^28 bytes), time= 2.5 seconds
  no anomalies in 196 test result(s)

rng=RNG_stdin64, seed=unknown
length= 1 gigabyte (2^30 bytes), time= 10.1 seconds
  no anomalies in 213 test result(s)

"""

TABLE_BLOCK = """\
rng=RNG_stdin64, seed=unknown
length= 512 megabytes (2^29 bytes), time= 5.2 seconds
  Test Name                         Raw       Processed     Evaluation
  [Low1/64]DC6-9x1Bytes-1           R=  +5.3  p =  5.3e-3   unusual
  [Low8/32]FPF-14+6/16:cross        R=  +9.1  p =  2.2e-4   mildly suspicious
  ...and 205 test result(s) without anomalies

"""

FAIL_BLOCK = """\
rng=RNG_stdin64, seed=unknown
length= 2 gigabytes (2^31 bytes), time= 23.9 seconds
  Test Name                         Raw       Processed     Evaluation
  BCFN(2+0,13-3,T)                  R=+127.4  p =  1.4e-60    FAIL !!!!
  [Low1/64]FPF-14+6/16:(0,14-0)     R= +14.2  p =  1.2e-12  VERY SUSPICIOUS
  [Low1/32]Gap-16:A                 R=  -8.8  p =1-3.4e-5   very suspicious
  [Low4/16]DC6-9x1Bytes-1           R=  +7.0  p =  4.4e-4   suspicious
  ...and 201 test result(s) without anomalies

"""


def test_parser_reads_blocks_without_anomalies() -> None:
    summary = practrand.parse_practrand_output(CLEAN_BLOCKS)
    lengths = summary["lengths"]
    assert [b["length_bytes"] for b in lengths] == [1 << 27, 1 << 28, 1 << 30]
    assert [b["without_anomalies"] for b in lengths] == [189, 196, 213]
    assert [b["seconds"] for b in lengths] == [1.4, 2.5, 10.1]
    assert summary["anomalies"] == []
    assert summary["max_length_bytes"] == 1 << 30
    assert summary["fail"] is False
    assert practrand.verdict(summary, required_bytes=1 << 30) == "pass"


def test_parser_reads_an_anomaly_table() -> None:
    summary = practrand.parse_practrand_output(TABLE_BLOCK)
    (block,) = summary["lengths"]
    assert block["length_bytes"] == 1 << 29
    assert block["without_anomalies"] == 205
    assert [(a["test"], a["raw"], a["p"], a["evaluation"]) for a in block["anomalies"]] == [
        ("[Low1/64]DC6-9x1Bytes-1", "+5.3", "5.3e-3", "unusual"),
        ("[Low8/32]FPF-14+6/16:cross", "+9.1", "2.2e-4", "mildly suspicious"),
    ]
    assert summary["evaluations"]["unusual"] == 1
    assert summary["evaluations"]["mildly suspicious"] == 1
    assert summary["fail"] is False
    assert practrand.verdict(summary, required_bytes=1 << 29) == "pass"


def test_parser_reads_every_evaluation_and_fails_on_fail() -> None:
    summary = practrand.parse_practrand_output(CLEAN_BLOCKS + TABLE_BLOCK + FAIL_BLOCK)
    assert summary["evaluations"] == {
        "unusual": 1,
        "mildly suspicious": 1,
        "suspicious": 1,
        "very suspicious": 1,
        "VERY SUSPICIOUS": 1,
        "FAIL": 1,
    }
    failing = [a for a in summary["anomalies"] if a["evaluation"] == "FAIL"]
    assert failing == [
        {
            "test": "BCFN(2+0,13-3,T)",
            "raw": "+127.4",
            "p": "1.4e-60",
            "evaluation": "FAIL",
            "length_bytes": 1 << 31,
        }
    ]
    assert {a["p"] for a in summary["anomalies"]} >= {"1-3.4e-5", "1.2e-12"}
    assert summary["fail"] is True
    assert summary["max_length_bytes"] == 1 << 31
    assert practrand.verdict(summary) == "fail"


EXTREME_BLOCK = """\
rng=RNG_stdin64, seed=unknown
length= 1 gigabyte (2^30 bytes), time= 11.0 seconds
  Test Name                         Raw       Processed     Evaluation
  [Low1/64]BCFN(2+0,13-0,T)         R>+99999  p =  0        FAIL !!!!!!!!
  [Low1/64]DC6-9x1Bytes-1           R<-99999  p~= 1         FAIL !!!!!!!!
  [Low1/64]a-very-long-test-name-34chars-R= +9.0  p =  1.0e-9   VERY SUSPICIOUS
  ...and 150 test result(s) without anomalies

"""


def test_parser_reads_extreme_raw_values_and_fails() -> None:
    summary = practrand.parse_practrand_output(EXTREME_BLOCK)
    anomalies = summary["anomalies"]
    assert [(a["raw"], a["p"], a["evaluation"]) for a in anomalies] == [
        ("+99999", "0", "FAIL"),
        ("-99999", "1", "FAIL"),
        ("+9.0", "1.0e-9", "VERY SUSPICIOUS"),
    ]
    assert summary["fail"] is True
    assert practrand.verdict(summary, required_bytes=1 << 30) == "fail"


def test_a_fail_line_the_parser_cannot_read_is_still_a_failure() -> None:
    text = (
        "length= 1 gigabyte (2^30 bytes), time= 11.0 seconds\n"
        "  something-new   garbled   FAIL !!!!\n"
        "  ...and 10 test result(s) without anomalies\n"
    )
    summary = practrand.parse_practrand_output(text)
    assert summary["fail"] is True
    assert summary["anomalies"][0]["unparsed"] is True
    assert practrand.verdict(summary, required_bytes=1 << 30) == "fail"
    early = practrand.parse_practrand_output("  oops FAIL\n")
    assert early["fail"] is True


def test_parser_reads_small_and_bare_lengths() -> None:
    text = (
        "length= 512 bytes (2^9 bytes), time= 0.0 seconds\n  no anomalies in 3 test result(s)\n"
        "length= 64 kilobytes (2^16 bytes), time= 0.1 seconds\n"
        "  no anomalies in 80 test result(s)\n"
        "length= 3 megabytes, time= 0.4 seconds\n  no anomalies in 90 test result(s)\n"
    )
    summary = practrand.parse_practrand_output(text)
    assert [b["length_bytes"] for b in summary["lengths"]] == [512, 1 << 16, 3 << 20]


def test_empty_output_is_not_a_pass() -> None:
    summary = practrand.parse_practrand_output("")
    assert summary["lengths"] == [] and summary["fail"] is False
    assert practrand.verdict(summary) == "fail"


def test_a_run_that_stopped_short_is_not_a_pass() -> None:
    summary = practrand.parse_practrand_output(CLEAN_BLOCKS)
    assert practrand.verdict(summary, required_bytes=1 << 31) == "fail"


def test_length_spellings() -> None:
    assert practrand.parse_length("2^30") == 1 << 30
    assert practrand.parse_length("1GB") == 1 << 30
    assert practrand.parse_length("256mb") == 256 << 20
    assert practrand.parse_length("4096") == 4096
    assert practrand.format_length(1 << 30) == "1GB"
    assert practrand.format_length(3 << 20) == "3MB"
    assert practrand.format_length(1000) == "1000"


# ---------------------------------------------------------------------------
# Feeders
# ---------------------------------------------------------------------------


def _engine_first_words(seed: int, rows: int) -> bytes:
    root = practrand.make_root(seed)
    return b"".join(
        struct.pack(
            "<Q",
            DrawStream.from_entropy(stream_entropy(root, practrand.row_path(row))).next_raw(),
        )
        for row in range(rows)
    )


def _engine_interleave(seed: int, streams: int, words: int) -> bytes:
    root = practrand.make_root(seed)
    drawn = [
        DrawStream.from_entropy(stream_entropy(root, practrand.row_path(row)))
        for row in range(streams)
    ]
    return b"".join(struct.pack("<Q", drawn[index % streams].next_raw()) for index in range(words))


def test_first_words_feeder_equals_the_engines_own_words(tier: Tier) -> None:
    seed = tier.seed("streams", "practrand", "feeder-test")
    rows = 300
    assert practrand.first_words_bytes(seed, rows) == _engine_first_words(seed, rows)
    sink = io.BytesIO()
    assert practrand.feed("first-words", seed, rows * 8, sink) == rows * 8
    assert sink.getvalue() == _engine_first_words(seed, rows)


def test_first_words_feeder_rounds_down_to_whole_words(tier: Tier) -> None:
    seed = tier.seed("streams", "practrand", "feeder-test")
    sink = io.BytesIO()
    practrand.feed("first-words", seed, 8 * 10 + 5, sink)
    assert sink.getvalue() == _engine_first_words(seed, 10)


def test_parallel_first_words_feeder_equals_the_serial_one(tier: Tier) -> None:
    seed = tier.seed("streams", "practrand", "feeder-test")
    total = 8 * 1000
    serial = b"".join(practrand.iter_first_words(seed, total, workers=1, chunk_rows=64))
    parallel = b"".join(practrand.iter_first_words(seed, total, workers=3, chunk_rows=64))
    assert len(serial) == total
    assert parallel == serial == _engine_first_words(seed, 1000)


def test_interleave_feeder_is_round_robin_over_sibling_streams(tier: Tier) -> None:
    seed = tier.seed("streams", "practrand", "feeder-test")
    streams = practrand.INTERLEAVE_STREAMS
    words = streams * 3 + 17  # a partial final round
    expected = _engine_interleave(seed, streams, words)
    small = practrand.interleave_bytes(seed, words * 8)
    sink = io.BytesIO()
    practrand.feed("interleave", seed, words * 8, sink)
    assert small == expected
    assert sink.getvalue() == expected


def test_feed_refuses_an_unknown_mode() -> None:
    with pytest.raises(ValueError):
        practrand.feed("shuffle", 1, 64, io.BytesIO())


def _fake_rng_test(
    tmp_path: Path, report: str, *, read_input: bool, sleep: float = 0.0
) -> "list[str]":
    """A launcher for a stand-in RNG_test: ``[python, script]``, so no shebang is involved."""
    script = tmp_path / "RNG_test.py"
    body = "sys.stdin.buffer.read()\n" if read_input else ""
    delay = f"import time\ntime.sleep({sleep})\n" if sleep else ""
    script.write_text(
        f"import sys\n{delay}{body}sys.stdout.write({report!r})\n"
        "sys.stdout.write(' '.join(sys.argv[1:]) + '\\n')\n"
    )
    return [sys.executable, str(script)]


@pytest.mark.parametrize("mode", practrand.MODES)
def test_runner_pipes_the_feeder_in_and_parses_the_report(
    mode: str, tmp_path: Path, tier: Tier
) -> None:
    fake = _fake_rng_test(tmp_path, CLEAN_BLOCKS, read_input=True)
    seed = tier.seed("streams", "practrand", "runner-test")
    summary = practrand.run_practrand(fake, mode, 1 << 20, seed, workers=2)
    assert summary["command"][2:] == ["stdin64", "-tlmax", "1MB", "-multithreaded"]
    assert summary["returncode"] == 0
    assert summary["verdict"] == "pass"
    assert summary["max_length_bytes"] == 1 << 30


def test_runner_reports_fail_and_survives_a_closed_pipe(tmp_path: Path, tier: Tier) -> None:
    fake = _fake_rng_test(tmp_path, CLEAN_BLOCKS + FAIL_BLOCK, read_input=False)
    seed = tier.seed("streams", "practrand", "runner-test")
    summary = practrand.run_practrand(fake, "first-words", 1 << 24, seed, workers=1)
    assert summary["verdict"] == "fail"
    assert summary["evaluations"]["FAIL"] == 1


def test_runner_cleans_up_when_the_feeder_raises(
    tmp_path: Path, tier: Tier, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_rng_test(tmp_path, CLEAN_BLOCKS, read_input=False, sleep=60.0)
    spawned = []
    real_popen = practrand.subprocess.Popen

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("feeder failed")

    monkeypatch.setattr(practrand.subprocess, "Popen", spy)
    monkeypatch.setattr(practrand, "feed", boom)
    with pytest.raises(RuntimeError, match="feeder failed"):
        practrand.run_practrand(fake, "first-words", 1 << 20, 1)
    assert spawned and spawned[0].poll() is not None  # killed and reaped, not left running


def test_feed_cli_exits_quietly_when_its_consumer_closes_early() -> None:
    command = (
        f"{sys.executable} -m tests.accuracy.streams.practrand feed --mode interleave "
        "--bytes 2^26 --seed 3 | head -c 100 >/dev/null"
    )
    # Not shell=True on untrusted input: a fixed command line, to get a real pipe.
    result = subprocess.run(  # noqa: S602
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert result.stderr == ""


def test_runner_kills_a_hung_practrand_at_the_timeout(tmp_path: Path, tier: Tier) -> None:
    fake = _fake_rng_test(tmp_path, CLEAN_BLOCKS, read_input=False, sleep=60.0)
    seed = tier.seed("streams", "practrand", "runner-test")
    summary = practrand.run_practrand(fake, "first-words", 1 << 22, seed, timeout=1.0)
    assert summary["timed_out"] is True
    assert summary["verdict"] == "fail"


# ---------------------------------------------------------------------------
# The real run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", practrand.MODES)
def test_practrand_finds_no_failure_at_any_length(
    mode: str, tier: Tier, accuracy_log: ResultLog
) -> None:
    rng_test = os.environ.get(practrand.RNG_TEST_ENV, "").strip()
    if not rng_test:
        pytest.skip(f"{practrand.RNG_TEST_ENV} does not name a PractRand RNG_test binary")
    tier.only_nightly("PractRand reads 2**30 bytes per feeder")
    total = 1 << 30
    seed = tier.seed("streams", "practrand", mode)
    summary = practrand.run_practrand(
        rng_test, mode, total, seed, workers=os.cpu_count() or 1, echo=True
    )
    passed = summary["verdict"] == "pass"
    accuracy_log.record(
        family=FAMILY,
        case=f"practrand/{mode}",
        criterion="no FAIL at any length, and PractRand reached 2**30 bytes",
        passed=passed,
        seed=seed,
        n=total,
        metrics={
            "verdict": summary["verdict"],
            "returncode": summary["returncode"],
            "maxLengthBytes": summary["max_length_bytes"],
            "evaluations": summary["evaluations"],
            "anomalies": summary["anomalies"],
            "lengths": [
                {
                    "bytes": b["length_bytes"],
                    "seconds": b["seconds"],
                    "withoutAnomalies": b["without_anomalies"],
                    "anomalies": len(b["anomalies"]),
                }
                for b in summary["lengths"]
            ],
        },
    )
    assert passed, f"{mode}: {summary['evaluations']}, reached {summary['max_length_bytes']} bytes"
