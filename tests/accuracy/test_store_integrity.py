# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The store keeps the seal: written once, verified on every read, no link followed.

Before, ``register`` wrote with a plain ``write_text``, so registering an id
again replaced the sealed record and exited 0; ``read_prediction`` returned
the stored JSON without checking its ``sealDigest``, so a hand-edited
record was scored, and published with a digest recomputed from the edit;
and every write followed a link planted at the record's name.

``tests/cli/test_accuracy_seal_integrity.py`` covers the same behaviour
through the real command.
"""

from __future__ import annotations

import errno
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.accuracy import _entries
from flux_engine.accuracy.records import seal_prediction
from flux_engine.accuracy.store import AccuracyStore, prediction_from_document
from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict, PredictionRecord
from flux_engine.api.claims import ClaimBasis
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit

_T1 = "2026-09-01T00:00:00Z"
_T2 = "2026-09-02T00:00:00Z"


def _document(prediction_id: str = "p-1", **fields: Any) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "prediction_id": prediction_id,
        "run_digest": "sha256:" + "0" * 64,
        "metric_id": "telco/saved_customers",
        "statement": "saved customers rise",
        "predicted": "12",
        "interval_low": "10",
        "interval_high": "14",
        "horizon_end": "2026-10-01T00:00:00Z",
        "registered_at": _T1,
    }
    document.update(fields)
    return document


def _register(store: AccuracyStore, document: Dict[str, Any]):
    return store.register(prediction_from_document(document), document)


def _code(raised: pytest.ExceptionInfo) -> str:
    return str(raised.value.code.code)


def _record(store: AccuracyStore, prediction_id: str = "p-1") -> Path:
    return store.predictions_dir / f"{prediction_id}.json"


def _edit(path: Path, **fields: Any) -> None:
    """Change fields of a sealed record by hand, leaving its sealDigest as it was."""
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(fields)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _verdict() -> AccuracyVerdict:
    return AccuracyVerdict(
        prediction_id="p-1",
        metric_id="telco/saved_customers",
        outcome=AccuracyOutcome.WITHIN_INTERVAL,
        basis=ClaimBasis.HISTORICAL_HOLDOUT,
    )


# --- written once -------------------------------------------------------------


def test_registering_the_same_claim_again_writes_nothing(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    first = _register(store, _document())
    before = os.stat(first.path)
    again = _register(store, _document())
    after = os.stat(first.path)
    assert first.created and not again.created
    assert again.seal.value == first.seal.value
    assert again.path == first.path
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_different_claim_under_a_registered_id_is_refused(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    first = _register(store, _document())
    sealed_bytes = first.path.read_bytes()
    changed = _document(predicted="13")
    with pytest.raises(FluxError) as raised:
        _register(store, changed)
    assert _code(raised) == "FLX-7007"
    detail = raised.value.detail
    assert "'p-1'" in detail
    assert first.seal.value in detail
    assert seal_prediction(prediction_from_document(changed)).value in detail
    assert first.path.read_bytes() == sealed_bytes


def test_the_same_claim_spelled_another_way_is_the_same_registration(tmp_path: Path) -> None:
    """The seal decides: ``1E+1`` and ``10`` are one decimal, so one claim, and the
    record first written keeps its spelling."""
    store = AccuracyStore.for_state_dir(tmp_path)
    first = _register(store, _document(predicted="1E+1"))
    sealed_bytes = first.path.read_bytes()
    again = _register(store, _document(predicted="10"))
    assert not again.created
    assert first.path.read_bytes() == sealed_bytes


def test_a_record_is_the_same_bytes_an_earlier_build_wrote(tmp_path: Path) -> None:
    """A state directory written before this change reads back and verifies."""
    store = AccuracyStore.for_state_dir(tmp_path)
    document = _document()
    registration = _register(store, document)
    earlier = json.dumps(
        {**document, "sealDigest": registration.seal.value}, indent=2, sort_keys=True
    )
    assert registration.path.read_bytes() == (earlier + "\n").encode("utf-8")


def test_a_document_that_describes_another_claim_is_not_written(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    prediction = prediction_from_document(_document())
    with pytest.raises(ValueError, match="another claim"):
        store.register(prediction, _document(predicted="99"))
    assert not _record(store).exists()


def test_a_document_that_is_not_a_prediction_is_not_written(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    prediction = prediction_from_document(_document())
    document = _document()
    del document["run_digest"]
    with pytest.raises(ValueError, match="'run_digest'"):
        store.register(prediction, document)
    assert not _record(store).exists()


def test_a_second_register_does_not_compare_against_a_record_that_fails_its_seal(
    tmp_path: Path,
) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    _register(store, _document())
    _edit(_record(store), predicted="13")
    with pytest.raises(FluxError) as raised:
        _register(store, _document())
    assert _code(raised) == "FLX-7008"


# --- verified on every read ---------------------------------------------------


def test_a_record_edited_after_it_was_sealed_is_refused(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    registration = _register(store, _document())
    _edit(registration.path, interval_high="99")
    with pytest.raises(FluxError) as raised:
        store.read_prediction("p-1")
    assert _code(raised) == "FLX-7008"
    detail = raised.value.detail
    assert registration.seal.value in detail
    assert seal_prediction(prediction_from_document(_document(interval_high="99"))).value in detail


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"{not json", "is not JSON"),
        (b"\xff\xfe", "is not JSON"),
        (b"[" * 100_000, "is not JSON the store can read"),
        (b"[]", "is not a JSON object"),
        (json.dumps(_document()).encode(), "carries no sealDigest"),
        (
            json.dumps(
                {**_document(predicted="twelve"), "sealDigest": "sha256:" + "1" * 64}
            ).encode(),
            "a decimal field is not a number",
        ),
        (
            json.dumps({**_document(), "run_digest": "nope", "sealDigest": "x"}).encode(),
            "malformed digest",
        ),
        (
            json.dumps(
                {key: value for key, value in _document().items() if key != "metric_id"}
                | {"sealDigest": "x"}
            ).encode(),
            "it has no 'metric_id'",
        ),
    ],
)
def test_a_record_that_is_not_a_sealed_prediction_is_refused(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    store.predictions_dir.mkdir(parents=True)
    _record(store).write_bytes(content)
    with pytest.raises(FluxError) as raised:
        store.read_prediction("p-1")
    assert _code(raised) == "FLX-7008"
    assert reason in raised.value.detail


def test_a_record_copied_under_another_id_is_refused(tmp_path: Path) -> None:
    """The record of p-2 under p-1's name matches its own seal, but it is not p-1's."""
    store = AccuracyStore.for_state_dir(tmp_path)
    other = _register(store, _document("p-2"))
    _record(store).write_bytes(other.path.read_bytes())
    with pytest.raises(FluxError) as raised:
        store.read_prediction("p-1")
    assert _code(raised) == "FLX-7008"
    assert "is the record of 'p-2'" in raised.value.detail


def test_ids_that_differ_only_by_case_are_named_not_replaced(tmp_path: Path) -> None:
    """A filesystem that ignores case (macOS's default) keeps Abc and abc in one
    file, so the second id reaches the first one's record. Before, register
    replaced it; now it is refused, with the first id and the cause named."""
    store = AccuracyStore.for_state_dir(tmp_path)
    first = _register(store, _document("Abc"))
    sealed_bytes = first.path.read_bytes()
    folded = store.predictions_dir / "abc.json"
    if not folded.exists():  # a filesystem that keeps case: make the collision by hand
        folded.write_bytes(sealed_bytes)
    for call in (lambda: _register(store, _document("abc")), lambda: store.read_prediction("abc")):
        with pytest.raises(FluxError) as raised:
            call()
        assert _code(raised) == "FLX-7008"
        assert "is the record of 'Abc'" in raised.value.detail
        assert "differ only by case" in raised.value.detail
    assert first.path.read_bytes() == sealed_bytes


def test_a_record_moved_out_of_its_registration_stops_the_listing(tmp_path: Path) -> None:
    """An edited registered_at would leave the window without a word, so every
    record is verified, not only the ones that match."""
    store = AccuracyStore.for_state_dir(tmp_path)
    _register(store, _document("p-0"))
    _register(store, _document("p-1"))
    _edit(_record(store, "p-1"), registered_at=_T2)
    with pytest.raises(FluxError) as raised:
        store.documents_registered_at(_T1)
    assert _code(raised) == "FLX-7008"
    assert "'p-1'" in raised.value.detail


def test_every_record_that_fails_is_named_in_one_refusal(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    for prediction_id in ("p-0", "p-1", "p-2"):
        _register(store, _document(prediction_id))
    _edit(_record(store, "p-0"), predicted="1")
    _record(store, "p-2").unlink()
    _record(store, "p-2").symlink_to(_record(store, "p-1"))
    with pytest.raises(FluxError) as raised:
        store.documents_registered_at(_T1)
    detail = raised.value.detail
    assert _code(raised) == "FLX-7008"
    assert detail.startswith("2 entries under")
    assert "FLX-7008: the sealed record of 'p-0'" in detail
    assert "FLX-7009: " in detail and "p-2.json is a symbolic link" in detail


def test_a_staged_file_is_not_read_as_a_record(tmp_path: Path) -> None:
    """What a crash between staging and publishing leaves is ignored."""
    store = AccuracyStore.for_state_dir(tmp_path)
    _register(store, _document())
    (store.predictions_dir / ".p-2.json.0123456789abcdef.tmp").write_text("{", encoding="utf-8")
    assert [d["prediction_id"] for d in store.documents_registered_at(_T1)] == ["p-1"]


# --- no link followed ---------------------------------------------------------


@pytest.mark.parametrize("target", ["outside", "another record", "nowhere"])
def test_a_link_at_the_prediction_path_is_refused_and_its_target_untouched(
    tmp_path: Path, target: str
) -> None:
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    other = _register(store, _document("p-2"))
    victim = {
        "outside": tmp_path / "victim.txt",
        "another record": other.path,
        "nowhere": tmp_path / "absent.txt",
    }[target]
    if target == "outside":
        victim.write_text("keep me\n", encoding="utf-8")
    before = victim.read_bytes() if victim.exists() else None
    _record(store).symlink_to(victim)
    with pytest.raises(FluxError) as raised:
        _register(store, _document())
    assert _code(raised) == "FLX-7009"
    assert "p-1.json is a symbolic link" in raised.value.detail
    assert (victim.read_bytes() if victim.exists() else None) == before
    assert _record(store).is_symlink()
    with pytest.raises(FluxError) as read:
        store.read_prediction("p-1")
    assert _code(read) == "FLX-7009"


@pytest.mark.parametrize("target", ["inside the store", "outside", "nowhere"])
def test_a_link_in_place_of_the_predictions_directory_is_refused(
    tmp_path: Path, target: str
) -> None:
    """FLX-7009 wherever the link points: resolving the directory first answered
    FLX-7003 for a link out of the store, as if the id were at fault."""
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    store.verdicts_dir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.predictions_dir.symlink_to(
        {
            "inside the store": store.verdicts_dir,
            "outside": elsewhere,
            "nowhere": tmp_path / "absent",
        }[target]
    )
    for call in (
        lambda: _register(store, _document()),
        lambda: store.read_prediction("p-1"),
        lambda: store.documents_registered_at(_T1),
    ):
        with pytest.raises(FluxError) as raised:
            call()
        assert _code(raised) == "FLX-7009"
        assert "predictions is a symbolic link" in raised.value.detail
    assert list(store.verdicts_dir.iterdir()) == []
    assert list(elsewhere.iterdir()) == []
    assert not (tmp_path / "absent").exists()


def test_a_link_in_place_of_the_store_directory_is_refused(tmp_path: Path) -> None:
    """Only the state directory is the operator's to place; a link at
    <state-dir>/accuracy is inside it, so it is refused, not followed."""
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.root.parent.mkdir(parents=True)
    store.root.symlink_to(elsewhere)
    for call in (
        lambda: _register(store, _document()),
        lambda: store.write_verdict(_verdict()),
        lambda: store.read_prediction("p-1"),
    ):
        with pytest.raises(FluxError) as raised:
            call()
        assert _code(raised) == "FLX-7009"
        shown = (tmp_path / "state").resolve() / "accuracy"
        assert raised.value.detail.startswith(f"{shown} is a symbolic link")
    assert list(elsewhere.iterdir()) == []


def test_a_state_directory_reached_through_a_link_is_followed(tmp_path: Path) -> None:
    """Where the state directory lives is the operator's choice."""
    real = tmp_path / "real-state"
    real.mkdir()
    (tmp_path / "state").symlink_to(real)
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    assert _register(store, _document()).created
    assert (real / "accuracy" / "predictions" / "p-1.json").is_file()
    assert store.read_prediction("p-1") is not None


def test_a_state_directory_through_a_link_and_up_goes_where_the_kernel_goes(
    tmp_path: Path,
) -> None:
    """``link/..`` is the parent of the link's target, as for every other stage
    given the same ``--state-dir``, not the directory the link sits in."""
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "link").symlink_to(real / "inner")
    store = AccuracyStore.for_state_dir(tmp_path / "work" / "link" / "..")
    registration = _register(store, _document())
    assert registration.path == real.resolve() / "accuracy" / "predictions" / "p-1.json"
    assert registration.path.is_file()
    assert not (tmp_path / "work" / "accuracy").exists()


