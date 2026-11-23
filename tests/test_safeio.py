# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``flux_engine.safeio``: writes and reads that never follow a link."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.safeio import (
    append_text,
    is_regular_file,
    open_append,
    read_bytes_nofollow,
    read_text_nofollow,
    refuse_links_within,
    require_real_directory,
    write_bytes_atomic,
    write_text_atomic,
)

pytestmark = pytest.mark.unit

SYMLINKS = pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")


def _victim(tmp_path: Path) -> Path:
    victim = tmp_path / "outside" / "victim.txt"
    victim.parent.mkdir()
    victim.write_text("original", encoding="utf-8")
    return victim


@SYMLINKS
@pytest.mark.parametrize(
    "write", [write_text_atomic, lambda p, t: write_bytes_atomic(p, t.encode())]
)
def test_an_atomic_write_replaces_a_link_and_leaves_its_target_alone(tmp_path: Path, write) -> None:
    victim = _victim(tmp_path)
    target = tmp_path / "state" / "out.txt"
    target.parent.mkdir()
    target.symlink_to(victim)

    write(target, "new")

    assert victim.read_text(encoding="utf-8") == "original"
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "new"


def test_an_atomic_write_leaves_no_staged_file_and_makes_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "out.txt"
    write_text_atomic(target, "x\n")
    assert [p.name for p in target.parent.iterdir()] == ["out.txt"]


def test_an_atomic_write_does_not_translate_newlines(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    write_text_atomic(target, "a\nb\n")
    assert target.read_bytes() == b"a\nb\n"


@SYMLINKS
def test_an_append_to_a_link_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    target = tmp_path / "state" / "lineage.ndjson"
    target.parent.mkdir()
    target.symlink_to(victim)

    with pytest.raises(FluxError) as refused:
        append_text(target, "line\n")

    assert refused.value.code.code == "FLX-2117"
    assert victim.read_text(encoding="utf-8") == "original"


def test_an_append_to_a_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "lineage.ndjson").mkdir()
    with pytest.raises(FluxError) as refused:
        append_text(tmp_path / "lineage.ndjson", "line\n")
    assert refused.value.code.code == "FLX-2117"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_an_append_to_a_pipe_is_refused_without_blocking(tmp_path: Path) -> None:
    pipe = tmp_path / "lineage.ndjson"
    os.mkfifo(pipe)
    with pytest.raises(FluxError) as refused:
        append_text(pipe, "line\n")
    assert refused.value.code.code == "FLX-2117"


def test_an_append_creates_and_then_extends_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "new" / "lineage.ndjson"
    append_text(target, "one\n")
    with open_append(target) as stream:
        stream.write(b"two\n")
    assert target.read_bytes() == b"one\ntwo\n"


