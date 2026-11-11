# ADR 0004: adapt Concordia and TinyTroupe's patterns; depend on neither

## Status

Accepted (W0)

## Context

Borrow-before-build search: "google-deepmind concordia generative
agent-based modeling 2026" (2026-09-06); fetched
https://github.com/google-deepmind/concordia (Apache-2.0, roughly 1.7k
stars). Concordia's v2 architecture is Entity-Component composition with a
Game Master entity that resolves actions via a language model; core ships
`call_limit_wrapper` (a call-budget guard) and `no_language_model` (a
deterministic stand-in for tests), but no seed/replay mechanism for the
language-model calls themselves.

Also fetched https://github.com/microsoft/TinyTroupe (MIT, experimental).
TinyTroupe's `control.begin/checkpoint/end` caches language-model outputs
keyed by run state, so a simulation replays byte-for-byte on a second run
without calling the model again.

FLUX's deterministic core (the v0.1/v0.2 scope) is seeded,
byte-stable, and language-model-free by construction -- putting either
library under the kernel would introduce a non-deterministic dependency
under the one property that scope promises. The agentic tier is also the
first thing cut if the build is squeezed (per the build plan's descope
order), so any dependency it takes must be easy to remove entirely: an
adapted pattern qualifies, a hard dependency on a research library with a
moving API does not.

Also considered: AgentTorch (differentiable calibration of language-model
archetypes) -- rejected outright as a dependency because it is AGPL-3.0,
whose terms would reach every downstream user of an Apache-2.0 library; its
archetype-sampling idea may still be adapted without incorporating any of its
code.

## Decision

Adapt, do not depend:

- Concordia's Entity-Component composition maps onto a FLUX `skill`
  attached to a kind; its Game Master role is played by the deterministic
  kernel itself (never a language model) -- a skill proposes content
  within bounds the kernel already enforces, it never resolves world
  state. `call_limit_wrapper` maps onto `agentPolicy.tokenBudget`
  enforcement; `no_language_model` maps onto an explicitly-selected CI
  stand-in port, never a silent default.
- TinyTroupe's checkpoint/replay cache maps onto keying every
  language-model call by (seed path, model pin, prompt digest, policy
  digest) and replaying byte-for-byte on a cache hit -- the mechanism the
  agentic tier (a separate, detachable distribution) uses to stay inside
  the determinism promise.
- AgentTorch's archetype-sampling idea may inform the agentic tier's
  design; no AgentTorch code or dependency.

## Consequences

- The agentic tier (a separate `flux-engine-agentic` distribution,
  discovered by entry points) can be removed entirely -- `pip uninstall`
  leaves the rest of the suite green -- because nothing in the kernel or
  the core distribution imports Concordia, TinyTroupe, or AgentTorch.
- Apache-2.0 (Concordia) and MIT (TinyTroupe) are both compatible with
  adapting a pattern inside an Apache-2.0 project, with attribution
  recorded in `THIRD_PARTY_LICENSES.md` if any literal code is ever
  copied (none is, as of W0).
