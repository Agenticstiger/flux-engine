---
title: "The plan and the run directory"
description: "What plan.json holds, the keys that appear only when a plan needs them, the files a run directory carries, and the ledger in the output of init --json."
---

# The plan and the run directory

`fluxsim plan` writes a run directory, `.flux/runs/<run id>/`, and `plan.json` inside it. `fluxsim apply` adds the
run's files to the same directory. This page lists what is in them. The commands are in the
[command line reference](./cli/README.md), and the rules are in the
[kernel specification](../spec/KERNEL-SPEC.md).

## `plan.json`

`plan.json` is canonical JSON. Its `planDigest` is the digest of the document with `planDigest` and `generated_at`
masked. The run id is the first sixteen hex digits of it. `apply` and `run` refuse a plan whose `inputDigest` or
`planDigest` no longer matches.

| Key | Holds |
|---|---|
| `simulationId`, `seed`, `fidelity` | The Simulation the plan was made for, its seed and its fidelity mode. |
| `inputDigest`, `planDigest` | The digest of the closed set of input files, and the digest of the plan. |
| `kernelVersion`, `seedTreeVersion` | The kernel and the seed-tree wire version the plan was made with. A recorded plan keeps replaying under them. |
| `seedTreeDigest` | The digest of `seed-paths.ndjson`. |
| `seedTreeShapes` | A table of `{canonical path shape name: number of seed paths of that shape}`, in sorted key order, for example `{"world.subject.trait": 75000}`. The shapes are the closed table of [KERNEL-SPEC](../spec/KERNEL-SPEC.md) section 3, and the paths themselves are in `seed-paths.ndjson`. |
| `privacyGrade` | The privacy grade of the run: `P1`, `P3` or `P5`. It is written only when the Simulation pins a profile or declares a generator plugin. |
| `columnProvenanceDigest` | The digest of `column-provenance.json`. |
| `emits` | One entry for each emitted table. See [The entry for an emit](#the-entry-for-an-emit). |
| `generated_at` | When the plan was made. It is masked out of `planDigest`. |

### The entry for an emit

Each key of `emits` is an expose. These members are in every entry:

| Key | Holds |
|---|---|
| `H`, `G` | The two digests of the expose's seam. `H` is the digest of the contract, the emit entry, the extension entry and the column names. `G` is the same with every tolerance, every `minVariance` and `budget.maxStateAge` left out, and with every declaration that shapes the rows but not the committed state: links, counts, bridges, associations, history and the like. A tolerance changes `H` and keeps `G` ([SEAM-FIDELITY-SPEC](../spec/SEAM-FIDELITY-SPEC.md) section 3). |
| `productRef` | The product the expose belongs to. |
| `declaresFidelity` | Whether the expose declares a fidelity block. |
| `generator` | `generatorDigest`, `rows` and the `columns` the emit writes, and `hiddenColumns` when some column is drawn and never written. It is `null` for an emit with no generator plan. |

### Keys that appear only when a plan needs them

A key in these tables is absent from every other plan, so a plan that does not need it keeps its bytes and its digest.
A recorded plan without the key replays the way it was made, so an engine that learns a new behaviour does not change
an old run.

Top level:

| Key | Meaning |
|---|---|
| `profileDigest` | The digest of the profile the Simulation pins. |
| `scorecard` | The thresholds the Simulation declares for its scorecard: a `thresholds` list of `target`, `operator` and `value`, and a `shape` member with `maxFlags` when it declares a shape limit. `thresholds` is written only when the Simulation declares thresholds, or declares no shape limit; a declaration of a shape limit alone has no `thresholds`. `verify` and `ship` read them through the plan's digest. |
| `overlay` | The profile overlay the Simulation declares, as `name` and `patch`, so `verify` and `ship` score against the overlaid profile. |
| `parameterRegisterDigest` | The digest of `parameter-register.json`, for a Simulation that declares a register. |
| `generators` | For each expose a plugin generates: the plugin, its distribution and version, whether the install is editable, its grade, the reproducibility the run is recorded with (`none` for an editable install), the environment digest and, when declared, the privacy account and the model digest. |
| `references` | The rows the pinned profile publishes for each reference expose, as `columns` and `rows`, so `apply --run-id`, `replay` and `verify` need no profile for them. |
| `responseEstimates` | The estimates the response families took from the pinned profile, as `estimates` and `unestimated`. |
| `replicate` | The run `r` of `R` that a forecast's replicate is, with the `effects` it took, so `replay` applies the same ones. |
| `scale` | The `--scale` of the plan, as text, when it is not 1. |
| `scaleFloors` | `"trackedLevels"`: under a `--scale` below 1, a parent was kept at more rows than the scale gives it, so that its children's tracked levels still occur. |
| `maxRows` | The budget `--max-rows` set for the rows a periodic emit may derive, when it is not the default. |
| `rangeSetters` | `"held"`: a range of the contract cut a history's layout or the periods of a periods entry. |
| `dateRanges` | `"cut"`: a column's dates were cut at the range its contract declares. |
| `projectedGivens` | `"afterProjection"`: a column drawn `given` a column of a history's version is drawn after the projection, given the level the version holds. `"afterOverrides"`: the same, where a periods entry's kinds override the column it is drawn given. |
| `projectedReads` | `"final"`: a link into a projected emit reads the rows the projected emit writes. |
| `historyEndShare` | `"entities"`: a history's `end` share is a share of every entity. It is written only when a history's `end` entry leaves some entity unable to end. A plan without the key (engine 0.11.1 or earlier) replays with the share taken over only the entities that can end. |

In an emit's entry:

| Key | Meaning |
|---|---|
| `rows`, `rowsFrom` | A projected emit's derived row count, and where it comes from: `of`, the history, and `projection`, its kind. |
| `declaredRows`, `shortfall` | A history that keeps to its parent's lifetime holds fewer rows than it declared. `declaredRows` is what the bundle asked for, and `shortfall` is how many rows are missing. Only an emit with a shortfall above zero has them. `plan` and `verify` count the rows the history has. |
| `copula` | `alpha`, the shrinkage of the copula, and `factor`, the digest of its factor. See [draw number columns together](../how-to/draw-number-columns-together.md). |
| `countsGiven` | One member for each counted relationship whose `children` are given a parent column: the `relationship`, the `given` column, the weights of every class in `childrenGiven`, and `childrenGivenDefault`, the classes that took the shape's own weights because the entry gave them none. |
| `association` | The relationships of an association table, `distinct`, its rows, the rows moved to meet a cap or the Gale-Ryser condition, what the repair did, and each side's distance from its shape. See [give every parent its children](../how-to/give-every-parent-its-children.md#association-tables). |
| `fidelity` | The compiled fidelity block of an emit that declares one: its encoding, its properties and every tolerance with the margin the comparison used. |
| `hidden` | `true` for a hidden table: it is in the plan with its generator digest and in no stream of the run. |

## The run directory

| File | Written by | Holds |
|---|---|---|
| `plan.json` | `plan` | The plan above. |
| `seed-paths.ndjson` | `plan` | Every seed path the run materialised, sorted, one canonical path on each line. |
| `population.ndjson`, `events.ndjson` | `apply` | The generated population and its events. |
| `.stage/` | `plan` | The rows and files `apply` would write, staged by the plan, so that `apply --run-id` commits them without generating again. `apply` removes it. `plan --no-stage` writes none. |
| `emits/<expose>.ndjson` | `apply` | The rows of one emitted table, one file for each expose. `apply --run-id` writes them from the staged rows. |
| `seam-attestation-<expose>.json` | `apply` | The attestation of one emit against its contract. |
| `column-provenance.json` | `apply` | Where each column's draw came from. |
| `parameter-register.json` | `apply` | What each number the run reads rests on, for a Simulation that declares a register. |
| `decisions.ndjson`, `decisions.meta.ndjson`, `suppressions.ndjson`, `lineage.ndjson` | `apply` | The run's decision log, its metadata, the suppressed rows and the lineage records. |
| `run-digest.json`, `receipt.json` | `apply` | The run digest, and the receipt that names the digests of the run. |

The set of files can grow with the sinks and the plugins a run uses. `fluxsim verify` and `fluxsim ship` read the
directory, so do not edit a file in it.

## The ledger in `init --json`

`fluxsim init --json` prints one document. Its `ledger` member lists what the command read of the profile:

| Key | Holds |
|---|---|
| `counts` | How many statistics are used, unused and withheld. |
| `statistics` | Every statistic, by its JSON Pointer, with its state. |
| `unused` | What an owner can leave out of the profile: a pointer, its definition and the reason. An entry whose members are all unused is named whole. |
| `replacedBy` | One entry for each statistic that would replace assumed numbers: its pointer, its definition and `assumptions`, the count of numbers it replaces. |

Beside `ledger`, `gaps` lists the assumptions that stand in for what the profile did not publish, and `historyFile`
names the file given to `--history`, when there is one. A profile with a noisy history or `nullCount/2` adds its
entries to `statistics`, and a NULL share set aside for a clique column is named there with the share. See
[write a bundle from a private release](../how-to/write-a-bundle-from-a-private-release.md).
