# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The size-and-split page says what a joined set of shares is, and where the share files stay."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PAGE = Path(__file__).resolve().parents[2] / "docs" / "how-to" / "size-and-split-a-run.md"


def _text() -> str:
    return " ".join(PAGE.read_text(encoding="utf-8").split())


def test_a_joined_set_of_shares_is_said_not_to_be_a_run() -> None:
    text = _text()
    assert "holds the run's rows, but it is not a run" in text
    assert "`verify` and `ship` refuse a run directory that holds only shares" in text
    assert "apply the run whole once" in text and "cmp " in PAGE.read_text(encoding="utf-8")


def test_the_share_files_are_said_to_stay_beside_the_whole_streams() -> None:
    text = _text()
    assert "share files stay in the run directory" in text
    assert "names its files one by one" in text
