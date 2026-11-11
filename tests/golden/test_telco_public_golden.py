# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the *public* telco example's apply-only run to the artifacts under
``tests/golden/telco-public/``.

Companion to ``tests/golden/test_telco_golden.py``, which pins the
internal ``tests/fixtures/telco-fidelity`` fixture (25k subjects, a
declared fidelity block, run through apply + ship). This module instead
re-runs the public ``examples/telco-payment-recovery`` bundle from the
sibling FLUX repo -- the one every README walkthrough and demo command
actually points at -- through ``apply`` alone (no fidelity block is
declared there), and pins its non-empty, journey-driven emit: proof that
``fluxsim run <FLUX>/examples/telco-payment-recovery`` keeps producing real
rows, not the empty-emit regression the journey-driven emit fix closed.

Regenerating the baseline is a deliberate, out-of-band act:
``python tests/golden/telco/_generate_golden.py``, followed by a new row
in ``GOLDEN-CHANGELOG.md`` -- enforced here by
``test_a_golden_change_requires_a_changelog_row``, never by this module
writing the row itself.

Skipped when the sibling FLUX repo (a separate, gitignored checkout) is
not present -- this module has no way to run the public example without
it, and that absence is an environment fact, not a defect in this repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.canonical import SELF_REFERENTIAL_FIELDS, mask_document
from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout

REPO_ROOT = Path(__file__).resolve().parents[2]
FLUX_REPO = REPO_ROOT.parent / "FLUX"
BUNDLE_DIR = FLUX_REPO / "examples" / "telco-payment-recovery"
GOLDEN_DIR = Path(__file__).resolve().parent / "telco-public"
CHANGELOG_PATH = REPO_ROOT / "GOLDEN-CHANGELOG.md"
EXPOSE_ID = "payment_recovery_moment"

_EXTRA_VOLATILE_FIELDS = frozenset({"committedAt", "signature", "signer", "C2"})

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not BUNDLE_DIR.is_dir(),
        reason=f"sibling FLUX repo example not found at {BUNDLE_DIR}",
    ),
]


def _masked(document: Dict[str, Any]) -> Dict[str, Any]:
    return mask_document(
        document, self_referential=SELF_REFERENTIAL_FIELDS | _EXTRA_VOLATILE_FIELDS
    )


def _read_golden(name: str) -> Dict[str, Any]:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden-telco-public") / ".flux"


@pytest.fixture(scope="module")
def run_id(state_dir: Path) -> str:
    applied = apply(BUNDLE_DIR, state_dir=state_dir)
    assert applied.success, applied.stderr
    return str(applied.artifacts["run_id"])


@pytest.fixture(scope="module")
def layout(state_dir: Path, run_id: str) -> RunLayout:
    return RunLayout.for_state_dir(state_dir, run_id)


def test_run_digest_matches_the_pinned_baseline(layout: RunLayout) -> None:
    actual = _masked(json.loads(layout.run_digest_path.read_text(encoding="utf-8")))
    expected = _read_golden("run-digest.json")
    assert actual == expected


def test_seam_attestation_matches_the_pinned_baseline(layout: RunLayout) -> None:
    actual = _masked(
        json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    )
    expected = _read_golden("seam-attestation.json")
    assert actual == expected


def test_emitted_row_count_matches_the_pinned_baseline_and_is_nonzero(
    layout: RunLayout,
) -> None:
    """The journey-driven emit fix's whole point: the public example must
    emit real rows, not an empty ``emit-<exposeId>.ndjson``.
    """
    lines = [
        line
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    expected = _read_golden("row-count.json")[f"emit-{EXPOSE_ID}"]
    assert expected > 0, "pinned row count must itself be > 0"
    assert len(lines) == expected


def _has_changelog_row(changelog_text: str, digest: str) -> bool:
    """Whether ``GOLDEN-CHANGELOG.md`` carries a row for *digest*.

    The one predicate both the gate and its control run, so a change that
    stops the gate constraining anything -- a ``_run_digest_value`` that
    returned a constant, a lookup that matched everything -- is caught by
    the control instead of hiding behind it.
    """
    return digest in changelog_text


def _run_digest_of(document: Dict[str, Any]) -> str:
    return digest_of_document(document, subject="runDigest").value


def _run_digest_value() -> str:
    """Keyed the same way ``tests/golden/test_telco_golden.py`` keys its
    changelog rows: KERNEL-SPEC §11's ``digest_of_document`` over the
    pinned, masked ``run-digest.json``.
    """
    return _run_digest_of(_read_golden("run-digest.json"))


def test_the_pinned_baseline_has_a_changelog_row() -> None:
    changelog_text = CHANGELOG_PATH.read_text(encoding="utf-8")
    digest = _run_digest_value()
    assert _has_changelog_row(changelog_text, digest), (
        f"{CHANGELOG_PATH.name} has no row for {digest} -- "
        "tests/golden/telco-public/run-digest.json changed with no changelog row"
    )


def test_a_golden_change_requires_a_changelog_row(tmp_path: Path) -> None:
    """A pinned artifact that moved without a matching changelog row is a
    regression, not a golden update -- proved by actually moving a byte.

    The previous version of this control moved nothing: it asserted that a
    fabricated digest was absent from the changelog and that the real one
    differed from the fabricated one, both true by construction. A
    ``_run_digest_value`` that stopped recomputing and returned any
    constant already in ``GOLDEN-CHANGELOG.md`` would have defeated the
    real gate and left this control green.

    Here the pinned document is written out with one component digest
    changed, the run digest is recomputed over the file as it lands on
    disk, and the predicate the real gate uses is asserted in **both**
    directions: red for the moved golden, green for the checked-in one.
    """
    changelog_text = CHANGELOG_PATH.read_text(encoding="utf-8")
    pinned = _read_golden("run-digest.json")

    moved = json.loads(json.dumps(pinned))
    moved["components"]["population"] = "sha256:" + "ab" * 32
    moved_path = tmp_path / "run-digest.json"
    moved_path.write_text(json.dumps(moved, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    moved_digest = _run_digest_of(json.loads(moved_path.read_text(encoding="utf-8")))

    assert moved_digest != _run_digest_of(pinned), "the mutation did not move the run digest"
    assert not _has_changelog_row(changelog_text, moved_digest), (
        "a golden that moved has a changelog row already -- the control cannot "
        "distinguish a moved golden from a documented one"
    )
    assert _has_changelog_row(changelog_text, _run_digest_of(pinned))
