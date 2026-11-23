# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The boundary store: blinding values and the check counter -- §4.

    What leaves the boundary in v0.1: ``S``, ``C1``, ``C2``,
    ``stateOrdinal``, record count (or declared range), commitment time.
    **Never a record.** The check counter
    (``budget.maxChecksPerState``) lives here and refuses beyond budget
    (``FLX-5010``).

Everything the boundary keeps to itself lives in this one class, which is
what makes the boundary auditable: the blinding value and the counter are
the only boundary-private state there is, and neither is reachable from a
:class:`~flux_engine.concordance.boundary.slice.CommittedState` or from
anything the verifier sees.

Four rules the store enforces that a caller could otherwise skip.

**A blinding value is drawn once, when the state is formed.** §4 asks for
a *per-state* blinding value, and :meth:`BoundaryStore.commit` is the only
place this package draws one. It files the draw under the emit's
``exposeId`` and the digest of the records
(:func:`~flux_engine.concordance.boundary.slice.slice_content_digest`) in
a :class:`~flux_engine.concordance.boundary.vault.BlindingVault` under the
owner's state directory, and every later commitment of the same records --
a second ``ship``, a ``verify``, a re-ship -- reads that same value back
and recomputes the same ``C2``. A stage that drew its own per invocation
gave one unchanged record set a different ``C2``, a different fidelity
record and a different evidence archive on every call.

**A commitment is refused when its blinding value cannot be kept or
returned.** ``FLX-5013``, fail-closed. Drawing a replacement for a state
whose stored value has gone would publish a second ``C2`` for a record set
that has not changed.

**The counter is not resettable.** ``budget.maxChecksPerState`` bounds how
many questions one fixed record set answers; a caller able to reset the
count could ask an unbounded number and learn more from the aggregate
answers than the budget was set to allow. :meth:`BoundaryStore.record_check`
only ever increments, and the store exposes no way to lower a count.

**A state is bound to the ``G`` it was committed under.** §8: a change
that moves ``G`` requires a new committed state. Asking an existing state
a question under a different ``G`` is ``FLX-5011``, not a silent answer
about declarations the statistic was never formed under.

What the store keeps *across* processes is the identity of a committed
state and nothing else -- its ordinal, its commitment time and its
blinding value. The check counter stays in memory, and stays explicit
about it: it counts the questions one invocation asks of one state, and a
counter carried across invocations would also make the fidelity record of
an unchanged state differ from run to run (§6's ``checkOrdinal`` is inside
the signed body). Where that budget is enforced across processes is the
owner's infrastructure decision, not a file this class picks for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.concordance.boundary.merkle import LeafFunction
from flux_engine.concordance.boundary.slice import (
    CommittedState,
    SliceReference,
    commit_state,
    commitment_c2,
    refuse_weak_blinding,
    slice_content_digest,
)
from flux_engine.concordance.boundary.statistic import Row
from flux_engine.concordance.boundary.vault import BlindingVault
from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.errors import FLX_5010, FLX_5011, FLX_5012, FLX_5013, FLX_5035
from flux_engine.core.errors import FluxError

__all__ = ["BoundaryStore", "StateRecord"]


@dataclass(frozen=True)
class StateRecord:
    """What the boundary keeps about one committed state.

    ``blinding`` is here and, between processes, in the store's
    :class:`~flux_engine.concordance.boundary.vault.BlindingVault` -- both
    inside the boundary. It is not on
    :class:`~flux_engine.concordance.boundary.slice.CommittedState`, is
    not in any document that class renders, is not in the run directory or
    the evidence pack, and has no accessor on :class:`BoundaryStore` --
    the store uses it to recompute ``C2`` and hands back the comparison,
    never the value.
    """

    state: CommittedState
    blinding: bytes
    checks_used: int = 0


