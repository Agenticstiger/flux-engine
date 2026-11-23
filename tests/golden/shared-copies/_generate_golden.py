# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One-shot generator for the pinned shared-copies golden sets in this directory.

Not a test, and not collected by pytest. Run it by hand, deliberately, whenever a baseline
has to move, and add a ``GOLDEN-CHANGELOG.md`` row for each set that moved in the same commit
(``tests/golden/test_shared_copies_golden.py`` enforces the rows):

    python tests/golden/shared-copies/_generate_golden.py

It plans ``tests/fixtures/shared-copies`` under kernel 0.2.0 (``fluxsim plan --kernel 0.2.0``)
twice and applies each plan: as the fixture is written, which declares a ``sharedColumns`` and a
``sharedAttribute`` and not ``copies: true`` (``without-copies/``), and with ``copies: true``
added under the Simulation's namespace (``with-copies/``). Each set holds the files
``tests/golden/related-star-v2/`` holds:

- ``run-digest.json``, masked the way the test masks it, whose ``emits`` map pins every row of
  the three tables;
- ``row-count.json``, the rows each table emitted;
- ``replay.json``, the run id and the unmasked ``runDigest`` that ``fluxsim replay
  --assert-digest`` must reproduce.
"""

from __future__ import annotations

import json
import shutil
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
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "shared-copies"
EXPOSE_IDS = ("customer", "account", "invoice")
KERNEL = "0.2.0"
_NAMESPACE = "  com.agentics.flux/1:\n"


def _write(directory: Path, name: str, document: dict) -> None:
    (directory / name).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _bundle(workdir: Path, *, copies: bool) -> Path:
    """The fixture, with ``copies: true`` under the namespace when *copies*."""
    target = workdir / ("with-copies" if copies else "without-copies")
    shutil.copytree(FIXTURE_DIR, target)
    if copies:
        path = target / "simulation.flux.yml"
        text = path.read_text(encoding="utf-8")
        assert text.count(_NAMESPACE) == 1
        path.write_text(text.replace(_NAMESPACE, _NAMESPACE + "    copies: true\n"), "utf-8")
    return target


def _pin(workdir: Path, *, copies: bool) -> None:
    bundle = _bundle(workdir, copies=copies)
    directory = GOLDEN_DIR / bundle.name
    directory.mkdir(exist_ok=True)
    state_dir = workdir / f"{bundle.name}-state" / ".flux"
    planned = plan(bundle, state_dir=state_dir, kernel=KERNEL)
    if not planned.success:
        raise SystemExit(f"plan failed: {planned.stderr}")
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, run_id=run_id, state_dir=state_dir, auto_plan=False)
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
    print(f"wrote {directory} ({counts}, runDigest {run_digest})")


def main() -> None:
    workdir = Path(tempfile.mkdtemp())
    for copies in (False, True):
        _pin(workdir, copies=copies)


if __name__ == "__main__":
    main()
