# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the public towers example's apply-only run to ``tests/golden/towers-public/``.

The towers counterpart of ``tests/golden/test_telco_public_golden.py``. It runs
``examples/towers-tenant-renewal`` from the sibling FLUX repo (tenant renewal
risk under rising SLA breaches, emitting for a FLUID 0.7.6 contract) through
``apply`` and pins three things:

* the run's masked ``run-digest.json`` and seam attestation, like the telco set;
* a non-zero row count for ``tenant_renewal_risk_moment``;
* the unmasked ``runDigest`` in ``replay.json``, which ``fluxsim replay
  --assert-digest`` must reproduce. That is the determinism claim for this
  bundle as the command a user runs, not only as artifacts that compare equal.

Regenerate with ``python tests/golden/towers-public/_generate_golden.py`` and add
a ``GOLDEN-CHANGELOG.md`` row in the same commit;
``test_a_golden_change_requires_a_changelog_row`` enforces the row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.cli import main
from flux_engine.core.canonical import SELF_REFERENTIAL_FIELDS, mask_document
from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.replay import replay
from tests.flux_examples import skip_or_fail_without_flux_examples

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = "towers-tenant-renewal"
GOLDEN_DIR = Path(__file__).resolve().parent / "towers-public"
CHANGELOG_PATH = REPO_ROOT / "GOLDEN-CHANGELOG.md"
EXPOSE_ID = "tenant_renewal_risk_moment"

_EXTRA_VOLATILE_FIELDS = frozenset({"committedAt", "signature", "signer", "C2"})

pytestmark = pytest.mark.integration


def _masked(document: Dict[str, Any]) -> Dict[str, Any]:
    return mask_document(
        document, self_referential=SELF_REFERENTIAL_FIELDS | _EXTRA_VOLATILE_FIELDS
    )


def _read_golden(name: str) -> Dict[str, Any]:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bundle_dir() -> Path:
    return skip_or_fail_without_flux_examples(EXAMPLE)


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden-towers-public") / ".flux"


@pytest.fixture(scope="module")
def run_id(bundle_dir: Path, state_dir: Path) -> str:
    applied = apply(bundle_dir, state_dir=state_dir)
    assert applied.success, applied.stderr
    return str(applied.artifacts["run_id"])


@pytest.fixture(scope="module")
def layout(state_dir: Path, run_id: str) -> RunLayout:
    return RunLayout.for_state_dir(state_dir, run_id)


def test_run_digest_matches_the_pinned_baseline(layout: RunLayout) -> None:
    actual = _masked(json.loads(layout.run_digest_path.read_text(encoding="utf-8")))
    assert actual == _read_golden("run-digest.json")


def test_seam_attestation_matches_the_pinned_baseline(layout: RunLayout) -> None:
    actual = _masked(
        json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    )
    assert actual == _read_golden("seam-attestation.json")


def test_the_seam_is_a_fluid_0_7_6_contract(layout: RunLayout) -> None:
    attestation = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    assert attestation["reference"]["fluidVersion"] == "0.7.6"


def test_emitted_row_count_matches_the_pinned_baseline_and_is_nonzero(
    layout: RunLayout,
) -> None:
    lines = [
        line
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    expected = _read_golden("row-count.json")[f"emit-{EXPOSE_ID}"]
    assert expected > 0, "pinned row count must itself be > 0"
    assert len(lines) == expected


def test_the_run_id_and_run_digest_are_the_pinned_ones(run_id: str, layout: RunLayout) -> None:
    pinned = _read_golden("replay.json")
    assert run_id == pinned["runId"]
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    assert digest_of_document(document, subject="runDigest").value == pinned["runDigest"]


def test_replay_reproduces_the_pinned_run_digest(
    bundle_dir: Path, state_dir: Path, run_id: str
) -> None:
    replayed = replay(
        bundle_dir,
        run_id=run_id,
        assert_digest=_read_golden("replay.json")["runDigest"],
        state_dir=state_dir,
    )
    assert replayed.success, replayed.stderr


def test_the_replay_command_accepts_the_pinned_digest_and_refuses_another(
    bundle_dir: Path, state_dir: Path, run_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned = _read_golden("replay.json")["runDigest"]
    base = ["replay", str(bundle_dir), "--run-id", run_id, "--state-dir", str(state_dir)]
    assert main([*base, "--assert-digest", pinned]) == 0
    assert "matches asserted digest" in capsys.readouterr().out
    other = "sha256:" + ("0" if pinned[-1] != "0" else "1") * 64
    assert main([*base, "--assert-digest", other]) != 0


def _has_changelog_row(changelog_text: str, digest: str) -> bool:
    """The one predicate both the gate and its control run."""
    return digest in changelog_text


def _run_digest_of(document: Dict[str, Any]) -> str:
    return digest_of_document(document, subject="runDigest").value


def test_the_pinned_baseline_has_a_changelog_row() -> None:
    changelog_text = CHANGELOG_PATH.read_text(encoding="utf-8")
    digest = _run_digest_of(_read_golden("run-digest.json"))
    assert _has_changelog_row(changelog_text, digest), (
        f"{CHANGELOG_PATH.name} has no row for {digest} -- "
        "tests/golden/towers-public/run-digest.json changed with no changelog row"
    )


def test_a_golden_change_requires_a_changelog_row(tmp_path: Path) -> None:
    """Move one byte of the pinned document and prove the gate turns red for it."""
    changelog_text = CHANGELOG_PATH.read_text(encoding="utf-8")
    pinned = _read_golden("run-digest.json")

    moved = json.loads(json.dumps(pinned))
    moved["components"]["population"] = "sha256:" + "ab" * 32
    moved_path = tmp_path / "run-digest.json"
    moved_path.write_text(json.dumps(moved, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    moved_digest = _run_digest_of(json.loads(moved_path.read_text(encoding="utf-8")))

    assert moved_digest != _run_digest_of(pinned), "the mutation did not move the run digest"
    assert not _has_changelog_row(changelog_text, moved_digest)
    assert _has_changelog_row(changelog_text, _run_digest_of(pinned))
