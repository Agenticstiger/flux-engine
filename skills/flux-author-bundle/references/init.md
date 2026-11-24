# What `fluxsim init` reads and records

Read this when you take path (a) or (b) of the skill. The command's own help (`fluxsim init --help`) is the authority for the installed engine; the pages are [the init reference](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/reference/cli/init.md), [write a bundle from a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/how-to/write-a-bundle-from-a-profile.md) and [ADR 0014](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/adr/0014-init-reads-every-statistic-a-profile-publishes.md).

## The three forms

| You give | It writes | Notes |
|---|---|---|
| nothing but a folder | the starter project: a small contract and a bundle that runs as it is | the folder must not exist, or must be empty |
| `--contract` | a profile request, beside the folder by default | `--request` names another place; it never overwrites a file |
| `--contract` and `--profile` | the bundle: the contract copied unchanged, and a Simulation declared from the profile | the profile stays outside the bundle; the Simulation pins its digest |

Nothing is ever overwritten: the folder must not exist yet, or must be empty. Before it writes a file, the command checks the bundle as `fluxsim validate` does, and as `plan` does from the documents alone, and writes nothing either refuses.

## What it reads

It reads the contract and the profile, never a record of the data. The contract is read for the columns, types, keys, relationships and rules the owner published. The profile is read for the aggregates the data owner's profiler released.

Every statistic of the profile is read, so every one ends up in one of three states, which the command prints as a ledger line:

- **used**: a number of the bundle rests on it, a rule read it, a property is scored against it, or the bundle was checked against it;
- **unused**: no rule of init reads it for that column. Each is named by its JSON Pointer with the reason;
- **withheld**: the profile published no value for it, because its disclosure checks held it back.

The ledger counts are in the output and in `--json`, which is the form to parse.

## What it records

- Each number of the Simulation is registered in its parameter register, the `parameters` list in `simulation.flux.yml`, with a source: a pointer into the profile and the statistic it lands in, a pointer into the contract, or an assumption with its reason, or the default of a setting.
- The statistics no number rests on are listed under `unused` beside the register. `plan`, `verify` and `ship` count them, and `ship` names each one in the evidence report as something not checked.
- Each assumption is stated in the output with its reason, for example the opaque levels of a category whose values the profile did not publish, or a time window when no date column publishes its range.
- A column is never drawn as one value; where the profile leaves a gap, init assumes, labels the assumption and says why.

## What `--strict` refuses

`--strict` writes nothing and exits 1 when a statistic the profile publishes is unused, or when an assumption stands in for something the profile did not publish (a gap). Each finding is named. Tolerances and the bounds of a committed state are settings, not gaps.

When it refuses:

1. Read every finding. Tell the user which statistics are unused and which gaps stand.
2. The remedy the command prints is to publish what the profile left out. That means a new profile request and a new profile from the data owner (the `flux-privacy-and-profiling` skill); do not edit the profile yourself.
3. Without `--strict`, the same bundle is written with its assumptions labelled. Offer that to the user as a choice that is theirs; do not pick it to get past the refusal.
4. Do not use any option to accept findings on the user's behalf. Whether an unused statistic or a gap is acceptable is a decision for the user.

## What to do with the output

1. Run `fluxsim validate bundle` (checkpoint 1).
2. Read the assumed numbers. Where the user knows better (the time window the data covers, the names of the levels, the margin a property accepts), edit the Simulation inside the bundle and change that number's register entry with it: give it the user's own reason. The same holds when you drop or edit a `counts` or `history` entry of the Simulation: drop or edit its register entries with it, since `plan` refuses an entry that reaches nothing (`FLX-3105`).
3. Do not change the contract copied into the bundle. If it is wrong, the owner changes it, and you start from the new one.
4. Hand over to the `flux-run-and-verify` skill for `plan`, `apply` and `verify`.

## Profiles under noise

A profile that carries statistics released under differential privacy is read as point estimates with a variance; the output lists each noisy statistic with its mechanism and scale. The privacy grade a run needs is the `flux-privacy-and-profiling` skill's matter; see [ADR 0020](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/adr/0020-privacy-grades.md) and `fluxsim explain FLX-4005`.

## A profile request

A profile request lists the statistics, column metrics and fact shares a profiler must measure for the contract. It names columns and registered identifiers and holds no value of the data. It is written in a plain YAML format of its own that is read with no float, no timestamp and unknown keys refused, so do not rewrite it by hand: give it to the data owner as it is.