def test_a_directory_at_the_prediction_path_is_refused(tmp_path: Path) -> None:
    """os.fdopen refuses a directory with its own IsADirectoryError, so the type
    is checked on the descriptor before it is wrapped."""
    store = AccuracyStore.for_state_dir(tmp_path)
    _register(store, _document("p-0"))
    _record(store).mkdir()
    for call in (
        lambda: _register(store, _document()),
        lambda: store.read_prediction("p-1"),
        lambda: store.documents_registered_at(_T1),
    ):
        with pytest.raises(FluxError) as raised:
            call()
        assert _code(raised) == "FLX-7009"
        assert "p-1.json is a directory, not a regular file" in raised.value.detail
    assert _record(store).is_dir()


def test_the_longest_prediction_id_registers_and_is_scored(tmp_path: Path) -> None:
    """A staged file's name does not grow with the record's, so a 250-character
    id (``<id>.json`` is 255 bytes, the most a filesystem takes) still works."""
    store = AccuracyStore.for_state_dir(tmp_path)
    long_id = "p" * 250
    assert _register(store, _document(long_id)).created
    assert not _register(store, _document(long_id)).created
    verdict = AccuracyVerdict(
        prediction_id=long_id,
        metric_id="telco/saved_customers",
        outcome=AccuracyOutcome.WITHIN_INTERVAL,
        basis=ClaimBasis.HISTORICAL_HOLDOUT,
    )
    store.write_verdict(verdict)
    assert store.read_verdict(long_id) is not None
    assert [d["prediction_id"] for d in store.documents_registered_at(_T1)] == [long_id]


