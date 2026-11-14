# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle that declares a fidelity block ships, unstubbed, and verifies.

SEAM-FIDELITY-SPEC §2 moved this engine's own declarations -- the fidelity
block, the declared row count, the column mapping -- out of
``Simulation.spec.emits[]`` and into RFC-02's document-level ``extensions``
map. ``spec.emits[]`` is closed (``additionalProperties: false``) in FLUX
0.5.0, so a bundle carrying them there was refused by the published
validator *and* by the packer, and ``ship`` could not complete on the one
fixture that declares a block.

This module is the proof that it can now. It runs the real gate against a
real committed slice, calls the real packer (nothing stubbed), and hands
the archive to the vendored verifier -- the same code the published
``scripts/bundle.py verify`` runs -- with **nothing stripped and nothing
relaxed**. The companion test at the bottom re-runs the published
validator itself when the FLUX repository is checked out beside this one.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flux_engine.concordance.boundary.vault import BlindingVault
from flux_engine.concordance.compiler import compile_fidelity
from flux_engine.concordance.predicate import expose_from_contract
from flux_engine.core.digest import build_plan_digest
from flux_engine.evidence.pack import verify_evidence
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.replay import replay
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify

from .conftest import FIXTURE_BUNDLE, read_json

#: The module, not the function of the same name that
#: ``flux_engine.stages`` re-exports over it.
ship_module = importlib.import_module("flux_engine.stages.ship")

REPO_ROOT = Path(__file__).resolve().parents[2]
FLUX_REPO = REPO_ROOT.parent / "FLUX"
EXPOSE_ID = "payment_recovery_moment"


@pytest.fixture(scope="module")
def bundle_dir() -> Path:
    assert FIXTURE_BUNDLE.is_dir(), FIXTURE_BUNDLE
    return FIXTURE_BUNDLE


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ship-fidelity") / ".flux"


@pytest.fixture(scope="module")
def run_id(bundle_dir: Path, state_dir: Path) -> str:
    result = apply(bundle_dir, state_dir=state_dir)
    assert result.success, result.stderr
    return str(result.artifacts["run_id"])


