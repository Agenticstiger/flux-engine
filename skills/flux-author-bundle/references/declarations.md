# Declarations: where they go, how to check one, which page covers it

Read this when the user wants the bundle to say more than `fluxsim init` wrote. The pages named here are tested: copy a shape from one, never invent a key.

## Where a declaration goes

- **In the FLUX bundle, never in the FLUID contract.** Module facts live under the Simulation's `extensions`, in the `com.agentics.flux/1` namespace ([ADR 0018](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/adr/0018-module-facts-live-in-the-flux-bundle.md)). The contract is the data owner's promise to its consumers, so it gains no FLUX key, and a tolerance edit never moves its digest.
- **The namespace's keys are closed.** A key nothing reads is refused, not ignored. Per-table declarations sit under `emits`, keyed by the expose's id; some keys sit beside `emits`, such as the profile pin and `strictCoverage`.
- **Each declaration is opt-in.** A bundle that declares none keeps every byte and every digest it had.
- **Some things are documents of their own**, not extensions: a signal, a detector, a treatment and a journey are FLUX documents that reach a run only through a Module's resources, which the Simulation lists under `moduleRefs`.
- **The Simulation references the contract and never restates it.** It names the contract, the expose, and pins the contract's digest ([the seam](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/explanation/the-seam.md)).

## Which kernel

A declaration is read under the kernel the plan names. A new plan takes kernel 0.2.0 by default, `fluxsim plan` has `--kernel` to name 0.1.0 instead, and apply, replay, verify and ship run the kernel their plan names ([ADR 0010](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/adr/0010-faster-generation-through-a-second-kernel.md)). Say what the page of the declaration says, and no more:

- A catalogue, a reference expose, needs kernel 0.2.0; on kernel 0.1.0 the plan is refused with `FLX-3105`, which names the kernel ([publish a catalogue](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/publish-a-catalogue.md)).
- The keys of ADR 0022 (the table's last row) need kernel 0.2.0; under kernel 0.1.0 the plan is refused with `FLX-3105`, naming the kernel.
- The pages on journeys, drawing a column given another and calibration name no kernel. Say that the page names none; do not guess one.

If a plan is refused with a kernel in the message, read `fluxsim explain FLX-3105` and the page; do not switch kernels to get past it without the user's agreement, since the digests of a new plan differ between kernels.

## How to check one

1. Test an expression before you write it. `fluxsim expr model --bundle bundle` prints the attribute model the expression is checked against; `fluxsim expr eval "tenureMonths == 24" --bundle bundle --values '{"tenureMonths": 24}'` parses and evaluates one expression against a subject view you give as JSON. Both need `--bundle`; `expr eval` needs `--values`. `--simulation-id` picks one Simulation when a bundle holds several, and `--json` gives machine output. See [the expr reference](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/expr.md).
2. After the edit, `fluxsim validate bundle` (checkpoint 1). It reads only and writes nothing ([the validate reference](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/validate.md)).
3. Before generating, `fluxsim plan bundle` (checkpoint 2, the `flux-run-and-verify` skill). It reports columns that fell back to a type default. `--strict-coverage` refuses the run on such a column, and on a declaration the run does not generate (`FLX-3304`, `FLX-3305`); `fluxsim explain` says how to cover one. The bundle can make this a property with `strictCoverage: true` under the namespace, beside `emits`.
4. Run `fluxsim explain FLX-nnnn` on any refusal and make the change its remedy names, inside the bundle.

## Declaration families

| What the user wants | Where it goes | Page that covers it |
|---|---|---|
| Journeys: a second signal, detector, treatment and journey | separate FLUX documents, wired through a Module's resources and the Simulation's `moduleRefs` | [Add a journey](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/add-a-journey.md) |
| A column drawn given another, with its own weights per level | the emit's `fidelity` block, under `extensions` | [Draw a column given another](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/draw-a-column-given-another.md) |
| A small table that other tables point at, kept as the owner's own rows | the emit's block under `emits`, with the profile pinned | [Publish a catalogue whole](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/publish-a-catalogue.md) |
| Where a model's dials came from, and a minimum credibility an emit requires | a Playback document, and `minCredibility` on an emit | [Calibrate from aggregates](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/calibrate-from-aggregates.md) |
| A change to the contract's columns, its pinned digest, or its keys | the contract (the owner's) and the Simulation's `emits[]` entry | [Use the FLUID seam](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/use-the-fluid-seam.md) |
| Fidelity checks and the profile pin | the emit's `fidelity` block and `profile.digest` under the namespace | [Score a run against a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/score-a-run-against-a-profile.md) |
| Links between tables and copied columns | the namespace; the record says how, so read it first. The `flux-relational-modeling` skill covers them | [ADR 0022](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/adr/0022-rows-read-their-linked-rows.md) |

Calibration here is declared-scorecard calibration: nothing is fitted in this release, and the grade letter is the author's, not the engine's. Say so if the user expects a fit.

## Rules for every row of the table

- A declaration that needs a number from the data takes it from the profile, or from the user, and gets a register entry with its reason. Do not make one up.
- A grouped table of counts can single out a few real records. Where the user supplies counts, ask whether the data owner's disclosure rule has been applied; do not apply or invent one yourself.
- A sensitivity label, a privacy grade and the contract stay the owner's. A declaration never changes them.
