# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify --sample``: the pinned-profile comparison read from a sample of whole blocks.

docs/spec/SAMPLE-SPEC.md, end to end on the small telco bundle: a run pinned to
its own profile is verified on every block, on half of them, and against a
profile whose mean of ``amount`` has moved, with and without a committed slice.
A sampled verify forms no scorecard, and is refused without the profile it
compares with. Its reader splits an emit into the lines the full reader does.
"""

# ruff: noqa: F811
# The fixtures are the scorecard tests' own, imported by name so a run is built one way.

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.cli import main
from flux_engine.concordance.sampling import SampleRequest

from .test_scorecard_stages import (  # noqa: F401
    EXPOSE,
    Pinned,
    Unpinned,
    _own_rows_slice,
    _shifted,
    compiled_and_rows,
    own_pinned,
    own_profile,
    unpinned,
)

pytestmark = pytest.mark.integration

BLOCK = 8
#: Far past the declared absolute tolerance of 50 on the mean of ``amount``.
FAR_SHIFT = 10_000


def _request(fraction: str, seed: int = 0) -> SampleRequest:
    return SampleRequest(fraction=Fraction(fraction), block_records=BLOCK, seed=seed)


def _means(emit: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [entry for entry in emit["properties"] if "mean" in entry["propertyId"]]


def test_every_block_read_passes_with_every_mean_within(own_pinned: Pinned) -> None:
    result = own_pinned.verify(profile=own_pinned.profile, sample=_request("1"))
    assert result.exit_code == 0, result.stderr
    leg = result.artifacts["fidelity"][EXPOSE]
    assert leg["status"] == "passed" and leg["sampled"] is True and leg["reference"] == "profile"
    assert leg["records"]["read"] == leg["records"]["of"]
    assert leg["blocks"]["read"] == leg["blocks"]["of"]
    means = _means(leg)
    assert means and all(entry["outcome"] == "within" for entry in means)
    document = result.artifacts["sample"]
    assert document["fraction"] == "1" and document["blockRecords"] == BLOCK
    assert document["emits"][EXPOSE]["status"] == "passed"
    scorecard = result.artifacts["scorecard"]
    assert scorecard["status"] == "not checked" and "no scorecard" in scorecard["detail"]
    assert f"  fidelity {EXPOSE}: passed -- sampled " in result.stdout


def test_half_the_blocks_carry_intervals_and_repeat_exactly(own_pinned: Pinned) -> None:
    first = own_pinned.verify(profile=own_pinned.profile, sample=_request("1/2"))
    again = own_pinned.verify(profile=own_pinned.profile, sample=_request("1/2"))
    assert first.artifacts["sample"] == again.artifacts["sample"]
    emit = first.artifacts["sample"]["emits"][EXPOSE]
    assert emit["records"]["read"] < emit["records"]["of"]
    assert emit["blocks"]["read"] < emit["blocks"]["of"]
    groups = [group for entry in _means(emit) for group in entry["groups"]]
    assert groups and all("interval" in group for group in groups)

    other = own_pinned.verify(profile=own_pinned.profile, sample=_request("1/2", seed=1))
    read = other.artifacts["sample"]["emits"][EXPOSE]
    assert read["blocks"] == emit["blocks"]
    assert read != emit, "another seed reads another set of blocks"


def test_a_profile_mean_moved_far_fails_the_emit(
    tmp_path: Path, own_profile: Dict[str, Any]
) -> None:
    pinned = Pinned(tmp_path, _shifted(own_profile, FAR_SHIFT))
    result = pinned.verify(profile=pinned.profile, sample=_request("1"))
    assert result.exit_code != 0
    leg = result.artifacts["fidelity"][EXPOSE]
    assert leg["status"] == "failed"
    amount = next(entry for entry in _means(leg) if entry["columns"] == ["amount"])
    assert amount["outcome"] == "outside"
    assert "property " + amount["propertyId"] in result.stderr
    assert "outside" in result.stderr


def test_an_emit_with_a_committed_slice_is_compared_with_the_profile_on_its_sample(
    tmp_path: Path, own_profile: Dict[str, Any]
) -> None:
    pinned = Pinned(tmp_path, _shifted(own_profile, FAR_SHIFT))
    uri = _own_rows_slice(pinned.run_dir, tmp_path / "own.ndjson")
    both = pinned.verify(
        profile=pinned.profile, committed_slice={EXPOSE: uri}, sample=_request("1")
    )
    leg = both.artifacts["fidelity"][EXPOSE]
    assert leg["outside"] == [], "the slice, read in full, found nothing outside"
    assert leg["profile"]["sampled"] is True and leg["profile"]["status"] == "failed", leg
    assert leg["status"] == "failed" and both.exit_code == 1
    assert EXPOSE in both.artifacts["sample"]["emits"]
    assert "against the pinned profile" in both.stderr


def test_a_sample_without_the_profile_is_refused_before_any_leg(own_pinned: Pinned) -> None:
    result = own_pinned.verify(sample=_request("1"))
    assert result.exit_code == 1 and result.error_code == "FLX-3109", result.stderr
    assert "--profile" in result.stderr
    assert "fidelity" not in result.artifacts


def test_a_sample_of_a_run_that_pins_no_profile_is_refused(unpinned: Unpinned) -> None:
    from flux_engine.stages.verify import verify
    from tests.stages.test_scorecard_stages import FIXTURE_BUNDLE

    result = verify(
        FIXTURE_BUNDLE, run_id=unpinned.run_id, state_dir=unpinned.state, sample=_request("1")
    )
    assert result.exit_code == 1 and result.error_code == "FLX-3109", result.stderr
    assert "pins no profile" in result.stderr
    assert "fidelity" not in result.artifacts


@pytest.mark.parametrize(
    "arguments",
    [
        ["--sample", "0"],
        ["--sample", "1.5"],
        ["--sample", "abc"],
        ["--sample", "nan"],
        ["--sample", "1e9999999"],
        ["--sample", "1", "--sample-block", "0"],
        ["--sample", "1", "--sample-seed", "-1"],
        ["--sample-block", "8"],
        ["--sample-seed", "3"],
    ],
)
def test_a_bad_sample_option_is_a_usage_error(tmp_path: Path, arguments: List[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), pytest.raises(SystemExit) as raised:
        main(["verify", str(tmp_path), "--run-id", "x", *arguments])
    assert raised.value.code == 2
    assert "sample" in err.getvalue() + out.getvalue()


def test_the_cli_runs_a_sampled_verify(own_pinned: Pinned) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(
            [
                "verify",
                str(own_pinned.bundle),
                "--run-id",
                own_pinned.run_id,
                "--state-dir",
                str(own_pinned.state),
                "--profile",
                str(own_pinned.profile),
                "--sample",
                "1",
                "--sample-block",
                str(BLOCK),
                "--sample-seed",
                "2",
                "--json",
            ]
        )
    assert code == 0
    document = json.loads(out.getvalue())
    sample = document["artifacts"]["sample"]
    assert sample["fraction"] == "1" and sample["seed"] == 2


def test_the_sampled_reader_splits_an_emit_into_the_full_readers_lines(tmp_path: Path) -> None:
    from flux_engine.stages.verify import _emit_lines

    text = '["a"]\r\n   \n["b\u2028c"]\n\n["d"]\x85["e"]\r["f"]\x0b\n["g"]'
    path = tmp_path / "emit.ndjson"
    path.write_bytes(text.encode("utf-8"))
    assert list(_emit_lines(path)) == path.read_text(encoding="utf-8").splitlines()
