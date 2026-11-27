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
masked, and the run id is the first sixteen hex digits of it. `apply` and `run` refuse a plan whose `inputDigest` or `planDigest` no
longer matches.

| Key | Holds |
|---|---|
| `simulationId`, `seed`, `fidelity` | The Simulation the plan was made for, its seed and its fidelity mode. |
| `inputDigest`, `planDigest` | The digest of the closed set of input files, and the digest of the plan. |
| `kernelVersion`, `seedTreeVersion` | The kernel and the seed-tree wire version the plan was made with. A recorded plan keeps replaying under them. |
| `seedTreeDigest`, `seedTreeShapes` | The digest of `seed-paths.ndjson`, and a summary of the seed-tree shapes the run used. |
| `columnProvenanceDigest` | The digest of `column-provenance.json`. |
| `emits` | One entry for each emitted table, with its digests (`G`, `H`), whether it declares fidelity, its `generator` plan and its `productRef`. |
| `generated_at` | When the plan was made. It is masked out of `planDigest`. |

### Keys that appear only when a plan needs them

A key in this list is absent from every other plan, so a plan that does not need it keeps its bytes and its digest.
A recorded plan without the key replays the way it was made, so an engine that learns a new behaviour does not change
an old run.

| Key | Where | Meaning |
|---|---|---|
| `projectedGivens` | Top level | `"afterProjection"`: a column drawn `given` a column of a history's version is drawn after the projection, given the level the version holds. `"afterOverrides"`: the same, where a periods entry's kinds override the column it is drawn given. |
| `projectedReads` | Top level | `"final"`: a link into a projected emit reads the rows the projected emit writes. |
| `historyEndShare` | Top level | `"entities"`: a history's `end` share counts every entity. |
| `dateRanges` | Top level | A column's dates were cut at the range its contract declares. |
| `declaredRows`, `shortfall` | An emit's entry | A history that keeps to its parent's lifetime holds fewer rows than it declared. `declaredRows` is what the bundle asked for, and `shortfall` is how many rows are missing. Only an emit with a shortfall above zero has them. `plan` and `verify` count the rows the history has. |
| `association` | An emit's entry | The relationships of an association table, `distinct`, its rows, the rows moved to meet a cap or the Gale-Ryser condition, what the repair did, and each side's distance from its shape. See [give every parent its children](../how-to/give-every-parent-its-children.md#association-tables). |

## The run directory

| File | Written by | Holds |
|---|---|---|
| `plan.json` | `plan` | The plan above. |
| `seed-paths.ndjson` | `plan` | Every seed path the run materialised, sorted, one canonical path on each line. |
| `population.ndjson`, `events.ndjson` | `apply` | The generated population and its events. |
| `emits/<expose>.ndjson` | `plan`, then `apply` | The rows of one emitted table. `plan` stages them, and `apply --run-id` commits them. |
| `seam-attestation-<expose>.json` | `apply` | The attestation of one emit against its contract. |
| `column-provenance.json` | `apply` | Where each column's draw came from. |
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
| `replacedBy` | For each assumed number, the pointer and definition of the statistic that would replace it, and how many assumed numbers it replaces. |

Beside `ledger`, `gaps` lists the assumptions that stand in for what the profile did not publish, and `historyFile`
names the file given to `--history`, when there is one. A profile with a noisy history or `nullCount/2` adds its
entries to `statistics`, and a NULL share set aside for a clique column is named there with the share. See
[write a bundle from a profile](../how-to/write-a-bundle-from-a-profile.md).
