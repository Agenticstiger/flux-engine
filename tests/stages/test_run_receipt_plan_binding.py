# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A committed run directory is the one its plan describes, and holds only what the run wrote.

``receipt.json`` names the run's simulation and its emits, and so does ``plan.json``; ``verify``
and ``ship`` read both back and used to compare neither. A receipt that names another
simulation, or other emits, than the plan beside it is refused (``FLX-2122``), even when
``run-digest.json`` was rewritten to match it. ``ship`` packs the run directory whole, so a
file the run did not write is refused too (``FLX-2123``), and every file the engine does
write there is still accepted: shares, a sink receipt, and the outputs of ``verify`` and
``ship`` themselves.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from flux_engine.cli import main
from flux_engine.core.digest import digest_of_bytes, digest_of_document
from flux_engine.stages.layout import COMPONENT_LEDGER, RunLayout
from flux_engine.stages.verify import recompute_components

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

EXPOSE_ID = "payment_recovery_moment"


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> "tuple[int, str]":
    code = main([*argv])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.fixture()
def run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> RunLayout:
    state = tmp_path / ".flux"
    assert _cli(capsys, "plan", str(FIXTURE_BUNDLE), "--state-dir", str(state))[0] == 0
    assert _cli(capsys, "apply", str(FIXTURE_BUNDLE), "--state-dir", str(state))[0] == 0
    (run_dir,) = (state / "runs").iterdir()
    return RunLayout.for_state_dir(state, run_dir.name)