@SYMLINKS
def test_a_read_of_a_link_is_a_catalogued_refusal_that_is_also_an_oserror(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    target = tmp_path / "ledger.ndjson"
    target.symlink_to(victim)
    with pytest.raises(FluxError) as refused:
        read_bytes_nofollow(target)
    assert refused.value.code.code == "FLX-2117"
    assert isinstance(refused.value, OSError)
    assert not is_regular_file(target)


def test_a_read_of_a_directory_is_a_catalogued_refusal_and_a_missing_name_is_not(
    tmp_path: Path,
) -> None:
    with pytest.raises(FluxError) as refused:
        read_bytes_nofollow(tmp_path)
    assert refused.value.code.code == "FLX-2117"
    with pytest.raises(OSError) as missing:
        read_bytes_nofollow(tmp_path / "missing")
    assert not isinstance(missing.value, FluxError)


def test_an_atomic_write_syncs_the_directory_that_holds_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine import safeio

    synced = []
    monkeypatch.setattr(safeio, "_fsync_directory", synced.append)
    write_text_atomic(tmp_path / "sub" / "f.txt", "x")
    assert synced == [tmp_path / "sub"]


def test_a_junction_is_treated_as_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "runs"
    directory.mkdir()
    monkeypatch.setattr(os.path, "isjunction", lambda path: Path(path) == directory, raising=False)
    with pytest.raises(FluxError) as refused:
        require_real_directory(directory)
    assert refused.value.code.code == "FLX-2118"
    assert not is_regular_file(directory)


@SYMLINKS
def test_a_read_of_a_link_to_a_device_is_refused_not_streamed(tmp_path: Path) -> None:
    target = tmp_path / "ledger.ndjson"
    target.symlink_to("/dev/zero")
    with pytest.raises(OSError):
        read_text_nofollow(target)


def test_a_read_of_a_directory_or_a_missing_name_is_an_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        read_bytes_nofollow(tmp_path)
    with pytest.raises(OSError):
        read_bytes_nofollow(tmp_path / "missing")


def test_a_read_of_a_regular_file_returns_its_bytes(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_bytes(b"\x00abc")
    assert read_bytes_nofollow(target) == b"\x00abc"
    assert read_text_nofollow(target) == "\x00abc"
    assert is_regular_file(target)


@SYMLINKS
def test_a_linked_directory_is_refused_by_name(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "runs"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(FluxError) as refused:
        require_real_directory(link, create=True)
    assert refused.value.code.code == "FLX-2118"


def test_a_file_where_a_directory_belongs_is_refused(tmp_path: Path) -> None:
    (tmp_path / "runs").write_text("x", encoding="utf-8")
    with pytest.raises(FluxError) as refused:
        require_real_directory(tmp_path / "runs", create=True)
    assert refused.value.code.code == "FLX-2118"


def test_a_real_or_missing_directory_is_accepted(tmp_path: Path) -> None:
    assert require_real_directory(tmp_path / "missing") == tmp_path / "missing"
    assert not (tmp_path / "missing").exists()
    made = require_real_directory(tmp_path / "a" / "b", create=True)
    assert made.is_dir()


@SYMLINKS
def test_a_link_anywhere_under_a_directory_is_found(tmp_path: Path) -> None:
    root = tmp_path / "run"
    (root / "emits").mkdir(parents=True)
    refuse_links_within(root)
    (root / "emits" / "x.ndjson").symlink_to(tmp_path)
    with pytest.raises(FluxError) as refused:
        refuse_links_within(root)
    assert refused.value.code.code == "FLX-2118"


# --- the sites outside the stages that write under the state directory ----------


def _event():
    from flux_engine.api.lineage import LineageEvent, LineageEventType

    return LineageEvent(
        event_type=LineageEventType.START,
        event_time="2026-01-01T00:00:00Z",
        run_id="r",
        job_namespace="flux://x",
        job_name="j",
    )


@SYMLINKS
def test_the_lineage_emitter_refuses_to_append_through_a_link(tmp_path: Path) -> None:
    from flux_engine.lineage.emitter import NdjsonLineageEmitter

    victim = _victim(tmp_path)
    target = tmp_path / "run" / "lineage.ndjson"
    target.parent.mkdir()
    target.symlink_to(victim)

    with pytest.raises(FluxError) as refused:
        NdjsonLineageEmitter(path=target).emit(_event())

    assert refused.value.code.code == "FLX-2117"
    assert victim.read_text(encoding="utf-8") == "original"


@SYMLINKS
def test_the_decision_log_replaces_links_at_both_ledgers(tmp_path: Path) -> None:
    from flux_engine.governance.audit import DecisionAuditLog

    out = tmp_path / "run"
    out.mkdir()
    victims = []
    for name in ("decisions.ndjson", "decisions.meta.ndjson"):
        victim = out.parent / f"victim-{name}"
        victim.write_text("original", encoding="utf-8")
        (out / name).symlink_to(victim)
        victims.append(victim)

    DecisionAuditLog().write(out)

    assert [v.read_text(encoding="utf-8") for v in victims] == ["original", "original"]
    assert not (out / "decisions.ndjson").is_symlink()
    assert not (out / "decisions.meta.ndjson").is_symlink()


@SYMLINKS
def test_the_decision_log_refuses_a_directory_that_is_a_link(tmp_path: Path) -> None:
    from flux_engine.governance.audit import DecisionAuditLog

    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "run").symlink_to(real, target_is_directory=True)
    with pytest.raises(FluxError) as refused:
        DecisionAuditLog().write(tmp_path / "run")
    assert refused.value.code.code == "FLX-2118"
    assert not list(real.iterdir())


@SYMLINKS
def test_the_calibration_provenance_replaces_a_link(tmp_path: Path) -> None:
    from decimal import Decimal

    from flux_engine.calibrate.provenance import write_calibration_provenance

    victim = _victim(tmp_path)
    target = tmp_path / "calibration.json"
    target.symlink_to(victim)

    write_calibration_provenance(
        target, world_ref="w", dials={"d": Decimal("1")}, generated_at="2026-01-01T00:00:00Z"
    )

    assert victim.read_text(encoding="utf-8") == "original"
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8").startswith("{")


@SYMLINKS
def test_the_sink_receipts_replace_a_link(tmp_path: Path) -> None:
    from flux_engine.sinks.receipts import SinkReceipt
    from flux_engine.stages.deliver import write_receipts
    from flux_engine.stages.layout import RunLayout

    layout = RunLayout.for_state_dir(tmp_path / "state", "0123456789abcdef")
    layout.run_dir.mkdir(parents=True)
    victim = _victim(tmp_path)
    layout.sink_receipts_path.symlink_to(victim)
    receipt = SinkReceipt(
        sink="memory",
        run_id=layout.run_id,
        expose_id="e",
        output_digest="sha256:" + "0" * 64,
        rows=0,
        target="memory",
    )

    write_receipts(layout, [receipt])

    assert victim.read_text(encoding="utf-8") == "original"
    assert not layout.sink_receipts_path.is_symlink()
