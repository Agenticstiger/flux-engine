# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.calibrate.provenance."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from flux_engine.calibrate.provenance import (
    PRIOR_SOURCE_AUTHORED,
    SCHEMA_VERSION,
    build_calibration_provenance,
    write_calibration_provenance,
)


@pytest.mark.unit
def test_build_calibration_provenance_marks_every_dial_authored() -> None:
    document = build_calibration_provenance(
        world_ref="q3-retention-world",
        dials={"engagement": Decimal("0.4"), "campaignResponse": Decimal("0.10")},
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert document["kind"] == "CalibrationProvenance"
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["worldRef"] == "q3-retention-world"
    for entry in document["dials"].values():
        assert entry["priorSource"] == PRIOR_SOURCE_AUTHORED


@pytest.mark.unit
def test_build_calibration_provenance_renders_decimals_as_exact_strings() -> None:
    document = build_calibration_provenance(
        world_ref="w",
        dials={"d": Decimal("0.100000")},
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert document["dials"]["d"]["value"] == "0.100000"


@pytest.mark.unit
def test_write_calibration_provenance_is_byte_identical_across_two_calls(tmp_path) -> None:
    kwargs = dict(
        world_ref="q3-retention-world",
        dials={"engagement": Decimal("0.4")},
        generated_at="2026-01-01T00:00:00.000Z",
    )
    first = write_calibration_provenance(tmp_path / "a" / "calibration-provenance.json", **kwargs)
    second = write_calibration_provenance(tmp_path / "b" / "calibration-provenance.json", **kwargs)
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.unit
def test_write_calibration_provenance_writes_sorted_key_json_with_trailing_newline(
    tmp_path,
) -> None:
    path = write_calibration_provenance(
        tmp_path / "calibration-provenance.json",
        world_ref="w",
        dials={"d": Decimal("1")},
        generated_at="2026-01-01T00:00:00.000Z",
    )
    text = path.read_text()
    assert text.endswith("\n")
    # sort_keys=True: envelope keys must appear in ASCII order.
    assert text.index('"dials"') < text.index('"generated_at"') < text.index('"kind"')
    json.loads(text)  # round-trips as valid JSON
