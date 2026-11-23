# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The truth: a contract and a bundle that generates it, and how to run them.

The bundle lives in ``truth/`` beside this package. Its Simulation pins the contract's
digest, so editing the contract needs :func:`repin` before the bundle runs again.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

from flux_engine.core.digest import digest_of_document
from flux_engine.seam.resolve import load_documents_exact

TRUTH_DIR = Path(__file__).resolve().parents[1] / "truth"
CONTRACT = "contract.fluid.yaml"
PRODUCT = "b2b.billing"
TABLES = ("organisation", "account", "invoice")
#: Each relationship: child table, its foreign key, the parent table and the parent's key.
RELATIONSHIPS = (
    ("account", "org_id", "organisation", "org_id"),
    ("invoice", "account_id", "account", "account_id"),
)
_PINNED = re.compile(r'contractDigest: "(sha256:[0-9a-f]{64})"')


def contract_digest(bundle: Path) -> str:
    """The digest the Simulation must pin for the contract in *bundle*."""
    documents = load_documents_exact(bundle / CONTRACT)
    (document,) = [doc for doc in documents if doc.get("id") == PRODUCT]
    return digest_of_document(document, subject=f"contractDigest:{PRODUCT}").value


def pinned_digests(bundle: Path) -> List[str]:
    """Every contract digest the bundle's Simulation pins (one per emit)."""
    text = (bundle / "simulation.flux.yml").read_text(encoding="utf-8")
    return _PINNED.findall(text)


def repin(bundle: Path) -> str:
    """Write the contract's current digest into the Simulation; return it."""
    digest = contract_digest(bundle)
    simulation = bundle / "simulation.flux.yml"
    text = _PINNED.sub(f'contractDigest: "{digest}"', simulation.read_text(encoding="utf-8"))
    simulation.write_text(text, encoding="utf-8")
    return digest


def fluxsim(args: Sequence[str], *, cwd: Path) -> Dict[str, Any]:
    """Run ``fluxsim`` with ``--json`` and return its envelope.

    A non-zero exit is not an error here: ``run --verify`` exits 1 on a failed
    leg and still prints its whole envelope.
    """
    beside = Path(sys.executable).parent / "fluxsim"
    done = subprocess.run(
        [str(beside) if beside.exists() else "fluxsim", *args, "--json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        envelope = json.loads(done.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"fluxsim {' '.join(args)} exited {done.returncode} without JSON:\n"
            f"{done.stdout[-2000:]}\n{done.stderr[-2000:]}"
        ) from error
    envelope["_exit"] = done.returncode
    return envelope
