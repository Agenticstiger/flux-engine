# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One-shot generator for the pinned related-tables golden set in this directory.

Not a test, and not collected by pytest (the filename matches neither
``test_*.py`` nor ``*_test.py``). Run it by hand, deliberately, whenever this
baseline has to move, and add a ``GOLDEN-CHANGELOG.md`` row in the same
commit (``tests/golden/test_related_star_golden.py`` enforces the row):

    python tests/golden/related-star/_generate_golden.py

It runs ``tests/fixtures/related-star`` (five related tables of a FLUID 0.7.7
store star, all generated from statistics) through apply alone and writes:

- ``run-digest.json``, masked the way the test masks it. Its ``emits`` map
  holds every table's ``outputDigest``, so it pins every row;
- ``row-count.json``, the rows each table emitted;
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
BUNDLE_DIR = REPO_ROOT / "tests" / "fixtures" / "related-star"
EXPOSE_IDS = ("date_dim", "item", "customer", "store_sales", "store_returns")


def _write(name: str, document: dict) -> None:
    (GOLDEN_DIR / name).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    state_dir = Path(tempfile.mkdtemp()) / ".flux"
    applied = apply(BUNDLE_DIR, state_dir=state_dir)
    if not applied.success:
        raise SystemExit(f"apply failed: {applied.stderr}")
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state_dir, run_id)
    counts = {}
    for expose_id in EXPOSE_IDS:
        lines = layout.emit_path(expose_id).read_text(encoding="utf-8").splitlines()
        counts[f"emit-{expose_id}"] = sum(1 for line in lines if line)
    run_digest = str(applied.artifacts["run_digest"])
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    _write("run-digest.json", mask_document(document, self_referential=SELF_REFERENTIAL_FIELDS))
    _write("row-count.json", counts)
    _write("replay.json", {"runDigest": run_digest, "runId": run_id})
    print(f"wrote {GOLDEN_DIR} ({counts}, runDigest {run_digest})")


if __name__ == "__main__":
    main()