@dataclass
class BoundaryStore:
    """The boundary's private state, keyed by ``stateOrdinal``.

    *vault* is where a committed state's blinding value, ordinal and
    commitment time survive the process that formed them; without one this
    store holds states only for as long as it is alive and
    :meth:`commit` refuses (``FLX-5013``), because a commitment whose
    blinding value cannot be kept cannot be re-checked. Stages build the
    store with :meth:`for_state_dir`.
    """

    max_checks_per_state: int
    vault: Optional[BlindingVault] = None
    _states: Dict[int, StateRecord] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.max_checks_per_state < 1:
            raise FluxError(FLX_5010, "budget.maxChecksPerState is at least one check")

    @classmethod
    def for_state_dir(
        cls, state_dir: Union[str, Path], *, max_checks_per_state: int
    ) -> "BoundaryStore":
        """A store whose blinding values live under *state_dir*.

        ``<state_dir>/boundary/`` -- a sibling of ``runs/``, never inside
        one. Everything under a run directory is staged into the evidence
        archive, and a blinding value that travelled with the pack would
        blind nothing.
        """
        return cls(
            max_checks_per_state=max_checks_per_state,
            vault=BlindingVault.for_state_dir(state_dir),
        )

    def __repr__(self) -> str:
        """Name the ordinals held, never the blinding values."""
        return f"BoundaryStore(ordinals={sorted(self._states)!r})"

    @property
    def ordinals(self) -> Tuple[int, ...]:
        """The committed state ordinals this store holds, ascending."""
        return tuple(sorted(self._states))

    @property
    def latest(self) -> Optional[CommittedState]:
        """The highest-ordinal committed state, or ``None`` when empty."""
        if not self._states:
            return None
        return self._states[max(self._states)].state

    def commit(
        self,
        *,
        rows: Sequence[Row],
        compiled: CompiledFidelity,
        slice_reference: SliceReference,
        committed_at: Optional[str] = None,
        stamped_at: Optional[str] = None,
        leaf_function: Optional[LeafFunction] = None,
    ) -> CommittedState:
        """Fix *rows* as the committed state these records already are.

        The one path that draws a blinding value, and it draws at most one
        per record set. The records are named by
        :func:`~flux_engine.concordance.boundary.slice.slice_content_digest`;
        the vault is asked for that name under the emit's ``exposeId``, and

        * a record set the vault has never seen is drawn a blinding value
          (:func:`~flux_engine.concordance.boundary.slice.generate_blinding`),
          given the next ``stateOrdinal`` for that expose, and written
          back before any of it is used -- §8's "appended source records |
          new committed state (ordinal+1)";
        * a record set it has seen is re-formed under the ordinal, the
          commitment time and the blinding value it was formed under
          before, so ``C2`` and the fidelity record over it are the ones
          the previous check produced.

        *committed_at* is the owner's own statement of when they fixed the
        records (``fluxsim ship --committed-at``). It travels into this
        check's committed state, and it is what a state formed by this
        call is dated with, but it never re-dates a state the vault
        already holds: a re-declaration is a statement about this check,
        not a licence to move the commitment time of records that have not
        changed. *stamped_at* is what dates a **new** state when the owner
        declared nothing -- supplied by the caller, because nothing in
        this package reads the host clock.
        """
        if self.vault is None:
            raise FluxError(
                FLX_5013,
                "this boundary store keeps nothing between processes, so a blinding value "
                "drawn now could not be read back; build it with "
                "BoundaryStore.for_state_dir(<state dir>) to commit a state",
            )
        digest = slice_content_digest(rows)
        dated = committed_at or stamped_at
        if dated is None:
            raise FluxError(
                FLX_5013,
                "a committed state is dated by the owner's declaration or by an instant "
                "the caller supplies; this package reads no clock of its own",
            )
        vaulted = self.vault.resolve(
            expose_id=compiled.block.expose_id, slice_digest=digest, committed_at=dated
        )
        state = commit_state(
            rows=rows,
            compiled=compiled,
            ordinal=vaulted.ordinal,
            committed_at=committed_at or vaulted.committed_at,
            blinding=vaulted.blinding,
            slice_reference=slice_reference,
            leaf_function=leaf_function,
        )
        self.put(state, vaulted.blinding)
        return state

    def put(self, state: CommittedState, blinding: bytes) -> None:
        """Store *state* with the blinding value ``C2`` was taken under.

        Refuses a re-commitment of an ordinal already held: §4 says
        ``stateOrdinal`` increments per state, so reusing one would let a
        record set be replaced under a name a record already refers to.

        Refuses (``FLX-5035``) a *blinding* that is not the one ``C2`` was
        taken under: a store that answered
        :meth:`confirm_commitment` from a value the published commitment
        was not made with would report every commitment false and explain
        nothing.
        """
        refuse_weak_blinding(blinding)
        if commitment_c2(state.statistic, blinding) != state.c2:
            raise FluxError(
                FLX_5035,
                f"state {state.ordinal}'s published C2 was not taken under the blinding "
                "value it is being stored with",
            )
        if state.ordinal in self._states:
            raise FluxError(
                FLX_5035,
                f"stateOrdinal {state.ordinal} is already committed; the ordinal increments "
                "per state",
            )
        if self._states and state.ordinal <= max(self._states):
            raise FluxError(
                FLX_5035,
                f"stateOrdinal {state.ordinal} does not increment past " f"{max(self._states)}",
            )
        self._states[state.ordinal] = StateRecord(state=state, blinding=blinding)

    def get(self, ordinal: int) -> CommittedState:
        """Return the committed state at *ordinal*."""
        try:
            return self._states[ordinal].state
        except KeyError:
            raise FluxError(FLX_5035, f"no committed state at ordinal {ordinal}") from None

    def checks_used(self, ordinal: int) -> int:
        """How many checks this state has already answered."""
        self.get(ordinal)
        return self._states[ordinal].checks_used

    def record_check(self, ordinal: int, *, g: str) -> int:
        """Charge one check against *ordinal*'s budget and return its ordinal.

        Refuses ``FLX-5011`` when *g* is not the ``G`` the state was
        committed under, and ``FLX-5010`` once the state has answered
        ``max_checks_per_state`` checks. The returned number is §6's
        ``committedState.checkOrdinal`` -- 1 for the first check of a
        state -- so a record says which of the state's budgeted checks it
        is, and two records over one state are never the same document.
        """
        record = self._states.get(ordinal)
        if record is None:
            raise FluxError(FLX_5035, f"no committed state at ordinal {ordinal}")
        if record.state.g != g:
            raise FluxError(
                FLX_5011,
                f"state {ordinal} was committed under a different G; a new committed state "
                "is required",
            )
        if record.checks_used >= self.max_checks_per_state:
            raise FluxError(
                FLX_5010,
                f"state {ordinal} has answered its budget of {self.max_checks_per_state} checks",
            )
        used = record.checks_used + 1
        self._states[ordinal] = StateRecord(
            state=record.state, blinding=record.blinding, checks_used=used
        )
        return used

    def confirm_commitment(self, ordinal: int, c2: str) -> bool:
        """Whether *c2* is the commitment this store took over that state.

        The comparison happens inside the boundary and only its result
        leaves, so a caller learns whether a published ``C2`` matches
        without the blinding value that would let them recompute it.
        """
        record = self._states.get(ordinal)
        if record is None:
            raise FluxError(FLX_5035, f"no committed state at ordinal {ordinal}")
        from flux_engine.concordance.boundary.slice import commitment_c2

        return commitment_c2(record.state.statistic, record.blinding) == c2

    def require_fresh(self, ordinal: int, *, now_ms: int, max_age_ms: Optional[int]) -> int:
        """Refuse (``FLX-5012``) a state older than *max_age_ms*; return its age.

        The current instant is supplied by the caller: nothing in this
        package reads the host clock, so the age is a comparison of two
        declared instants rather than of one declaration and one
        observation.
        """
        state = self.get(ordinal)
        age = now_ms - state.committed_at_ms
        if age < 0:
            raise FluxError(
                FLX_5012,
                f"state {ordinal} was committed after the instant it is being checked at",
            )
        if max_age_ms is not None and age > max_age_ms:
            raise FluxError(
                FLX_5012,
                f"state {ordinal} is {age} ms old, past the declared maximum of {max_age_ms} ms",
            )
        return age

    def snapshot(self) -> Mapping[int, int]:
        """The check count per ordinal -- the only counter view there is."""
        return {ordinal: record.checks_used for ordinal, record in sorted(self._states.items())}
