# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The agentic tier's two ports -- both propose-only.

ADR-0004 fixes the division of labour: "a skill proposes content within
bounds the kernel already enforces, it never resolves world state." The
deterministic kernel plays the resolving role a generative agent framework
would hand to a language model. That is why both ports here return a
*proposal*: nothing on this surface can write a fact into the world, and
so nothing on it can move a run's outcome except through a kernel decision
that was going to be made anyway.

The other half of ADR-0004 is replay. Every call is keyed by the seed path,
the model pin, the prompt digest, and the policy digest, so a second run
over the same bundle reaches the same content from cache without calling
out again. :class:`SkillInvocation` carries exactly that key, which is why
it carries no free-form request field.

Nothing in the core distribution imports this module's implementations;
the agentic tier ships separately and can be uninstalled without touching
anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from flux_engine.api.digests import DigestRef

__all__ = ["ModelPort", "SkillInvocation", "SkillProposal", "SkillRunner"]


@dataclass(frozen=True)
class SkillInvocation:
    """One call, addressed by everything that makes it reproducible.

    Attributes
    ----------
    skill_name
        The skill attached to the kind.
    seed_path
        The canonical seed-tree path for this call (KERNEL-SPEC section 3's
        ``("sim", sim_id, "agentic", skill_name, "subject", ordinal,
        call_ordinal)``). Derivation is by path, never by call order.
    subject_ordinal, call_ordinal
        Position within that path, carried separately so a runner can log
        and budget per subject without re-parsing the path.
    prompt_digest
        The rendered request, digested.
    policy_digest
        The governing policy, digested. A policy change changes the key and
        therefore invalidates the cached content -- it cannot silently
        reuse content produced under the old policy.
    token_budget
        The hard cap for this call. A runner that would exceed it declines
        the call rather than truncating it.
    """

    skill_name: str
    seed_path: tuple[str, ...]
    subject_ordinal: int
    call_ordinal: int
    prompt_digest: "DigestRef"
    policy_digest: "DigestRef"
    token_budget: int


@dataclass(frozen=True)
class SkillProposal:
    """Content offered to the kernel, which decides what to do with it.

    Attributes
    ----------
    content
        The proposed text. It is an input to a kernel decision, never a
        world state change.
    model_id
        The pinned model identity the content came from.
    tokens_used
        Charged against :attr:`SkillInvocation.token_budget`.
    replayed
        True when the content came from the replay cache rather than a
        fresh call. Recorded, because "the same answer" and "the same
        answer again without asking" are different facts about a run.
    content_digest
        The proposal, digested, so the run's trace can bind to it without
        carrying the text.
    """

    content: str
    model_id: str
    tokens_used: int
    replayed: bool
    content_digest: "DigestRef"


@runtime_checkable
class ModelPort(Protocol):
    """The one place a language model can be reached. Propose-only.

    An implementation may only return content. It has no path to the
    scheduler, the trace, or any emitted row, so no choice it makes can
    become a fact about the world without a kernel decision in between.
    """

    model_id: str

    def propose(self, invocation: SkillInvocation) -> SkillProposal:
        """Return content for *invocation*, within its token budget."""


@runtime_checkable
class SkillRunner(Protocol):
    """Runs a skill through a :class:`ModelPort`, under budget and replay.

    The runner owns the cache lookup, the budget accounting, and the
    refusal when either fails. It is also propose-only: its return value is
    a proposal, and the kernel remains the only thing that resolves state.

    **Reserved** (:data:`flux_engine.api.RESERVED_PORTS`): this build ships
    no implementation of this shape and no parameter through which a
    consumer could inject one. :class:`AIGate.invoke
    <flux_engine.governance.gate.AIGate>` reaches the model port directly.
    """

    def run(self, invocation: SkillInvocation, model: ModelPort) -> SkillProposal:
        """Return the proposal for *invocation*, from cache when the key matches."""

    def budget_remaining(self, skill_name: str) -> int:
        """Tokens still available to *skill_name* under the governing policy."""
