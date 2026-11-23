# Map of the FLUX engine's documentation

Each page is linked as it stands in the release these skills came with. In a clone of the repository the
same page is the file under `docs/` with the same path. Where a page and the help of the installed fluxsim disagree, follow
the help.

## Start

- [Documentation home](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/README.md): the way in to every section below.

## Tutorial

- [Tutorial](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/tutorial/README.md): the lesson from nothing to a verified run and an evidence pack.
- [First evidence pack in five minutes](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/tutorial/first-evidence-pack.md): create a small project, run it, verify it, ship it, then change one number and see what moves.

## How-to guides

- [How-to guides](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/README.md): index of the goal-sized guides.
- [Add a journey](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/add-a-journey.md): add a signal, detector, treatment and journey, and fix the two mistakes that stop one running.
- [Calibrate from aggregates](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/calibrate-from-aggregates.md): declare a scorecard for a Playback and read it back with `fluxsim calibrate`.
- [Check consumer queries, calibrate to them](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/check-consumer-queries.md): declare the SQL consumers run, check and count it on a run, read the queries in the scorecard, and calibrate with held-out queries deciding.
- [Draw a column given another](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/draw-a-column-given-another.md): declare a histogram column drawn given a categorical one, and how the fidelity gate checks the link.
- [Fix values and seed rows](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/fix-values-and-seed-rows.md): declare conditions so a group of rows holds fixed values and the rest is drawn given them, with seed rows as groups of one.
- [Forecast the next state and score it](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/forecast-the-next-state.md): declare a response family and forecasts, make them before the next profile exists, and score them against it.
- [Give every parent its children](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/give-every-parent-its-children.md): declare how many children each parent has with a counts entry, so every parent gets its children and the number per parent follows its histogram.
- [Load a simulation into a database](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/load-a-simulation-into-a-database.md): write a run to files or load it into a database with the `--sink` option of `fluxsim run`.
- [Publish a catalogue whole](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/publish-a-catalogue.md): declare a small table as a reference expose so other tables pick only its rows.
- [Score a run against a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/score-a-run-against-a-profile.md): pin a profile, then check a run in full or on a sample of blocks.
- [Seal and score a prediction](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/seal-and-score-a-prediction.md): register a prediction before the outcome, score it later, publish an outcomes-only record.
- [Ship with evidence](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/ship-with-evidence.md): pack a run into an evidence archive, pass the fidelity gate, sign it, use the JSON output in CI.
- [Size a run, split it and time it](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/size-and-split-a-run.md): plan at a multiple of the size, generate in shares, see where the time went.
- [Use the FLUID seam](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/use-the-fluid-seam.md): change a contract, carry its new digest into the Simulation, check the emitted rows.
- [Write a bundle from a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/write-a-bundle-from-a-profile.md): turn a contract and a profile into a bundle, see what each number rests on, then run and ship it.

## Explanation

- [Explanation](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/explanation/README.md): index of the background pages.
- [Architecture and patterns](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/explanation/architecture.md): how the parts, packages and rules between them fit together, and the life of a run.
- [Determinism and the seed tree](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/explanation/determinism-and-the-seed-tree.md): why one bundle and seed give the same bytes, and what the input digest covers.
- [The evidence pack](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/explanation/the-evidence-pack.md): what `fluxsim ship` writes, what a recipient can check offline, and what the pack does not say.
- [The FLUID seam](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/explanation/the-seam.md): why a Simulation references a contract instead of restating it.
- [The six-step loop](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/explanation/the-six-step-loop.md): why a run is validate, plan, apply, run, verify, ship, and what each step reads and refuses.

## Reference

- [Reference](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/README.md): index of the command line, codes, Python API and schemas.
- [Python API](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/python-api.md): the extension contract and the library entry point, name by name.
- [Schemas](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/schemas.md): the FLUX and FLUID schemas this version validates against.

### Command line

- [Command line](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/README.md): every command with its options, exit codes and environment.
- [fluxsim accuracy](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/accuracy.md): the outcome-accuracy gate (register, score, record).
- [fluxsim apply](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/apply.md): apply a recorded plan and write the run directory.
- [fluxsim calibrate](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/calibrate.md): read the declared scorecard of a Playback.
- [fluxsim describe](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/describe.md): describe the installation, including the command tree as data.
- [fluxsim explain](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/explain.md): print the catalog entry for one FLX code, or list them all.
- [fluxsim expr](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/expr.md): evaluate an expression and print its attribute model.
- [fluxsim gate](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/gate.md): the enforcement gate (decide, conform).
- [fluxsim init](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/init.md): write a small working project, or a bundle from a contract and a profile.
- [fluxsim plan](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/plan.md): execute the loop once and record the plan.
- [fluxsim plugins](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/plugins.md): list the installed vertical packs and sinks.
- [fluxsim replay](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/replay.md): re-execute a recorded plan and assert its digest.
- [fluxsim run](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/run.md): plan and apply in one step, with verify, sink and ship options.
- [fluxsim ship](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/ship.md): the fidelity gate, then the evidence pack.
- [fluxsim validate](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/validate.md): validate a bundle.
- [fluxsim verify](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/verify.md): golden regression, row conformance, the enforcement gate and fidelity.

### Error codes

- [Error codes](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/errors/README.md): every FLX code by family; each code also has its own page at the same address with the code and `.html` added.

The thousands digit of a code names the family:

| Family | Part of the engine | Look here for |
|---|---|---|
| 1000 | canonicalisation | a value that has no canonical JSON form, or keys that collide |
| 2000 | plan and replay binding | a plan, run directory, digest or golden that no longer matches |
| 3000 | seam and conformance | a bundle that disagrees with its contract, or rows that break it |
| 4000 | governance and sovereignty | privacy grade floors and what a run may read |
| 5000 | numerics | arithmetic and distribution inputs outside their domain |
| 6000 | expression language | a parse or evaluation error in an expression |
| 7000 | calibration | declared scorecards and predictions |
| 8000 | evidence and pack | the archive `ship` writes and what a recipient checks |

See the [error codes index](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/errors/README.md) for the codes in each family.

## Specifications

- [Specifications](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/README.md): index of the rule-by-rule documents.
- [Kernel](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/KERNEL-SPEC.md): the first kernel's seed tree, draws, clock, emit stream, keys and run digest.
- [Kernel v2](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/KERNEL-SPEC-2.md): the second kernel and what it keeps from the first.
- [Seam generator and fidelity check](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/SEAM-FIDELITY-SPEC.md): how rows are generated in a contract's shape and the gate `ship` runs.
- [flux-expr/1](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/EXPR-SPEC.md): the closed grammar of a Segment's query.
- [flux-expr/2](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/EXPR-SPEC-2.md): derived columns and rules about valid data.
- [flux-expr/3](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/EXPR-SPEC-3.md): flux-expr/2 plus date parts, date arithmetic and concatenation.
- [Profile](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/PROFILE-SPEC.md): the aggregate document a data owner makes, and how a Simulation pins it.
- [Profile request](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/PROFILE-REQUEST-SPEC.md): what a profiler must measure for a bundle.
- [Scorecard](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/SCORECARD-SPEC.md): how a run is scored against its pinned profile, and the thresholds.
- [Sample-based verification](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/SAMPLE-SPEC.md): checking a run on a sample of blocks, with intervals.
- [Parameter register](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/spec/PARAMETER-REGISTER-SPEC.md): what each number a run reads rests on.

## Decision records

- [Decision records](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/adr/README.md): the index; each record says what was decided, why, and what else was weighed.
