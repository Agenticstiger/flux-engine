# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Two resolves at once must not lose a committed state's blinding value.

:meth:`BlindingVault.resolve` reads the store, decides an ordinal, draws a
blinding value and writes the whole document back. Unsynchronised, two
callers read the same store and the second write drops the first caller's
entry -- and the dropped state has no entry at the next check, so it is
re-drawn under a *different* blinding value. That is the one thing
``vault.py``'s own docstring says this module must never do: a second
``C2``, a second signed fidelity record and a second Merkle root for a
record set nobody touched.

It is not a two-terminal curiosity. :mod:`flux_engine.engine` is the
documented library facade for "a host application's backend, a hosted runner"
and dispatches every stage through ``asyncio.to_thread``, so concurrent
``ship``/``verify`` against one ``--state-dir`` is the intended usage.

The interleave is forced rather than raced: the draw itself is made slow,
so both threads are inside the read-modify-write window at once. The
window is inside the lock, so the lock is what has to serialise them.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest

from flux_engine.concordance.boundary import vault as vault_module
from flux_engine.concordance.boundary.vault import BlindingVault

pytestmark = pytest.mark.unit

_EXPOSE = "payment_recovery_moment"


@pytest.fixture()
def slow_draw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the read-decide-draw-write window to the width of a scheduler tick."""
    real = vault_module.generate_blinding

    def slowly() -> bytes:
        time.sleep(0.25)
        return real()

    monkeypatch.setattr(vault_module, "generate_blinding", slowly)


def _resolve_all(vault: BlindingVault, digests: List[str]) -> Dict[str, bytes]:
    drawn: Dict[str, bytes] = {}
    errors: List[BaseException] = []

    def worker(slice_digest: str) -> None:
        try:
            state = vault.resolve(
                expose_id=_EXPOSE, slice_digest=slice_digest, committed_at="2026-07-01T00:00:00Z"
            )
            drawn[slice_digest] = state.blinding
        except BaseException as error:  # pragma: no cover - reported, not swallowed
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(digest,)) for digest in digests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    return drawn


def test_concurrent_resolves_all_survive_in_the_store(tmp_path: Path, slow_draw: None) -> None:
    vault = BlindingVault.for_state_dir(tmp_path)
    digests = [f"sha256:{index:064x}" for index in range(1, 5)]

    drawn = _resolve_all(vault, digests)

    held = {state.slice_digest: state for state in vault.entries()}
    assert sorted(held) == sorted(digests), "a concurrent resolve was lost from the store"
    for digest in digests:
        assert held[digest].blinding == drawn[digest], (
            "the state that survived carries a different blinding value from the one "
            "its caller was handed"
        )
    assert sorted(state.ordinal for state in vault.entries()) == [1, 2, 3, 4]


def test_a_state_lost_to_a_race_would_be_redrawn_under_another_value(
    tmp_path: Path, slow_draw: None
) -> None:
    """The consequence, asserted rather than argued: re-resolving is stable.

    Every state resolved concurrently above must resolve again to the very
    same blinding value. A lost entry shows up here as a *different* value
    for unchanged records -- the second ``C2``.
    """
    vault = BlindingVault.for_state_dir(tmp_path)
    digests = [f"sha256:{index:064x}" for index in range(1, 4)]
    drawn = _resolve_all(vault, digests)

    for digest in digests:
        again = vault.resolve(
            expose_id=_EXPOSE, slice_digest=digest, committed_at="2027-01-01T00:00:00Z"
        )
        assert again.blinding == drawn[digest]
        assert again.committed_at == "2026-07-01T00:00:00Z"


def test_one_resolve_is_unaffected_by_the_lock(tmp_path: Path) -> None:
    """The positive control: the ordinary single-caller path still draws and keeps."""
    vault = BlindingVault.for_state_dir(tmp_path)
    first = vault.resolve(
        expose_id=_EXPOSE, slice_digest="sha256:" + "aa" * 32, committed_at="2026-07-01T00:00:00Z"
    )
    assert first.ordinal == 1
    assert vault.find(expose_id=_EXPOSE, slice_digest="sha256:" + "aa" * 32) == first
