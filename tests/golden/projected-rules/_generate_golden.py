# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One-shot generator for the pinned projected-rules golden set in this directory.

Not a test, and not collected by pytest. Run it by hand, deliberately, whenever a baseline
has to move, and add a ``GOLDEN-CHANGELOG.md`` row for the set in the same commit
(``tests/golden/test_projected_rules_golden.py`` enforces the row):

    python tests/golden/projected-rules/_generate_golden.py

It plans ``tests/fixtures/projected-rules`` as it is written, a history with a snapshot, an events
emit and a periodic emit whose contract rules read the cells the projection sets, with
``fluxsim plan --kernel 0.2.0``, applies the plan, and writes ``kernel-0.2.0/``:

- ``run-digest.json``, masked the way the test masks it, whose ``emits`` map pins every row of
  the six tables;
- ``row-count.json``, the rows each table emitted;
- ``replay.json``, the run id and the unmasked ``runDigest`` that ``fluxsim replay
  --assert-digest`` must match.
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
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "projected-rules"
EXPOSE_IDS = (
    "depot",
    "service_visit",
    "vehicle_version",
    "vehicle_current",
    "vehicle_change",
    "vehicle_month",
)
KERNEL = "0.2.0"


def _write(directory: Path, name: str, document: dict) -> None:
    (directory / name).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    directory = GOLDEN_DIR / f"kernel-{KERNEL}"
    directory.mkdir(parents=True, exist_ok=True)
    state_dir = Path(tempfile.mkdtemp()) / ".flux"
    planned = plan(FIXTURE_DIR, state_dir=state_dir, kernel=KERNEL)
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
    assert document["kernelVersion"] == KERNEL, document["kernelVersion"]
    _write(
        directory,
        "run-digest.json",
        mask_document(document, self_referential=SELF_REFERENTIAL_FIELDS),
    )
    _write(directory, "row-count.json", counts)
    _write(directory, "replay.json", {"runDigest": run_digest, "runId": run_id})
    print(f"wrote {directory} ({counts}, runId {run_id}, runDigest {run_digest})")


if __name__ == "__main__":
    main()
