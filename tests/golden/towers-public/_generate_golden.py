# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One-shot generator for the pinned towers golden set in this directory.

Not a test, and not collected by pytest (the filename matches neither
``test_*.py`` nor ``*_test.py``). Run it by hand, deliberately, whenever this
baseline has to move, and add a ``GOLDEN-CHANGELOG.md`` row in the same
commit (``tests/golden/test_towers_public_golden.py`` enforces the row):

    python tests/golden/towers-public/_generate_golden.py

It runs the public ``examples/towers-tenant-renewal`` bundle from the sibling
FLUX repo through apply alone, as ``tests/golden/telco/_generate_golden.py``
does for the public telco example, and writes:

- ``run-digest.json`` and ``seam-attestation.json``, masked the way the test
  masks them;
- ``row-count.json``, the number of renewal-risk rows emitted;
- ``replay.json``, the run id and the unmasked ``runDigest`` that
  ``fluxsim replay --assert-digest`` must reproduce.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from flux_engine.core.canonical import SELF_REFERENTIAL_FIELDS, mask_document  # noqa: E402
from flux_engine.stages.apply import apply  # noqa: E402
from flux_engine.stages.layout import RunLayout  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = REPO_ROOT.parent / "FLUX" / "examples" / "towers-tenant-renewal"
EXPOSE_ID = "tenant_renewal_risk_moment"

#: The same mask ``tests/golden/test_towers_public_golden.py`` applies.
_EXTRA_VOLATILE_FIELDS = frozenset({"committedAt", "signature", "signer", "C2"})


def _masked(document: dict) -> dict:
    return mask_document(
        document, self_referential=SELF_REFERENTIAL_FIELDS | _EXTRA_VOLATILE_FIELDS
    )


def _write(name: str, document: dict) -> None:
    (GOLDEN_DIR / name).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    if not BUNDLE_DIR.is_dir():
        raise SystemExit(
            f"the towers example is not checked out at {BUNDLE_DIR}; "
            "clone the FLUX repo beside this one"
        )
    state_dir = Path(tempfile.mkdtemp()) / ".flux"
    applied = apply(BUNDLE_DIR, state_dir=state_dir)
    if not applied.success:
        raise SystemExit(f"apply failed: {applied.stderr}")
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state_dir, run_id)

    lines = layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
    row_count = sum(1 for line in lines if line)
    if row_count <= 0:
        raise SystemExit(f"the towers example emitted {row_count} rows for {EXPOSE_ID}")

    run_digest = str(applied.artifacts["run_digest"])
    _write("run-digest.json", _masked(json.loads(layout.run_digest_path.read_text())))
    _write(
        "seam-attestation.json",
        _masked(json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text())),
    )
    _write("row-count.json", {f"emit-{EXPOSE_ID}": row_count})
    _write("replay.json", {"runDigest": run_digest, "runId": run_id})
    print(f"wrote {GOLDEN_DIR} ({row_count} rows, runDigest {run_digest})")


if __name__ == "__main__":
    main()
