# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux_engine.core.artifact -- the single artifact-writer choke point."""

from __future__ import annotations

import json

import pytest

from flux_engine.core.artifact import ENVELOPE_NON_DIGEST_FIELDS, write_artifact


@pytest.mark.unit
def test_envelope_has_the_four_fields(tmp_path) -> None:
    target = tmp_path / "artifact.json"
    write_artifact(
        target,
        {"b": 1, "a": 2},
        kind="PlanArtifact",
        schema_version="0.1.0",
        generated_at="2026-01-01T00:00:00Z",
    )
    data = json.loads(target.read_text())
    assert data["schema_version"] == "0.1.0"
    assert data["kind"] == "PlanArtifact"
    assert data["generated_at"] == "2026-01-01T00:00:00Z"
    assert data["generated_by"] == "flux-engine"
    assert data["a"] == 2
    assert data["b"] == 1


@pytest.mark.unit
def test_keys_are_sorted_and_file_ends_with_newline(tmp_path) -> None:
    target = tmp_path / "artifact.json"
    write_artifact(
        target,
        {"zeta": 1, "alpha": 2},
        kind="PlanArtifact",
        schema_version="0.1.0",
        generated_at="2026-01-01T00:00:00Z",
    )
    text = target.read_text()
    assert text.endswith("\n")
    top_level_keys = list(json.loads(text).keys())
    assert top_level_keys == sorted(top_level_keys)


@pytest.mark.unit
def test_generated_at_is_present_and_caller_supplied(tmp_path) -> None:
    target = tmp_path / "artifact.json"
    write_artifact(
        target,
        {},
        kind="RunAttestation",
        schema_version="0.1.0",
        generated_at="2026-06-15T12:00:00Z",
    )
    data = json.loads(target.read_text())
    assert data["generated_at"] == "2026-06-15T12:00:00Z"


@pytest.mark.unit
def test_non_digest_fields_mask() -> None:
    assert ENVELOPE_NON_DIGEST_FIELDS == frozenset({"generated_at", "generated_by"})


@pytest.mark.unit
def test_creates_parent_directories(tmp_path) -> None:
    target = tmp_path / "nested" / "dir" / "artifact.json"
    write_artifact(
        target,
        {},
        kind="GoldenSnapshot",
        schema_version="0.1.0",
        generated_at="2026-01-01T00:00:00Z",
    )
    assert target.exists()
