# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One-shot generator for the pinned golden artifacts under this directory.

Not a test, and not run by pytest (pytest's default ``test_*.py``/``*_test.py``
collection pattern does not match this filename). Run by hand, deliberately,
whenever the golden baseline needs to move -- and paired with a
``GOLDEN-CHANGELOG.md`` row (enforced by ``tests/golden/test_telco_golden.py``
and ``tests/golden/test_telco_public_golden.py``) every time it is:

    python tests/golden/telco/_generate_golden.py

It writes two golden sets:

- ``tests/golden/telco/`` -- the full ``tests/fixtures/telco-fidelity``
  bundle (the 25k-subject population -- KERNEL-SPEC's own comparator size)
  run through apply + ship, pinning ``plan.json`` (now the slimmed,
  ``seed-paths.ndjson``-backed shape), ``run-digest.json``,
  ``seam-attestation.json``, ``fidelity-record.json`` (now under the
  manifest's ``extensions`` entry), and ``component-digests.json``.
- ``tests/golden/telco-public/`` -- the public
  ``examples/telco-payment-recovery`` bundle (sibling FLUX repo) run
  through apply only (no fidelity block is declared there), pinning
  ``run-digest.json`` and ``seam-attestation.json`` from a journey-driven
  emit with rows > 0.

Both sets mask the same volatile fields their matching test modules mask
before comparing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from flux_engine.core.canonical import SELF_REFERENTIAL_FIELDS, mask_document  # noqa: E402
from flux_engine.stages.apply import apply  # noqa: E402
from flux_engine.stages.layout import RunLayout  # noqa: E402
from flux_engine.stages.ship import ship  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent
PUBLIC_GOLDEN_DIR = GOLDEN_DIR.parent / "telco-public"
BUNDLE_DIR = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity"
FLUX_REPO = REPO_ROOT.parent / "FLUX"
PUBLIC_BUNDLE_DIR = FLUX_REPO / "examples" / "telco-payment-recovery"
EXPOSE_ID = "payment_recovery_moment"


#: Fields masked on top of KERNEL-SPEC's own SELF_REFERENTIAL_FIELDS,
#: specific to this golden: a fidelity record's ``committedAt`` is a real
#: wall-clock timestamp (ship() stamps it with ``datetime.now()``), its
#: ``signature`` -- and, since security-3, the sibling ``signer`` block
#: naming the key that produced it -- is produced by a fresh ephemeral
#: ed25519 key every ship() call (flux_engine.stages.ship._ephemeral_key),
#: and ``C2`` commits over those -- none is reproducible across two runs
#: of this generator, and none is part of what the golden is pinning (the
#: *statistical outcome*, not a wall-clock fact or a throwaway signing key).
_EXTRA_VOLATILE_FIELDS = frozenset({"committedAt", "signature", "signer", "C2"})


def _masked(document: dict) -> dict:
    return mask_document(
        document, self_referential=SELF_REFERENTIAL_FIELDS | _EXTRA_VOLATILE_FIELDS
    )


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


#: lineage.ndjson carries genuine OpenLineage ``eventTime`` wall-clock
#: timestamps on every line (see flux_engine.lineage) -- volatile the same
#: way ``generated_at`` is, but never itself a JSON *object* key that
#: ``mask_document`` would strip on its own line, since each ndjson line
#: is masked independently here rather than run through ``mask_document``'s
#: single-document recursion.
_NDJSON_VOLATILE_FIELDS = SELF_REFERENTIAL_FIELDS | frozenset({"eventTime"})


def _sha256_masked_ndjson(path: Path) -> str:
    """sha256 of *path* with every line's volatile JSON fields masked first.

    Rows that are not JSON objects (population.ndjson's rows are JSON
    *arrays* -- ordinal-addressed, no volatile fields to mask by KERNEL-
    SPEC's own generator design) hash their raw line unchanged.
    """
    import hashlib

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


def _generate_fidelity_bundle() -> None:
    import tempfile

    state_dir = Path(tempfile.mkdtemp()) / ".flux"

    applied = apply(BUNDLE_DIR, state_dir=state_dir)
    if not applied.success:
        raise SystemExit(f"apply failed: {applied.stderr}")
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state_dir, run_id)

    # A committed slice built from the run's own emitted rows -- see
    # tests/stages/test_verify_ship_replay.py's matching_committed_slice_uri
    # for why this is the deterministic way to reach a *passed* fidelity leg
    # in a fixture build, not a claim about real-data fidelity.
    columns = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text())["columns"]
    lines = [
        line
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    rows = [dict(zip(columns, json.loads(line), strict=True)) for line in lines]
    slice_path = GOLDEN_DIR.parent / "_tmp_committed_slice.ndjson"
    slice_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    # ship() runs the fidelity gate (writing the fidelity record -- now
    # nested under the manifest's ``extensions`` entry -- to the run
    # directory) *before* it hands the staged bundle to the vendored
    # packer's own schema validation -- which this fixture bundle cannot
    # pass (see tests/stages/test_verify_ship_replay.py's
    # test_ship_writes_evidence_md_before_packing_and_captures_pack_stdout,
    # which stubs the packer for exactly this reason). The fidelity record
    # this golden pins is written well before that unrelated failure, so a
    # failed ship() here is expected and the record is still read back off
    # disk.
    out_dir = state_dir.parent / "dist"
    shipped = ship(
        BUNDLE_DIR,
        run_id=run_id,
        out_dir=out_dir,
        state_dir=state_dir,
        committed_slice={EXPOSE_ID: f"file://{slice_path}"},
    )
    slice_path.unlink(missing_ok=True)
    if not layout.fidelity_record_path(EXPOSE_ID).is_file():
        raise SystemExit(f"ship never reached the fidelity gate: {shipped.stderr}")

    plan_document = json.loads(layout.plan_path.read_text())
    run_digest_document = json.loads(layout.run_digest_path.read_text())
    seam_attestation = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text())
    fidelity_record = json.loads(layout.fidelity_record_path(EXPOSE_ID).read_text())

    component_digests = {
        "population": _sha256_masked_ndjson(layout.population_path),
        "events": _sha256_masked_ndjson(layout.events_path),
        "decisions": _sha256_masked_ndjson(layout.decisions_path),
        "suppressions": _sha256_masked_ndjson(layout.suppressions_path),
        "lineage": _sha256_masked_ndjson(layout.lineage_path),
        f"emit-{EXPOSE_ID}": _sha256_file(layout.emit_path(EXPOSE_ID)),
    }

    (GOLDEN_DIR / "plan.json").write_text(
        json.dumps(_masked(plan_document), indent=2, sort_keys=True) + "\n"
    )
    (GOLDEN_DIR / "run-digest.json").write_text(
        json.dumps(_masked(run_digest_document), indent=2, sort_keys=True) + "\n"
    )
    (GOLDEN_DIR / "seam-attestation.json").write_text(
        json.dumps(_masked(seam_attestation), indent=2, sort_keys=True) + "\n"
    )
    (GOLDEN_DIR / "fidelity-record.json").write_text(
        json.dumps(_masked(fidelity_record), indent=2, sort_keys=True) + "\n"
    )
    (GOLDEN_DIR / "component-digests.json").write_text(
        json.dumps(component_digests, indent=2, sort_keys=True) + "\n"
    )
    print("wrote golden artifacts to", GOLDEN_DIR)


