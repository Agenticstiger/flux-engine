# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §2.4's totality test, over the receipt the engine actually writes.

``tests/core/test_canonical.py`` checks ``partition_fields`` against a
document it builds *from the mask sets*, so every field in that document
is masked by construction and the partition identity holds trivially.
§2.4 asks for the check over the real artifact: "a totality test
enumerates every receipt field and asserts it is either digested or
listed". This module runs the small telco fixture through
``flux_engine.stages.apply``, reads ``receipt.json`` off disk, and
enumerates it -- so a volatile field added to ``apply``'s receipt dict (a
duration, a hostname, a timestamp) is caught here rather than entering a
digest with no test objecting.

It also pins KERNEL-SPEC §1's version table: the four version fields §1
places in the receipt must all be present, and ``engineVersion`` must not
move any digest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.canonical import (
    NON_DIGEST_FIELDS,
    SELF_REFERENTIAL_FIELDS,
    field_paths,
    partition_fields,
)
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

#: §1's version table, as the receipt row spells it.
_SPEC_SECTION_1_RECEIPT_VERSIONS = (
    "kernelVersion",
    "seedTreeVersion",
    "traceVersion",
    "engineVersion",
)


@pytest.fixture(scope="module")
def receipt(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    state = tmp_path_factory.mktemp("receipt-totality") / ".flux"
    result = apply(SMALL_BUNDLE, state_dir=state)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(state, result.artifacts["run_id"])
    return json.loads(layout.receipt_path.read_text(encoding="utf-8"))


def test_every_field_of_a_real_receipt_is_either_digested_or_masked(
    receipt: Dict[str, Any],
) -> None:
    partition = partition_fields(receipt)
    assert set(partition.all_fields) == set(field_paths(receipt))
    assert set(partition.digested).isdisjoint(partition.masked)
    assert set(partition.all_fields) == set(partition.digested) | set(partition.masked)


def test_every_masked_leaf_of_a_real_receipt_is_a_declared_mask_name(
    receipt: Dict[str, Any],
) -> None:
    """A leaf is masked only because its NAME is in one of the two sets.

    Reading the masked set back to the declared names is what makes this
    an enumeration of the artifact rather than a restatement of the mask.
    """
    declared = NON_DIGEST_FIELDS | SELF_REFERENTIAL_FIELDS
    masked_leaves = {path.rsplit(".", 1)[-1] for path in partition_fields(receipt).masked}
    assert masked_leaves <= declared, sorted(masked_leaves - declared)


def test_the_receipt_carries_kernel_spec_section_1s_version_table(
    receipt: Dict[str, Any],
) -> None:
    missing = [name for name in _SPEC_SECTION_1_RECEIPT_VERSIONS if not receipt.get(name)]
    assert not missing, missing


def test_engine_version_is_masked_so_it_moves_no_digest(receipt: Dict[str, Any]) -> None:
    """§1: ``engineVersion`` is "informational only, never hashed"."""
    assert "engineVersion" in NON_DIGEST_FIELDS
    assert "engineVersion" in partition_fields(receipt).masked
