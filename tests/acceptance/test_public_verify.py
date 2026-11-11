# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Ship a bundle, then verify the resulting archive with nothing but the
PUBLIC ``FLUX/scripts/bundle.py verify`` -- no engine, no trust in the
packer.

Two subprocess boundaries make this a real proof rather than an in-process
convenience check:

1. ``ship()`` runs in-process (this engine, the packer it wraps).
2. ``bundle.py verify`` runs as a **subprocess**, launched with a
   ``PYTHONPATH`` that carries only the venv's third-party
   ``site-packages`` (jsonschema, pyyaml, ...) and *not* this repo's own
   editable-install path, and with ``-S`` (skip ``site`` module
   initialisation) so the venv's own ``__editable__.flux_simulation_
   engine*.pth`` -- which unconditionally appends this repo's root to
   ``sys.path`` -- is never processed. ``test_flux_engine_is_genuinely_not_
   importable_in_the_verify_subprocess`` proves the isolation itself,
   not just that the command happened to work.

Uses the FLUX spec repo's own public example
(``FLUX/examples/telco-payment-recovery``), not this repo's
``tests/fixtures/telco-fidelity`` copy: that fixture's ``blueprint.flux.yml``
carries a dangling topology reference the vendored packer's own schema
validation correctly refuses to pack (see
``tests/stages/test_verify_ship_replay.py``'s
``test_ship_writes_evidence_md_before_packing_and_captures_pack_stdout``,
which stubs the packer for exactly that reason) -- so it can never produce
a real, publicly-verifiable archive. The public example declares no
``fidelity`` block on its emit, so ``ship()`` needs no ``--committed-slice``
to reach a successful pack.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLUX_SPEC_REPO = REPO_ROOT.parent / "FLUX"
PUBLIC_EXAMPLE = FLUX_SPEC_REPO / "examples" / "telco-payment-recovery"
BUNDLE_SCRIPT = FLUX_SPEC_REPO / "scripts" / "bundle.py"

pytestmark = pytest.mark.integration

requires_flux_spec_checkout = pytest.mark.skipif(
    not (PUBLIC_EXAMPLE.is_dir() and BUNDLE_SCRIPT.is_file()),
    reason=f"sibling FLUX spec checkout not found at {FLUX_SPEC_REPO}",
)


def _isolated_site_packages() -> str:
    """The one directory an isolated (``-S``) subprocess needs on
    ``PYTHONPATH`` for ``bundle.py``'s own dependencies (jsonschema,
    pyyaml) -- deliberately *not* this repo's root, which is where the
    editable install's ``.pth`` file (processed only when ``site`` runs)
    would otherwise put ``flux_engine`` on ``sys.path``.
    """
    # sys.prefix, not sys.executable.resolve() -- the venv's own bin/python
    # is a symlink to a uv-managed base interpreter, and resolving it walks
    # straight past this venv's site-packages to that base interpreter's
    # own lib/ directory (which has neither flux_engine nor bundle.py's
    # third-party deps installed).
    site_dir = Path(sys.prefix) / "lib"
    candidates = list(site_dir.glob("python3.*/site-packages"))
    assert candidates, f"no site-packages found under {site_dir}"
    return str(candidates[0])


def _isolated_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = _isolated_site_packages()
    return env


def _run_isolated(args: List[str], *, cwd: Path) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-S", *args]
    return subprocess.run(argv, cwd=cwd, env=_isolated_env(), capture_output=True, text=True)


@requires_flux_spec_checkout
def test_flux_engine_is_genuinely_not_importable_in_the_verify_subprocess() -> None:
    """The isolation this module's other test relies on, proved directly:
    without it, ``bundle.py verify`` succeeding would prove nothing about
    public, engine-free verifiability.
    """
    result = _run_isolated(["-c", "import flux_engine"], cwd=FLUX_SPEC_REPO)
    assert result.returncode != 0
    assert "flux_engine" in result.stderr
    assert "No module named" in result.stderr


@requires_flux_spec_checkout
def test_shipped_archive_verifies_with_the_public_bundler_alone(tmp_path: Path) -> None:
    from flux_engine.stages.apply import apply
    from flux_engine.stages.ship import ship

    state_dir = tmp_path / ".flux"
    out_dir = tmp_path / "dist"

    applied = apply(PUBLIC_EXAMPLE, state_dir=state_dir)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])

    shipped = ship(PUBLIC_EXAMPLE, run_id=run_id, out_dir=out_dir, state_dir=state_dir)
    assert shipped.success, shipped.stderr
    archive = Path(shipped.artifacts["archive"])
    assert archive.is_file(), archive

    result = _run_isolated([str(BUNDLE_SCRIPT), "verify", str(archive)], cwd=FLUX_SPEC_REPO)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "merkle root verified" in result.stdout
    assert "attestation reproduced" in result.stdout


@requires_flux_spec_checkout
def test_a_tampered_archive_fails_public_verification(tmp_path: Path) -> None:
    """A negative control: the public verifier must be doing real work, not
    rubber-stamping any ``.flux.tgz`` it is handed.
    """
    import tarfile

    from flux_engine.stages.apply import apply
    from flux_engine.stages.ship import ship

    state_dir = tmp_path / ".flux"
    out_dir = tmp_path / "dist"

    applied = apply(PUBLIC_EXAMPLE, state_dir=state_dir)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    shipped = ship(PUBLIC_EXAMPLE, run_id=run_id, out_dir=out_dir, state_dir=state_dir)
    assert shipped.success, shipped.stderr
    archive = Path(shipped.artifacts["archive"])

    tampered = tmp_path / "tampered.flux.tgz"
    tampered.write_bytes(archive.read_bytes())
    with tarfile.open(tampered, "r:gz") as tf:
        first_member = next(m for m in tf.getmembers() if m.isfile())
        original_bytes = tf.extractfile(first_member).read()  # type: ignore[union-attr]

    # Flip one byte of one archived file, re-pack with the same member
    # metadata (name, size) so the archive still opens, and confirm the
    # public verifier's Merkle-root/manifest check catches the tamper.
    mutated_bytes = bytes([original_bytes[0] ^ 0xFF]) + original_bytes[1:]
    import io

    with (
        tarfile.open(tampered, "r:gz") as src,
        tarfile.open(tmp_path / "tampered-rebuilt.flux.tgz", "w:gz") as dst,
    ):
        for member in src.getmembers():
            data = src.extractfile(member).read() if member.isfile() else b""  # type: ignore[union-attr]
            if member.name == first_member.name:
                data = mutated_bytes
            dst.addfile(member, io.BytesIO(data)) if member.isfile() else dst.addfile(member)

    result = _run_isolated(
        [str(BUNDLE_SCRIPT), "verify", str(tmp_path / "tampered-rebuilt.flux.tgz")],
        cwd=FLUX_SPEC_REPO,
    )
    assert result.returncode != 0
