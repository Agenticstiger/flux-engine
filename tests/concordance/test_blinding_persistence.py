# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The blinding value belongs to the state, not to the invocation.

SEAM-FIDELITY-SPEC §4 asks ``C2`` to commit to the derived statistic under
"a per-STATE blinding value that never leaves the boundary". It was drawn
with ``generate_blinding()`` at every ``fluxsim ship`` and ``fluxsim verify``
call instead, so one unchanged record set was committed under a new value
every time it was checked: a different ``C2``, a different signed fidelity
record, and a different Merkle root over the evidence archive carrying it.

What is pinned here is the *state* half of that sentence -- the value is
drawn once, when the records are first committed, kept in the boundary
store under the owner's state directory, and read back for every later
check of those same records. The "never leaves the boundary" half is
pinned in :mod:`tests.concordance.test_boundary` (the published state
carries no blinding value) and, for the packed archive, in
``tests/stages/test_ship_fidelity_bundle.py``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from flux_engine.concordance.boundary.slice import (
    MINIMUM_BLINDING_BYTES,
    SliceReference,
    commitment_c2,
    slice_content_digest,
)
from flux_engine.concordance.boundary.store import BoundaryStore
from flux_engine.concordance.boundary.vault import BOUNDARY_DIRNAME, BlindingVault
from flux_engine.core.errors import FluxError
from tests.concordance.conftest import build_compiled

pytestmark = pytest.mark.unit

STAMPED = "2026-09-01T00:00:00.000Z"
DECLARED = "2026-08-01T12:00:00.000Z"


def _reference(rows) -> SliceReference:
    return SliceReference(uri="file:///committed.ndjson", first_line=1, last_line=len(rows))


def _store(state_dir: Path, *, budget: int = 8) -> BoundaryStore:
    return BoundaryStore.for_state_dir(state_dir, max_checks_per_state=budget)


def _commit(state_dir: Path, rows, compiled, **kwargs):
    return _store(state_dir).commit(
        rows=rows,
        compiled=compiled,
        slice_reference=_reference(rows),
        stamped_at=STAMPED,
        **kwargs,
    )


@pytest.fixture()
def rows(document):
    return [dict(row) for row in document["committedSlice"]]


# ---------------------------------------------------------------------------
# One record set, one state
# ---------------------------------------------------------------------------


def test_two_commits_of_one_record_set_are_one_state(tmp_path, rows, compiled) -> None:
    """The defect, at its smallest: two stores, one state directory, one C2."""
    first = _commit(tmp_path, rows, compiled)
    second = _commit(tmp_path, rows, compiled)
    assert first.c2 == second.c2
    assert first.ordinal == second.ordinal == 1
    assert first.committed_at == second.committed_at
    assert first.to_document() == second.to_document()


def test_the_negative_control_a_second_draw_would_move_c2(tmp_path, rows, compiled) -> None:
    """The scan has teeth: a freshly drawn value gives that same state a new C2.

    Without this, ``test_two_commits_of_one_record_set_are_one_state``
    would also pass against a ``C2`` that did not depend on the blinding
    value at all.
    """
    from flux_engine.concordance.boundary.slice import generate_blinding

    state = _commit(tmp_path, rows, compiled)
    assert commitment_c2(state.statistic, generate_blinding()) != state.c2


def _moved(rows):
    """The same slice with one record's ``attempts`` changed -- a new record set.

    Inside the declared range, and it moves no group below
    ``records.min``, so the second state forms cleanly and the two states
    differ only in the way the test is about.
    """
    moved = [dict(row) for row in rows]
    moved[0]["attempts"] = int(moved[0]["attempts"]) + 1
    return moved


def test_a_changed_record_set_is_a_new_state_at_the_next_ordinal(tmp_path, rows, compiled) -> None:
    """§8: a record set that is not the committed one is a new state (ordinal+1)."""
    first = _commit(tmp_path, rows, compiled)
    second = _commit(tmp_path, _moved(rows), compiled)
    assert second.ordinal == first.ordinal + 1
    assert second.c2 != first.c2

    vault = BlindingVault.for_state_dir(tmp_path)
    blindings = {entry.blinding for entry in vault.entries()}
    assert len(blindings) == 2, "each state is drawn its own blinding value"


def test_the_state_is_keyed_by_records_and_expose_not_by_the_slice_file(
    tmp_path, rows, compiled
) -> None:
    """Re-serialised records are the same records, so the same state."""
    reordered = [{key: row[key] for key in reversed(list(row))} for row in rows]
    assert [json.dumps(row) for row in reordered] != [json.dumps(row) for row in rows]
    assert slice_content_digest(reordered) == slice_content_digest(rows)

    first = _commit(tmp_path, rows, compiled)
    second = _commit(tmp_path, reordered, compiled)
    assert second.ordinal == first.ordinal
    assert second.c2 == first.c2