def _slice(layout: RunLayout, tmp_path: Path) -> str:
    """The run's own rows, as the committed slice the fidelity gate asks for."""
    attestation = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    rows = [
        dict(zip(attestation["columns"], json.loads(line), strict=True))
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    path = tmp_path / "slice.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return f"{EXPOSE_ID}=file://{path}"


def _verify(capsys: pytest.CaptureFixture[str], layout: RunLayout) -> "tuple[int, str]":
    return _cli(
        capsys,
        "verify",
        str(FIXTURE_BUNDLE),
        "--run-id",
        layout.run_id,
        "--state-dir",
        str(layout.state_dir),
    )


def _ship(
    capsys: pytest.CaptureFixture[str],
    layout: RunLayout,
    tmp_path: Path,
    out: str = "out",
    sliced: bool = True,
) -> "tuple[int, str]":
    return _cli(
        capsys,
        "ship",
        str(FIXTURE_BUNDLE),
        "--run-id",
        layout.run_id,
        "-o",
        str(tmp_path / out),
        "--state-dir",
        str(layout.state_dir),
        *(["--committed-slice", _slice(layout, tmp_path)] if sliced else []),
    )


def _receipt(layout: RunLayout) -> Dict[str, Any]:
    return json.loads(layout.receipt_path.read_text(encoding="utf-8"))


def _forge_receipt(layout: RunLayout, change: Callable[[Dict[str, Any]], None]) -> None:
    """Edit the receipt, then make ``run-digest.json`` and the receipt's run digest agree with it.

    The forger of the worst case: every record that is compared with the receipt is rewritten
    to match, so only the comparison with the plan can tell.
    """
    receipt = _receipt(layout)
    change(receipt)
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    document["components"] = recompute_components(layout, receipt)
    layout.run_digest_path.write_text(json.dumps(document), encoding="utf-8")
    receipt["runDigest"] = digest_of_document(document, subject="runDigest").value
    layout.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


# --- the receipt agrees with the plan ----------------------------------------


def test_a_receipt_naming_another_simulation_is_refused_by_verify_and_ship(
    run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = _receipt(run)
    receipt["simulationId"] = "another-simulation"
    run.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    code, text = _verify(capsys, run)
    assert code != 0
    assert "FLX-2122" in text and "simulationId" in text
    assert "another-simulation" in text and "sim-payment-recovery" in text

    code, text = _ship(capsys, run, tmp_path)
    assert code != 0
    assert "FLX-2122" in text and "simulationId" in text
    assert not (tmp_path / "out" / "EVIDENCE.md").exists()


def test_update_golden_writes_nothing_for_a_receipt_naming_another_simulation(
    run: RunLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = _receipt(run)
    receipt["simulationId"] = "another-simulation"
    run.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    code, text = _cli(
        capsys,
        "verify",
        str(FIXTURE_BUNDLE),
        "--run-id",
        run.run_id,
        "--state-dir",
        str(run.state_dir),
        "--update-golden",
        "--reason",
        "a receipt that names another simulation records no baseline",
    )
    assert code != 0 and "FLX-2122" in text
    assert not run.golden_dir.exists() or not list(run.golden_dir.iterdir())


@pytest.mark.parametrize("edit", ["renamed", "added", "removed"])
def test_a_receipt_whose_emits_differ_from_the_plans_is_refused_even_with_a_matching_run_digest(
    edit: str, run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def change(receipt: Dict[str, Any]) -> None:
        entry = receipt["emits"][EXPOSE_ID]
        if edit == "renamed":
            receipt["emits"] = {"renamed_moment": entry}
            shutil.copy(run.emit_path(EXPOSE_ID), run.emits_dir / "renamed_moment.ndjson")
        elif edit == "added":
            receipt["emits"]["extra_moment"] = dict(entry)
            shutil.copy(run.emit_path(EXPOSE_ID), run.emits_dir / "extra_moment.ndjson")
        else:
            receipt["emits"] = {}

    _forge_receipt(run, change)

    code, text = _verify(capsys, run)
    assert code != 0
    assert "FLX-2122" in text and "emits" in text

    # No slice is named: a receipt that has lost the emit a slice is meant for is refused
    # as a bad slice first, and what is asked here is the comparison with the plan.
    code, text = _ship(capsys, run, tmp_path, sliced=False)
    assert code != 0
    assert "FLX-2122" in text and "emits" in text
    assert not (tmp_path / "out" / "EVIDENCE.md").exists()


def test_a_receipt_only_emit_edit_is_still_refused(
    run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = _receipt(run)
    receipt["emits"]["extra_moment"] = dict(receipt["emits"][EXPOSE_ID])
    run.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    for code, text in (_verify(capsys, run), _ship(capsys, run, tmp_path)):
        assert code != 0
        assert "FLX-2110" in text or "FLX-2122" in text or "FLX-8001" in text


# --- the directory holds only files the run wrote ------------------------------


_STRAYS = (
    ("emits/extra_moment.ndjson", "emits/extra_moment.ndjson"),
    ("emits/extra_moment.shard-1-of-2.ndjson", "emits/extra_moment.shard-1-of-2.ndjson"),
    ("notes.txt", "notes.txt"),
    ("seam-attestation-extra_moment.json", "seam-attestation-extra_moment.json"),
    ("fidelity-record-extra_moment.json", "fidelity-record-extra_moment.json"),
    ("run-digest.json.bak", "run-digest.json.bak"),
    ("emits/deeper/x.ndjson", "emits/deeper/"),
    ("other-dir/x.json", "other-dir/"),
    ("weird\x1b[31mname.txt", "weird"),
)


@pytest.mark.parametrize(("relative", "named"), _STRAYS)
def test_a_file_the_receipt_does_not_name_is_refused_by_verify_and_ship(
    relative: str, named: str, run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stray = run.run_dir / relative
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("not the run's\n", encoding="utf-8")

    code, text = _verify(capsys, run)
    assert code != 0
    assert "FLX-2123" in text and named in text
    assert "\x1b" not in text

    code, text = _ship(capsys, run, tmp_path)
    assert code != 0
    assert "FLX-2123" in text and named in text
    assert not (tmp_path / "out" / "EVIDENCE.md").exists()


def _shared_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> RunLayout:
    """A run applied in two shares and then whole, the way `size-and-split-a-run` does it."""
    state = tmp_path / ".flux"
    bundle = str(FIXTURE_BUNDLE)
    assert _cli(capsys, "plan", bundle, "--state-dir", str(state), "--no-stage")[0] == 0
    (run_dir,) = (state / "runs").iterdir()
    layout = RunLayout.for_state_dir(state, run_dir.name)
    for share in ("1/2", "2/2", None):
        extra = ["--shard", share] if share is not None else []
        argv = ["apply", bundle, "--state-dir", str(state), "--run-id", layout.run_id, *extra]
        code, text = _cli(capsys, *argv)
        assert code == 0, text
    return layout


def _refused_everywhere(
    layout: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str], named: str
) -> None:
    for code, text in (_verify(capsys, layout), _ship(capsys, layout, tmp_path)):
        assert code != 0
        assert "FLX-2123" in text, text
        assert named in text, text


@pytest.mark.parametrize(
    "name",
    [
        f"emits/{EXPOSE_ID}.shard-7-of-3.ndjson",
        f"emits/{EXPOSE_ID}.shard-0-of-3.ndjson",
        f"emits/{EXPOSE_ID}.shard-1-of-2.ndjson",
        "shard-7-of-3.json",
        "shard-0-of-2.json",
    ],
)
def test_a_share_a_run_never_wrote_is_refused(
    name: str, run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (run.run_dir / name).write_text("[]\n", encoding="utf-8")
    _refused_everywhere(run, tmp_path, capsys, name)


def test_a_share_stream_without_its_manifest_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _shared_run(tmp_path, capsys)
    layout.shard_manifest_path(2, 2).unlink()
    _refused_everywhere(layout, tmp_path, capsys, f"{EXPOSE_ID}.shard-2-of-2.ndjson")


def test_a_share_stream_with_other_bytes_than_its_share_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _shared_run(tmp_path, capsys)
    share = layout.shard_emit_path(EXPOSE_ID, 1, 2)
    share.write_bytes(share.read_bytes() + b"[]\n")
    _refused_everywhere(layout, tmp_path, capsys, "shard-1-of-2")


def test_a_share_stream_swapped_for_the_other_share_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _shared_run(tmp_path, capsys)
    first = layout.shard_emit_path(EXPOSE_ID, 1, 2)
    second = layout.shard_emit_path(EXPOSE_ID, 2, 2)
    first.write_bytes(second.read_bytes())
    manifest = layout.shard_manifest_path(1, 2)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    entry = document["emits"][0]
    entry["bytes"] = first.stat().st_size
    entry["sha256"] = digest_of_bytes(first.read_bytes(), subject="shardFile").value
    entry["rows"] = first.read_bytes().count(b"\n")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    _refused_everywhere(layout, tmp_path, capsys, "shard-1-of-2")


def test_a_manifest_that_does_not_describe_its_share_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _shared_run(tmp_path, capsys)
    manifest = layout.shard_manifest_path(1, 2)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["emits"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(document), encoding="utf-8")
    _refused_everywhere(layout, tmp_path, capsys, manifest.name)


def test_a_changed_seed_path_list_is_refused_because_the_plan_binds_it(
    run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with run.seed_paths_path.open("ab") as handle:
        handle.write(b'["extra"]\n')
    for code, text in (_verify(capsys, run), _ship(capsys, run, tmp_path)):
        assert code != 0
        assert "FLX-2108" in text or "FLX-2123" in text, text


def test_a_directory_where_the_run_writes_a_file_is_named_as_such(
    run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (run.run_dir / "column-provenance.json").unlink()
    (run.run_dir / "column-provenance.json").mkdir()
    for code, text in (_verify(capsys, run), _ship(capsys, run, tmp_path)):
        assert code != 0
        assert "FLX-2123" in text
        assert "column-provenance.json/, a directory where the run writes a file" in text


def test_the_unnamed_file_is_named_by_its_relative_path(
    run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (run.emits_dir / "extra_moment.ndjson").write_text("[]\n", encoding="utf-8")
    for code, text in (_verify(capsys, run), _ship(capsys, run, tmp_path)):
        assert code != 0
        assert "FLX-2123" in text and "emits/extra_moment.ndjson" in text


# --- everything the engine writes there is still accepted ----------------------


def test_a_run_with_shares_and_a_sink_receipt_still_verifies_and_ships(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".flux"
    bundle = str(FIXTURE_BUNDLE)
    assert _cli(capsys, "plan", bundle, "--state-dir", str(state), "--no-stage")[0] == 0
    (run_dir,) = (state / "runs").iterdir()
    layout = RunLayout.for_state_dir(state, run_dir.name)
    for share in ("1/2", "2/2"):
        code, text = _cli(
            capsys,
            "apply",
            bundle,
            "--state-dir",
            str(state),
            "--run-id",
            layout.run_id,
            "--shard",
            share,
        )
        assert code == 0, text
    code, text = _cli(
        capsys,
        "apply",
        bundle,
        "--state-dir",
        str(state),
        "--run-id",
        layout.run_id,
        "--sink",
        f"arrow-ipc:{tmp_path / 'sink'}",
    )
    assert code == 0, text
    names = sorted(p.name for p in layout.run_dir.iterdir())
    assert "shard-1-of-2.json" in names and "sink-receipts.json" in names
    assert (layout.emits_dir / f"{EXPOSE_ID}.shard-2-of-2.ndjson").is_file()

    code, text = _verify(capsys, layout)
    assert code == 0, text
    code, text = _ship(capsys, layout, tmp_path)
    assert code == 0, text
    assert (tmp_path / "out" / "EVIDENCE.md").is_file() or any((tmp_path / "out").iterdir())

    # Ship wrote its own files into the run directory (the fidelity record and EVIDENCE.md):
    # a second verify and ship of the same run accept those too.
    assert (layout.run_dir / "EVIDENCE.md").is_file()
    assert layout.fidelity_record_path(EXPOSE_ID).is_file()
    code, text = _verify(capsys, layout)
    assert code == 0, text
    code, text = _ship(capsys, layout, tmp_path, out="out-again")
    assert code == 0, text


def test_a_staged_plan_leaves_its_stage_and_the_run_still_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".flux"
    bundle = str(FIXTURE_BUNDLE)
    assert _cli(capsys, "plan", bundle, "--state-dir", str(state))[0] == 0
    (run_dir,) = (state / "runs").iterdir()
    layout = RunLayout.for_state_dir(state, run_dir.name)
    assert (layout.run_dir / ".stage").is_dir()
    stale = layout.run_dir / ".receipt.json-ab12cd.tmp"
    stale.write_text("half a write", encoding="utf-8")
    assert (
        _cli(capsys, "apply", bundle, "--state-dir", str(state), "--run-id", layout.run_id)[0] == 0
    )
    code, text = _verify(capsys, layout)
    assert code == 0, text


def test_a_file_managers_ds_store_is_neither_refused_nor_packed(
    run: RunLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import tarfile

    (run.run_dir / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (run.emits_dir / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")

    code, text = _verify(capsys, run)
    assert code == 0, text
    code, text = _ship(capsys, run, tmp_path)
    assert code == 0, text
    archives = list((tmp_path / "out").glob("*.tgz"))
    assert archives, "the pack should hold an archive"
    with tarfile.open(archives[0]) as packed:
        names = packed.getnames()
    assert any(name.endswith("receipt.json") for name in names)
    assert not any(name.endswith(".DS_Store") for name in names)


def test_a_hidden_table_is_in_the_plan_and_not_in_the_receipt_and_the_run_still_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from .test_hidden_tables_run import hidden_bundle

    bundle = str(hidden_bundle(tmp_path))
    state = tmp_path / ".flux"
    assert _cli(capsys, "plan", bundle, "--state-dir", str(state))[0] == 0
    assert _cli(capsys, "apply", bundle, "--state-dir", str(state))[0] == 0
    (run_dir,) = (state / "runs").iterdir()
    layout = RunLayout.for_state_dir(state, run_dir.name)
    plan = json.loads(layout.plan_path.read_text(encoding="utf-8"))
    assert any(entry.get("hidden") for entry in plan["emits"].values())
    assert set(plan["emits"]) != set(_receipt(layout)["emits"])
    code, text = _cli(
        capsys, "verify", bundle, "--run-id", layout.run_id, "--state-dir", str(state)
    )
    assert "FLX-2122" not in text and "FLX-2123" not in text, text


def test_every_file_a_run_layout_names_is_one_the_run_directory_check_accepts(
    tmp_path: Path,
) -> None:
    """A new ``RunLayout`` path that the check does not know would refuse every run that has it."""
    import inspect

    from flux_engine.stages.run_binding import _name_is_known, _stream_is_known

    layout = RunLayout.for_state_dir(tmp_path / ".flux", "0123456789abcdef")
    exposes = frozenset({"sub-1"})
    arguments = {"expose_id": "sub-1", "index": 1, "count": 2}
    not_in_the_run_directory = {"golden_path", "golden_changelog_path", "ledger_path"}
    paths = []
    for name, member in inspect.getmembers(RunLayout):
        if not name.endswith("_path") or name in not_in_the_run_directory:
            continue
        if isinstance(member, property):
            paths.append(getattr(layout, name))
        elif callable(member):
            parameters = inspect.signature(member).parameters
            assert set(parameters) - {"self"} <= set(arguments), (name, tuple(parameters))
            paths.append(member(layout, **{k: arguments[k] for k in parameters if k != "self"}))
    assert len(paths) >= 15
    for path in paths:
        relative = path.relative_to(layout.run_dir)
        if relative.parent == Path("emits"):
            assert _stream_is_known(relative.name, exposes), relative
        else:
            assert relative.parent == Path("."), relative
            assert _name_is_known(relative.name, exposes), relative
    for ledger in COMPONENT_LEDGER:
        assert _name_is_known(layout.ledger_path(ledger).name, exposes)
    from flux_engine.concordance.scorecard import SCORECARD_FILE
    from flux_engine.evidence.forecast_accuracy import FORECAST_ACCURACY_FILE

    for name in (SCORECARD_FILE, FORECAST_ACCURACY_FILE, "EVIDENCE.md"):
        assert _name_is_known(name, exposes), name