def _generate_public_bundle() -> None:
    """Pins the public ``examples/telco-payment-recovery`` example's
    apply-only artifacts -- no fidelity block is declared there, so there
    is no ship() leg to reach; the point of this golden set is proving the
    journey-driven emit path produces non-empty, reproducible rows on the
    example every README walkthrough actually runs.
    """
    import tempfile

    if not PUBLIC_BUNDLE_DIR.is_dir():
        raise SystemExit(
            f"public example not found at {PUBLIC_BUNDLE_DIR} -- "
            "is the sibling FLUX repo checked out?"
        )

    state_dir = Path(tempfile.mkdtemp()) / ".flux"
    applied = apply(PUBLIC_BUNDLE_DIR, state_dir=state_dir)
    if not applied.success:
        raise SystemExit(f"apply failed: {applied.stderr}")
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state_dir, run_id)

    emit_path = layout.emit_path(EXPOSE_ID)
    row_count = sum(1 for line in emit_path.read_text(encoding="utf-8").splitlines() if line)
    if row_count <= 0:
        raise SystemExit(
            f"public example emitted {row_count} rows for {EXPOSE_ID} -- "
            "expected the journey-driven emit path to produce rows > 0"
        )

    run_digest_document = json.loads(layout.run_digest_path.read_text())
    seam_attestation = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text())

    PUBLIC_GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (PUBLIC_GOLDEN_DIR / "run-digest.json").write_text(
        json.dumps(_masked(run_digest_document), indent=2, sort_keys=True) + "\n"
    )
    (PUBLIC_GOLDEN_DIR / "seam-attestation.json").write_text(
        json.dumps(_masked(seam_attestation), indent=2, sort_keys=True) + "\n"
    )
    (PUBLIC_GOLDEN_DIR / "row-count.json").write_text(
        json.dumps({f"emit-{EXPOSE_ID}": row_count}, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote golden artifacts to {PUBLIC_GOLDEN_DIR} ({row_count} rows)")


def main() -> None:
    _generate_fidelity_bundle()
    _generate_public_bundle()


if __name__ == "__main__":
    main()
