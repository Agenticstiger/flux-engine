# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A published schema keeps its file: the profile format 1-0-0 stays beside 1-1-0, as published."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"

#: The sha256 of the profile schema of format 1-0-0 as release 0.6.2 published it. A profile written
#: before 0.7.0 names its ``$id``, so the file is kept and never edited.
PROFILE_1_0_0_SHA256 = "fde1dfedd35a20556643ca9102c6886de3982b8761e8730d70d46fdc7c360674"


def test_the_profile_1_0_0_schema_is_kept_as_published() -> None:
    raw = (SPEC / "profile-1-0.schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PROFILE_1_0_0_SHA256
    assert json.loads(raw)["$id"].endswith("/profile/1-0-0/FluxProfile.json")