@pytest.fixture(scope="module")
def committed_slice_uri(
    tmp_path_factory: pytest.TempPathFactory, state_dir: Path, run_id: str
) -> str:
    """A committed slice built from the run's own emitted rows.

    Self-referential on purpose: it makes every declared property evaluate
    ``within`` whatever the kernel's numerics do, so the gate reaches its
    packing path deterministically. It claims nothing about real-data
    fidelity, and the boundary never sees a record it did not already
    hold.
    """
    layout = RunLayout.for_state_dir(state_dir, run_id)
    columns = read_json(layout.seam_attestation_path(EXPOSE_ID))["columns"]
    lines = [
        line
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    rows = [dict(zip(columns, json.loads(line), strict=True)) for line in lines]
    assert rows, "the fixture declares a block, so its rows are generated"
    path = tmp_path_factory.mktemp("committed") / "slice.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path.as_uri()


@pytest.fixture(scope="module")
def shipped(
    tmp_path_factory: pytest.TempPathFactory,
    bundle_dir: Path,
    state_dir: Path,
    run_id: str,
    committed_slice_uri: str,
):
    out_dir = tmp_path_factory.mktemp("shipped") / "dist"
    return ship(
        bundle_dir,
        run_id=run_id,
        out_dir=out_dir,
        state_dir=state_dir,
        committed_slice={EXPOSE_ID: committed_slice_uri},
    )


def test_a_bundle_that_declares_a_block_ships(shipped) -> None:
    """The defect: the packer refused the bundle, so ship never completed."""
    assert shipped.success, shipped.stderr
    assert not shipped.artifacts.get("events"), shipped.artifacts.get("events")


def test_the_archive_exists_and_carries_the_run(shipped) -> None:
    archive = Path(shipped.artifacts["archive"])
    assert archive.is_file() and archive.stat().st_size > 0


def test_the_packed_archive_verifies_with_nothing_relaxed(shipped) -> None:
    """The published read side, run over the archive this engine wrote."""
    assert verify_evidence(shipped.artifacts["archive"]) == 0


def test_the_blinding_value_is_in_none_of_what_ships(shipped, state_dir: Path, run_id: str) -> None:
    """§4: the blinding value never leaves the boundary -- so not into the pack.

    The boundary store lives at ``<state_dir>/boundary/``, a sibling of
    ``runs/``. Everything under the run directory is staged into the
    archive by ``pack_evidence``, so a value kept there would travel to
    every reader of the pack and blind nothing.
    """
    entries = BlindingVault.for_state_dir(state_dir).entries()
    assert entries, "the ship committed a state, so the store holds one"
    layout = RunLayout.for_state_dir(state_dir, run_id)

    archive = Path(shipped.artifacts["archive"])
    packed = b""
    names = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            names.append(member.name)
            stream = tar.extractfile(member) if member.isfile() else None
            if stream is not None:
                packed += stream.read()
    # The scan is over an archive that really does carry the record whose
    # C2 the blinding value sits behind -- not over an empty read.
    assert any(f"fidelity-record-{EXPOSE_ID}.json" in name for name in names), names
    assert packed

    run_bytes = b"".join(
        path.read_bytes() for path in sorted(layout.run_dir.rglob("*")) if path.is_file()
    )
    for entry in entries:
        for spelling in (
            entry.blinding,
            entry.blinding.hex().encode(),
            entry.blinding.hex().upper().encode(),
        ):
            assert spelling not in run_bytes, "a blinding value reached the run directory"
            assert spelling not in packed, "a blinding value reached the evidence pack"


def test_two_ships_of_one_run_pack_one_archive(
    tmp_path_factory: pytest.TempPathFactory,
    bundle_dir: Path,
    state_dir: Path,
    run_id: str,
    committed_slice_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One applied run, one slice, one signing key -> one archive, byte for byte.

    The packing instant is held fixed for both calls. ``EVIDENCE.md``
    records when the report was written, which is honestly different on a
    second ship and has nothing to do with the committed state; freezing
    it is the control that leaves the state's own quantities -- the
    blinding value behind ``C2``, the ``stateOrdinal``, the commitment
    time -- as the only things that could still move the Merkle root.
    Before the fix they moved on every call.
    """
    frozen = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(ship_module, "datetime", _Frozen)
    key = bytes(range(64, 96))

    def ship_once(name: str) -> tuple[bytes, str]:
        result = ship(
            bundle_dir,
            run_id=run_id,
            out_dir=tmp_path_factory.mktemp(name) / "dist",
            state_dir=state_dir,
            committed_slice={EXPOSE_ID: committed_slice_uri},
            signing_key=key,
        )
        assert result.success, result.stderr
        statement = read_json(Path(result.artifacts["runtimeStatement"]))
        return Path(result.artifacts["archive"]).read_bytes(), str(
            statement["predicate"]["merkleRoot"]
        )

    first_bytes, first_root = ship_once("reship-a")
    second_bytes, second_root = ship_once("reship-b")

    assert first_root == second_root
    assert first_bytes == second_bytes


def test_verify_and_ship_check_one_committed_state(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, bundle_dir: Path
) -> None:
    """Both legs commit through the same boundary store, so one C2, one state.

    Its own state directory: the module's shared one is committed by
    whichever test touches it first, and a record whose ``committedAt``
    depends on test order is not something to assert on.
    """
    from flux_engine.stages.verify import verify

    state = tmp_path / ".flux"
    applied = apply(bundle_dir, state_dir=state)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state, run_id)

    columns = read_json(layout.seam_attestation_path(EXPOSE_ID))["columns"]
    rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    slice_path = tmp_path / "slice.ndjson"
    slice_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    verified = verify(
        bundle_dir,
        run_id=run_id,
        state_dir=state,
        committed_slice={EXPOSE_ID: slice_path.as_uri()},
    )
    assert verified.success, verified.stderr
    assert verified.artifacts["fidelity"][EXPOSE_ID]["status"] == "passed"

    held = BlindingVault.for_state_dir(state).entries()
    assert len(held) == 1, "verify committed exactly one state"

    shipped_result = ship(
        bundle_dir,
        run_id=run_id,
        out_dir=tmp_path_factory.mktemp("verified-ship") / "dist",
        state_dir=state,
        committed_slice={EXPOSE_ID: slice_path.as_uri()},
    )
    assert shipped_result.success, shipped_result.stderr
    assert BlindingVault.for_state_dir(state).entries() == held, "ship formed a second state"

    record = read_json(layout.fidelity_record_path(EXPOSE_ID))
    assert record["committedState"]["ordinal"] == held[0].ordinal
    assert record["committedState"]["committedAt"] == held[0].committed_at


def test_the_plan_ship_took_records_the_block_the_gate_compiles(
    shipped, state_dir: Path, run_id: str
) -> None:
    """SEAM-FIDELITY-SPEC §3: the plan records every epsilon the gate then uses.

    The module's run is a fresh plan, taken by ``apply`` itself. Its emit
    entry carries the compiled block, and that block is what the gate
    compiles from the record ``apply`` wrote for ``verify`` and ``ship``.
    """
    assert shipped.success, shipped.stderr
    layout = RunLayout.for_state_dir(state_dir, run_id)
    recorded = read_json(layout.fidelity_input_path(EXPOSE_ID))
    expose = expose_from_contract(recorded["contract"], EXPOSE_ID)
    compiled = compile_fidelity(contract=recorded["contract"], emit=recorded["emit"], expose=expose)
    planned = read_json(layout.plan_path)["emits"][EXPOSE_ID]["fidelity"]
    assert planned == json.loads(json.dumps(compiled.plan_document()))


def test_a_plan_written_before_the_block_was_recorded_still_applies_and_ships(
    tmp_path: Path, bundle_dir: Path, state_dir: Path, run_id: str, committed_slice_uri: str
) -> None:
    """A plan without ``emits.<exposeId>.fidelity``, as 0.4.0 wrote it, still runs to a pack.

    No stage after ``plan`` reads the key. ``apply``, ``verify`` and
    ``replay`` check the plan against its own ``planDigest`` and run id, and
    ``ship`` pins ``H`` and ``G`` from the entry's own keys. The plan lives
    under its own run id, because the block moves the digest. The rows do not
    depend on ``planDigest``, so the module's slice still matches them.
    """
    fresh = RunLayout.for_state_dir(state_dir, run_id)
    document = read_json(fresh.plan_path)
    del document["emits"][EXPOSE_ID]["fidelity"]
    document["planDigest"] = build_plan_digest(document).ref.value
    state = tmp_path / ".flux"
    older = RunLayout.for_state_dir(state, run_id_for(document["planDigest"])).ensure()
    assert older.run_id != run_id, "the block moves the plan's digest, so its run id"
    older.plan_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    older.seed_paths_path.write_bytes(fresh.seed_paths_path.read_bytes())

    applied = apply(bundle_dir, run_id=older.run_id, state_dir=state)
    assert applied.success, applied.stderr
    receipt = read_json(older.receipt_path)
    assert receipt["planDigest"] == document["planDigest"]
    verified = verify(bundle_dir, run_id=older.run_id, state_dir=state)
    assert verified.success, verified.stderr
    replayed = replay(
        bundle_dir, run_id=older.run_id, assert_digest=receipt["runDigest"], state_dir=state
    )
    assert replayed.success, replayed.stderr
    shipped_older = ship(
        bundle_dir,
        run_id=older.run_id,
        out_dir=tmp_path / "dist",
        state_dir=state,
        committed_slice={EXPOSE_ID: committed_slice_uri},
    )
    assert shipped_older.success, shipped_older.stderr


@pytest.mark.skipif(
    not (FLUX_REPO / "scripts" / "validate.py").is_file(),
    reason=f"the FLUX specification repository is not checked out at {FLUX_REPO}",
)
def test_the_fixture_validates_with_the_published_validator(bundle_dir: Path) -> None:
    """The unmodified 0.5.0 schema accepts the bundle as authored."""
    completed = subprocess.run(  # noqa: S603 - a fixed, local script path
        [sys.executable, str(FLUX_REPO / "scripts" / "validate.py"), str(bundle_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(
    not (FLUX_REPO / "scripts" / "bundle.py").is_file(),
    reason=f"the FLUX specification repository is not checked out at {FLUX_REPO}",
)
def test_the_published_bundler_verifies_the_archive(shipped) -> None:
    completed = subprocess.run(  # noqa: S603 - a fixed, local script path
        [
            sys.executable,
            str(FLUX_REPO / "scripts" / "bundle.py"),
            "verify",
            str(shipped.artifacts["archive"]),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