def test_a_file_in_place_of_the_verdicts_directory_is_refused(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    store.root.mkdir(parents=True)
    store.verdicts_dir.write_text("not a directory", encoding="utf-8")
    with pytest.raises(FluxError) as raised:
        store.write_verdict(_verdict())
    assert _code(raised) == "FLX-7009"
    assert "is a regular file, not a directory" in raised.value.detail


@pytest.mark.timeout(30)
def test_a_pipe_at_the_prediction_path_is_refused_without_blocking(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    store.predictions_dir.mkdir(parents=True)
    os.mkfifo(_record(store))
    with pytest.raises(FluxError) as raised:
        store.read_prediction("p-1")
    assert _code(raised) == "FLX-7009"
    assert "is a named pipe" in raised.value.detail


def test_a_verdict_written_over_a_link_replaces_the_link(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    store.verdicts_dir.mkdir(parents=True)
    link = store.verdicts_dir / "p-1.json"
    link.symlink_to(victim)
    path = store.write_verdict(_verdict())
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert not link.is_symlink()
    assert json.loads(path.read_text(encoding="utf-8"))["outcome"] == "within-interval"


def test_a_directory_at_the_verdict_path_is_refused(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    (store.verdicts_dir / "p-1.json").mkdir(parents=True)
    with pytest.raises(FluxError) as raised:
        store.write_verdict(_verdict())
    assert _code(raised) == "FLX-7009"
    assert "is a directory" in raised.value.detail
    assert [entry.name for entry in store.verdicts_dir.iterdir()] == ["p-1.json"]


def test_a_link_at_the_verdict_path_is_not_read(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    store.verdicts_dir.mkdir(parents=True)
    (store.verdicts_dir / "p-1.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(FluxError) as raised:
        store.read_verdict("p-1")
    assert _code(raised) == "FLX-7009"


# --- staged files and filesystems without hard links ---------------------------


def test_nothing_but_records_is_left_in_the_store(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    _register(store, _document())
    _register(store, _document())
    with pytest.raises(FluxError):
        _register(store, _document(predicted="13"))
    store.write_verdict(_verdict())
    store.write_verdict(_verdict())
    assert sorted(p.name for p in store.predictions_dir.iterdir()) == ["p-1.json"]
    assert sorted(p.name for p in store.verdicts_dir.iterdir()) == ["p-1.json"]


def test_a_failed_write_leaves_no_record_and_no_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)

    def no_space(_descriptor: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_entries.os, "fsync", no_space)
    with pytest.raises(OSError, match="No space"):
        _register(store, _document())
    assert list(store.predictions_dir.iterdir()) == []


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP])
def test_without_hard_links_the_record_is_created_by_name_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)

    def no_hard_links(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(_entries.os, "link", no_hard_links)
    first = _register(store, _document())
    assert first.created
    assert json.loads(first.path.read_text(encoding="utf-8"))["sealDigest"] == first.seal.value
    assert not _register(store, _document()).created
    with pytest.raises(FluxError) as raised:
        _register(store, _document(predicted="13"))
    assert _code(raised) == "FLX-7007"
    assert sorted(p.name for p in store.predictions_dir.iterdir()) == ["p-1.json"]


def test_any_other_link_failure_is_reported_not_worked_around(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)

    def read_only(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(_entries.os, "link", read_only)
    with pytest.raises(OSError, match="Read-only"):
        _register(store, _document())
    assert list(store.predictions_dir.iterdir()) == []


def test_a_record_that_vanishes_under_register_is_published_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry that refused the publish is gone by the time it is read."""
    store = AccuracyStore.for_state_dir(tmp_path)
    calls = {"create_once": 0}
    real_create_once = _entries.create_once

    def taken_once(dir_fd: int, name: str, data: bytes) -> bool:
        calls["create_once"] += 1
        return False if calls["create_once"] == 1 else real_create_once(dir_fd, name, data)

    monkeypatch.setattr("flux_engine.accuracy.store.create_once", taken_once)
    assert _register(store, _document()).created
    assert calls["create_once"] == 2


def test_a_name_that_keeps_vanishing_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    monkeypatch.setattr("flux_engine.accuracy.store.create_once", lambda *_args: False)
    with pytest.raises(OSError, match="run the command again"):
        _register(store, _document())


def test_prediction_from_document_reads_a_zero_bound() -> None:
    prediction = prediction_from_document(_document(interval_low=0))
    assert isinstance(prediction, PredictionRecord)
    assert prediction.interval_low == Decimal("0")
