# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the telco example's run to the artifacts under ``tests/golden/telco/``.

Re-runs the full ``tests/fixtures/telco-fidelity`` bundle (the same 25k-
subject population KERNEL-SPEC's own examples use -- not the shrunken
``telco-fidelity-small`` copy the acceptance suite's fuzz tests use for
speed) through ``apply`` and ``ship``, and diffs the result against the
pinned baseline field by field, so a failure names exactly which document
and which field moved rather than "the golden test failed".

Regenerating the baseline is a deliberate, out-of-band act:
``python tests/golden/telco/_generate_golden.py``, followed by a new row
in ``GOLDEN-CHANGELOG.md`` -- enforced here by
``test_a_golden_change_requires_a_changelog_row``, never by this module
writing the row itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.canonical import SELF_REFERENTIAL_FIELDS, mask_document
from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity"
GOLDEN_DIR = Path(__file__).resolve().parent / "telco"
CHANGELOG_PATH = REPO_ROOT / "GOLDEN-CHANGELOG.md"
EXPOSE_ID = "payment_recovery_moment"

_EXTRA_VOLATILE_FIELDS = frozenset({"committedAt", "signature", "signer", "C2"})
_NDJSON_VOLATILE_FIELDS = SELF_REFERENTIAL_FIELDS | frozenset({"eventTime"})


def _masked(document: Dict[str, Any]) -> Dict[str, Any]:
    return mask_document(
        document, self_referential=SELF_REFERENTIAL_FIELDS | _EXTRA_VOLATILE_FIELDS
    )


def _read_golden(name: str) -> Dict[str, Any]:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _sha256_masked_ndjson(path: Path) -> str:
    digest = hashlib.sha256()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            masked_line = json.dumps(
                mask_document(parsed, self_referential=_NDJSON_VOLATILE_FIELDS),
                sort_keys=True,
            )
        else:
            masked_line = line
        digest.update(masked_line.encode("utf-8"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden-telco") / ".flux"


@pytest.fixture(scope="module")
def run_id(state_dir: Path) -> str:
    applied = apply(BUNDLE_DIR, state_dir=state_dir)
    assert applied.success, applied.stderr
    return str(applied.artifacts["run_id"])


@pytest.fixture(scope="module")
def layout(state_dir: Path, run_id: str) -> RunLayout:
    return RunLayout.for_state_dir(state_dir, run_id)


@pytest.fixture(scope="module")
def fidelity_record(
    state_dir: Path, run_id: str, layout: RunLayout, tmp_path_factory: pytest.TempPathFactory
) -> Dict[str, Any]:
    """Ships (which writes ``fidelity-record-<exposeId>.json`` before the
    vendored packer's own schema validation runs -- see
    ``tests/golden/telco/_generate_golden.py``'s module docstring for why a
    failed ``ship()`` here is expected and the record is still read off
    disk).
    """
    columns = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text())["columns"]
    lines = [
        line
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    rows = [dict(zip(columns, json.loads(line), strict=True)) for line in lines]
    slice_path = tmp_path_factory.mktemp("golden-slice") / "slice.ndjson"
    slice_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    ship(
        BUNDLE_DIR,
        run_id=run_id,
        out_dir=tmp_path_factory.mktemp("golden-dist"),
        state_dir=state_dir,
        committed_slice={EXPOSE_ID: f"file://{slice_path}"},
    )
    record_path = layout.fidelity_record_path(EXPOSE_ID)
    assert record_path.is_file(), "ship() never reached the fidelity gate"
    return json.loads(record_path.read_text(encoding="utf-8"))


def test_plan_matches_the_pinned_baseline(layout: RunLayout) -> None:
    actual = _masked(json.loads(layout.plan_path.read_text(encoding="utf-8")))
    expected = _read_golden("plan.json")
    assert actual == expected


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


def test_fidelity_record_matches_the_pinned_baseline(fidelity_record: Dict[str, Any]) -> None:
    actual = _masked(fidelity_record)
    expected = _read_golden("fidelity-record.json")
    assert actual == expected


def test_component_digests_match_the_pinned_baseline(layout: RunLayout) -> None:
    actual = {
        "population": _sha256_masked_ndjson(layout.population_path),
        "events": _sha256_masked_ndjson(layout.events_path),
        "decisions": _sha256_masked_ndjson(layout.decisions_path),
        "suppressions": _sha256_masked_ndjson(layout.suppressions_path),
        "lineage": _sha256_masked_ndjson(layout.lineage_path),
        f"emit-{EXPOSE_ID}": _sha256_file(layout.emit_path(EXPOSE_ID)),
    }
    expected = _read_golden("component-digests.json")
    assert actual == expected


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
    """The digest ``GOLDEN-CHANGELOG.md`` rows are keyed by: KERNEL-SPEC
    §11's own ``digest_of_document`` over the pinned, masked
    ``run-digest.json`` -- the same construction
    ``tests/stages/test_verify_ship_replay.py`` uses for ``fluxsim replay
    --assert-digest``.
    """
    return _run_digest_of(_read_golden("run-digest.json"))


def test_golden_changelog_exists_and_is_a_table() -> None:
    assert CHANGELOG_PATH.is_file(), CHANGELOG_PATH
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    assert "| runDigest | components | reason |" in text


def test_the_pinned_baseline_has_a_changelog_row() -> None:
    """CI rule: every ``runDigest`` the pinned ``run-digest.json`` produces
    must have a matching row in ``GOLDEN-CHANGELOG.md`` -- the same
    "no row, no update" discipline ``fluxsim verify --update-golden`` already
    enforces at runtime (``flux_engine.core.trace.check_golden_update``),
    applied here to the checked-in golden baseline itself. If this test
    fails, the pinned files moved (or were hand-edited) without a
    changelog row: regenerate deliberately and add the row, don't just
    make this test pass another way.
    """
    changelog_text = CHANGELOG_PATH.read_text(encoding="utf-8")
    digest = _run_digest_value()
    assert _has_changelog_row(changelog_text, digest), (
        f"{CHANGELOG_PATH.name} has no row for {digest} -- "
        "tests/golden/telco/run-digest.json changed with no changelog row"
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
