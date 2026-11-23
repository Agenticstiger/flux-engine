# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One-shot generator for the pinned versioned golden sets in this directory.

Not a test, and not collected by pytest. Run it by hand, deliberately, whenever a baseline
has to move, and add a ``GOLDEN-CHANGELOG.md`` row for each set that moved in the same commit
(``tests/golden/test_versioned_golden.py`` enforces the rows):

    python tests/golden/versioned/_generate_golden.py

It plans ``tests/fixtures/versioned`` as it is written, a bundle with a versioned expose (a
validity window and no history key) that declares none of ADR 0017's or ADR 0027's keys, under each
kernel (``fluxsim plan --kernel 0.1.0`` and ``--kernel 0.2.0``) and applies each plan, into
``with-validity-window/kernel-0.1.0/`` and ``with-validity-window/kernel-0.2.0/``. Each set holds:

- ``run-digest.json``, masked the way the test masks it, whose ``emits`` map pins every row of
  the six tables;
- ``row-count.json``, the rows each table emitted;
- ``replay.json``, the run id and the unmasked ``runDigest`` that ``fluxsim replay
  --assert-digest`` must reproduce.

The sets were computed on origin/main (1c317c0), never on the branch that pins them.
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
from flux_engine.stages.plan import plan  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "versioned"
EXPOSE_IDS = (
    "customer",
    "subscription_version",
    "invoice",
    "subscription_current",
    "subscription_change",
    "subscription_month",
)
KERNELS = ("0.1.0", "0.2.0")


def _write(directory: Path, name: str, document: dict) -> None:
    (directory / name).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _pin(workdir: Path, kernel: str) -> None:
    directory = GOLDEN_DIR / "with-validity-window" / f"kernel-{kernel}"
    directory.mkdir(parents=True, exist_ok=True)
    state_dir = workdir / f"kernel-{kernel}-state" / ".flux"
    planned = plan(FIXTURE_DIR, state_dir=state_dir, kernel=kernel)
    if not planned.success:
        raise SystemExit(f"plan failed: {planned.stderr}")
    run_id = str(planned.artifacts["run_id"])
    applied = apply(FIXTURE_DIR, run_id=run_id, state_dir=state_dir, auto_plan=False)
    if not applied.success:
        raise SystemExit(f"apply failed: {applied.stderr}")
    layout = RunLayout.for_state_dir(state_dir, run_id)
    counts = {}
    for expose_id in EXPOSE_IDS:
        lines = layout.emit_path(expose_id).read_text(encoding="utf-8").splitlines()
        counts[f"emit-{expose_id}"] = sum(1 for line in lines if line)
    run_digest = str(applied.artifacts["run_digest"])
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    assert document["kernelVersion"] == kernel, document["kernelVersion"]
    _write(
        directory,
        "run-digest.json",
        mask_document(document, self_referential=SELF_REFERENTIAL_FIELDS),
    )
    _write(directory, "row-count.json", counts)
    _write(directory, "replay.json", {"runDigest": run_digest, "runId": run_id})
    print(f"wrote {directory} ({counts}, runDigest {run_digest})")


def main() -> None:
    workdir = Path(tempfile.mkdtemp())
    for kernel in KERNELS:
        _pin(workdir, kernel)


if __name__ == "__main__":
    main()