def test_one_record_set_under_two_exposes_is_two_states(tmp_path, document, rows) -> None:
    """The key is the exposeId as well as the records."""
    compiled = build_compiled(document)
    other = json.loads(json.dumps(document))
    for entry in other["contract"]["exposes"]:
        if entry["exposeId"] == document["expose"]["exposeId"]:
            entry["exposeId"] = "another_moment"
    other["expose"]["exposeId"] = "another_moment"
    other["emit"]["exposeId"] = "another_moment"
    renamed = build_compiled(other)
    assert renamed.block.expose_id != compiled.block.expose_id

    _commit(tmp_path, rows, compiled)
    _commit(tmp_path, rows, renamed)
    vault = BlindingVault.for_state_dir(tmp_path)
    assert {entry.expose_id for entry in vault.entries()} == {
        compiled.block.expose_id,
        "another_moment",
    }
    assert len({entry.blinding for entry in vault.entries()}) == 2


# ---------------------------------------------------------------------------
# The commitment time
# ---------------------------------------------------------------------------


def test_a_state_keeps_the_time_it_was_first_committed_at(tmp_path, rows, compiled) -> None:
    """A re-check that declares nothing does not re-date the state."""
    first = _commit(tmp_path, rows, compiled)
    later = _store(tmp_path).commit(
        rows=rows,
        compiled=compiled,
        slice_reference=_reference(rows),
        stamped_at="2027-01-01T00:00:00.000Z",
    )
    assert later.committed_at == first.committed_at == STAMPED


def test_the_owners_own_declaration_dates_this_check(tmp_path, rows, compiled) -> None:
    """``fluxsim ship --committed-at`` is a statement the check must carry.

    It is not a licence to move the stored commitment time of records that
    have not changed, and the blinding value is the state's either way.
    """
    stamped = _commit(tmp_path, rows, compiled)
    declared = _commit(tmp_path, rows, compiled, committed_at=DECLARED)
    assert declared.committed_at == DECLARED
    assert declared.c2 == stamped.c2, "C2 commits to S and the blinding value, not to a date"

    again = _commit(tmp_path, rows, compiled)
    assert again.committed_at == STAMPED


def test_a_state_dated_by_nothing_at_all_is_refused(tmp_path, rows, compiled) -> None:
    """This package reads no clock, so a caller supplies the instant."""
    with pytest.raises(FluxError) as caught:
        _store(tmp_path).commit(rows=rows, compiled=compiled, slice_reference=_reference(rows))
    assert caught.value.code.code == "FLX-5013"


# ---------------------------------------------------------------------------
# Fail-closed: a state whose blinding value the store cannot supply
# ---------------------------------------------------------------------------


def _rewrite(vault: BlindingVault, mutate) -> None:
    document = json.loads(vault.path.read_text(encoding="utf-8"))
    mutate(document)
    vault.path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_a_stored_state_with_no_blinding_value_fails_closed(tmp_path, rows, compiled) -> None:
    """The one thing the store must never do is quietly draw a replacement."""
    before = _commit(tmp_path, rows, compiled)
    vault = BlindingVault.for_state_dir(tmp_path)

    def drop(document):
        document["states"][0]["blinding"] = ""

    _rewrite(vault, drop)
    with pytest.raises(FluxError) as caught:
        _commit(tmp_path, rows, compiled)
    assert caught.value.code.code == "FLX-5013"
    assert before.c2  # the state existed; the refusal is about its blinding value


def test_a_stored_blinding_value_that_is_a_public_constant_is_refused(
    tmp_path, rows, compiled
) -> None:
    """``refuse_weak_blinding`` guards the way out of the store as well as in."""
    _commit(tmp_path, rows, compiled)
    vault = BlindingVault.for_state_dir(tmp_path)

    def flatten(document):
        document["states"][0]["blinding"] = bytes(MINIMUM_BLINDING_BYTES).hex()

    _rewrite(vault, flatten)
    with pytest.raises(FluxError) as caught:
        _commit(tmp_path, rows, compiled)
    assert caught.value.code.code == "FLX-5035"
    assert "public constant" in str(caught.value)


@pytest.mark.parametrize(
    "body",
    ["not json", '{"vaultVersion": 1}', '{"vaultVersion": 99, "states": []}', "[]"],
    ids=["unparseable", "no-states", "wrong-version", "not-an-object"],
)
def test_an_unreadable_store_is_not_an_empty_one(tmp_path, rows, compiled, body) -> None:
    _commit(tmp_path, rows, compiled)
    vault = BlindingVault.for_state_dir(tmp_path)
    vault.path.write_text(body, encoding="utf-8")
    with pytest.raises(FluxError) as caught:
        _commit(tmp_path, rows, compiled)
    assert caught.value.code.code == "FLX-5013"


