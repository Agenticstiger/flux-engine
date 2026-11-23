# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A profile of an earlier format keeps its bytes and its digest beside format 1-2-0.

The digest is ``jcs-document`` over the parsed document (PROFILE-SPEC.md section 1), so a key
the reader gained, and a document that does not hold it, add nothing to the form.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.seam.profile import load_profile
from flux_stats.profile import SCHEMA_URL, SCHEMA_URL_1_2_0, load_profile_text

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"

#: The digest of ``docs/spec/profile-example.json`` (format 1-0-0) as release 0.8.0 computed it.
EXAMPLE_1_0_0_DIGEST = "sha256:af97633046a8c9621d32fec595dd02672bc10e3fabd98c846a2ffcd713e89361"


def test_a_profile_of_format_1_0_0_keeps_its_digest() -> None:
    document = json.loads((SPEC / "profile-example.json").read_text(encoding="utf-8"))
    assert document["_schemaURL"] == SCHEMA_URL
    assert digest_of_document(document, subject="p").value == EXAMPLE_1_0_0_DIGEST
    pinned = load_profile(SPEC / "profile-example.json")
    assert pinned.digest.value == EXAMPLE_1_0_0_DIGEST
    assert pinned.profile.invariants == ()


def test_a_profile_of_format_1_2_0_validates_reads_and_digests() -> None:
    path = SPEC / "profile-1-2-example.json"
    pinned = load_profile(path)
    assert pinned.document["_schemaURL"] == SCHEMA_URL_1_2_0
    assert [entry.name for entry in pinned.profile.invariants] == [
        "payment.paid_after_due",
        "subscriber.tenure_within_term",
    ]
    profile, document = load_profile_text(path.read_text(encoding="utf-8"))
    assert document == pinned.document and profile == pinned.profile
    assert pinned.digest.value != EXAMPLE_1_0_0_DIGEST