def test_an_absent_store_is_an_empty_one(tmp_path) -> None:
    """The first check of the first run has nothing to read back yet."""
    assert BlindingVault.for_state_dir(tmp_path).entries() == ()


def test_a_store_that_cannot_be_written_refuses_the_commitment(tmp_path, rows, compiled) -> None:
    locked = tmp_path / "read-only"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        with pytest.raises(FluxError) as caught:
            _commit(locked, rows, compiled)
        assert caught.value.code.code == "FLX-5013"
    finally:
        os.chmod(locked, 0o700)


def test_a_store_that_keeps_nothing_refuses_to_commit(rows, compiled) -> None:
    """An in-memory store cannot hand a blinding value back to a later check."""
    store = BoundaryStore(max_checks_per_state=1)
    assert store.vault is None
    with pytest.raises(FluxError) as caught:
        store.commit(
            rows=rows, compiled=compiled, slice_reference=_reference(rows), stamped_at=STAMPED
        )
    assert caught.value.code.code == "FLX-5013"


# ---------------------------------------------------------------------------
# Where the value lives, and what names it
# ---------------------------------------------------------------------------


def test_the_store_lives_beside_the_runs_never_inside_one(tmp_path, rows, compiled) -> None:
    """Everything under a run directory is staged into the evidence pack."""
    _commit(tmp_path, rows, compiled)
    vault = BlindingVault.for_state_dir(tmp_path)
    assert vault.path.is_file()
    assert vault.directory == tmp_path / BOUNDARY_DIRNAME
    assert BOUNDARY_DIRNAME != "runs"
    assert not vault.path.is_relative_to(tmp_path / "runs")


def test_the_store_is_readable_only_by_its_owner(tmp_path, rows, compiled) -> None:
    _commit(tmp_path, rows, compiled)
    vault = BlindingVault.for_state_dir(tmp_path)
    assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(vault.directory.stat().st_mode) == 0o700


def test_no_staging_file_is_left_behind(tmp_path, rows, compiled) -> None:
    """The write is atomic; a half-written store must not survive it.

    The directory holds the store and the advisory-lock sentinel
    ``resolve`` serialises on, and nothing else -- in particular none of
    the ``.states-*.json`` files the staged write moves into place.
    """
    _commit(tmp_path, rows, compiled)
    _commit(tmp_path, _moved(rows), compiled)
    vault = BlindingVault.for_state_dir(tmp_path)
    held = [path.name for path in sorted(vault.directory.iterdir())]
    assert held == sorted([vault.path.name, vault.lock_path.name])
    assert not [name for name in held if name.startswith(".states-")]


def test_neither_the_store_nor_its_snapshot_names_a_blinding_value(
    tmp_path, rows, compiled
) -> None:
    state = _commit(tmp_path, rows, compiled)
    vault = BlindingVault.for_state_dir(tmp_path)
    held = vault.entries()[0].blinding
    assert held.hex() not in repr(vault)
    assert held.hex() not in json.dumps(vault.snapshot())
    assert held.hex() not in json.dumps(state.to_document(), sort_keys=True)
    assert vault.snapshot() == {state.expose_id: [state.ordinal]}


def test_the_drawn_value_is_the_length_the_module_requires(tmp_path, rows, compiled) -> None:
    _commit(tmp_path, rows, compiled)
    entry = BlindingVault.for_state_dir(tmp_path).entries()[0]
    assert len(entry.blinding) == MINIMUM_BLINDING_BYTES
    assert len(set(entry.blinding)) > 1


def test_two_state_directories_do_not_share_a_state(tmp_path, rows, compiled) -> None:
    """The store is the owner's, so a second one draws its own value."""
    here = _commit(tmp_path / "a", rows, compiled)
    there = _commit(tmp_path / "b", rows, compiled)
    assert here.ordinal == there.ordinal
    assert here.c2 != there.c2


# ---------------------------------------------------------------------------
# The store refuses a state and a blinding value that do not go together
# ---------------------------------------------------------------------------


def test_putting_a_state_under_a_blinding_value_it_was_not_committed_with_is_refused(
    tmp_path, rows, compiled
) -> None:
    from flux_engine.concordance.boundary.slice import generate_blinding

    state = _commit(tmp_path, rows, compiled)
    fresh = BoundaryStore(max_checks_per_state=1)
    with pytest.raises(FluxError) as caught:
        fresh.put(state, generate_blinding())
    assert caught.value.code.code == "FLX-5035"


def test_a_committed_state_still_confirms_against_the_store_that_holds_it(
    tmp_path, rows, compiled
) -> None:
    store = _store(tmp_path)
    state = store.commit(
        rows=rows, compiled=compiled, slice_reference=_reference(rows), stamped_at=STAMPED
    )
    assert store.confirm_commitment(state.ordinal, state.c2) is True
    assert store.confirm_commitment(state.ordinal, "sha256:" + "0" * 64) is False
