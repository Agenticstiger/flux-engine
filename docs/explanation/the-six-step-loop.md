---
title: "The six-step loop"
description: "Why the FLUX Simulation Engine splits a run into validate, plan, apply, run, verify and ship, and what each step reads, writes and refuses."
---

# The six-step loop

```console
$ fluxsim init demo
...
$ cd demo
```

`fluxsim` has six steps for one job: turn a FLUX bundle into rows and an evidence pack. This page explains why they are separate.

## Why not one step

One "go" command would answer four questions at once: is the bundle well formed, what will the run do, did it do it, can I hand the result on. A failure at the end would tell you little, and a pass would tell you only that nothing crashed.

Splitting the loop gives each question its own step, its own files and its own way of saying no. The steps are `validate` (is the bundle well formed), `plan` (what will the run do), `apply` (do it), `verify` (does the result still hold up) and `ship` (can it leave the building). `run` is plan plus apply in one call.

Every artifact of a run lives in `.flux/runs/<runId>/`. The golden baseline that `verify --update-golden` records, and the state a fidelity check keeps, sit beside `runs/` in the same state directory. The run id is the first 16 hex digits of the plan's own digest. It is derived, never chosen, so planning the same bundle twice lands in the same directory.

## Walking the steps one at a time

`validate` reads the bundle and writes nothing.

```console
$ fluxsim validate .
.: valid -- 9 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
```

`plan` runs the whole simulation once in memory, exactly as `apply` will, and writes down what it saw. It does not write rows, though it creates the empty `emits/` directory they will go in.

```console
$ fluxsim plan .
plan written to .flux/runs/8dec55892096169f/plan.json (runId=8dec55892096169f, planDigest=sha256:8dec55892096169f5b6a9281a379f5dc9c085418ad2d22d52543a34835c26bb3)
$ ls .flux/runs/8dec55892096169f
emits
plan.json
seed-paths.ndjson
```

`apply` runs the plan and writes everything else.

```console
$ fluxsim apply . --run-id 8dec55892096169f
applied to .flux/runs/8dec55892096169f (runDigest=sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f)
emitted payment_recovery_moment: 154 rows (scenario)
$ ls .flux/runs/8dec55892096169f
decisions.meta.ndjson
decisions.ndjson
emits
events.ndjson
lineage.ndjson
plan.json
population.ndjson
receipt.json
run-digest.json
seam-attestation-payment_recovery_moment.json
seed-paths.ndjson
suppressions.ndjson
```

The rows are in `emits/`, one file per exposed stream. `receipt.json` is written last, in one atomic rename, and the later steps read its presence as "this run completed".

```console
$ fluxsim verify . --run-id 8dec55892096169f
reproducible: not checked | validity: not asserted
...
$ fluxsim ship . --run-id 8dec55892096169f -o out
...
shipped out/demo.flux.tgz
$ ls out
demo.attestation.json
demo.flux-manifest.json
demo.flux.tgz
demo.runtime-evidence.json
```

`fluxsim run . --verify --ship -o out` does all of this in one call. It is plan plus apply, then the optional steps, and it stops at the first step that exits non-zero.

## Why plan records digests, and apply refuses

A plan is only useful if apply cannot quietly do something else. So the plan writes down two digests, and `apply` checks both before it writes a single row.

- `inputDigest` covers the bundle's documents by their raw bytes (and `flux.lock` if present). If any file moved since the plan, `apply` stops with `FLX-2101`.
- `planDigest` covers `plan.json` itself, with its own digest and its timestamp masked. If someone edited the plan by hand, `apply` stops with `FLX-2102`.

Even a comment counts, because the digest is over bytes:

```console
$ cp world.flux.yml ../world.orig
$ echo "# a note" >> world.flux.yml
$ fluxsim apply . --run-id 8dec55892096169f   # exit 1
FLX-2101: the bundle's inputDigest has moved since the plan was recorded -- plan recorded sha256:... bundle is now sha256:...
...
$ cp ../world.orig world.flux.yml
```

The rule is strict and has a cost: a harmless edit means a new plan and a new run id, in a new directory beside the old one. The benefit is that "the plan I read" and "the run I got" cannot drift apart.

## Why verify has five legs

`verify` re-checks an applied run without re-running the simulation. First it confirms that the run still belongs to the bundle: it reads `plan.json` back (`FLX-2113`) and recomputes the bundle's `inputDigest` (`FLX-2101`). Then it has five legs, and they are independent on purpose. One leg passing says nothing about another.

1. **Artifacts.** Every component of `run-digest.json` is recomputed from the files on disk. This catches an emit stream edited after `apply` wrote it.
2. **Golden regression.** The run digest is compared with the recorded baseline for the Simulation. It compares digests; nothing is run again. With no baseline it says `not checked`, not "passed".
3. **Row conformance.** Each stream's row count must match the receipt, and each row's width must match the columns its seam attestation declares.
4. **RFC-04 vectors.** The enforcement vectors that ship with the engine are replayed against the governance gate.
5. **Fidelity.** An emit that declares a fidelity check is measured against the slice you pass with `--committed-slice`, and reports `declared, not checked` when you pass none. One that declares no check reports `not declared`.

Here is a run whose emit stream was edited after the fact:

```console
$ echo '{}' >> .flux/runs/8dec55892096169f/emits/payment_recovery_moment.ndjson
$ fluxsim verify . --run-id 8dec55892096169f   # exit 1
reproducible: not checked | validity: not asserted
  golden: not checked (no golden baseline recorded yet -- record one with `--update-golden --reason "<why>"`)
  artifacts: 1 component(s) do not reproduce: emits.payment_recovery_moment
  rows: 1 problem(s) -- payment_recovery_moment: receipt declares 154 rows, file has 155
  rfc-04 vectors: 0 failure(s)
  fidelity payment_recovery_moment: not declared
...
```

The headline still reads `not checked`, because it describes the golden leg only. That also means it can read `reproducible: yes` over a run another leg has failed. The exit code carries the failure, so read that, not only the headline.

## Two verdicts, never one

The headline has two halves joined by a bar: `reproducible: ... | validity: ...`.

`reproducible` answers "does this run's digest match the baseline we recorded?" It is `reproducible`, `not reproducible` or `not checked`, and it compares digests: nothing is run again. (`fluxsim replay` is the check that runs the plan again.) `validity` answers "does the simulation resemble the world?" Nothing in this release measures a predicted outcome against the world, so it always says `not asserted`. A fidelity check compares the rows' statistics with a slice you supply, which supports a statistical relation and nothing wider, so the headline stays `not asserted` either way.

They are never combined because they are different claims. A run can repeat exactly and still describe nothing real. Folding them into one word would let the first stand in for the second.

## Why ship checks again and may withhold

`ship` is the step where a result leaves the building, so it checks again instead of trusting that `verify` ran. Before it packs anything it recomputes every component of the run digest (`FLX-2110`) and compares the bundle's contract with the one the run recorded. Then it runs the fidelity gate for every emit that declares a check.

If any gate withholds, the whole ship is refused: `FLX-8001`, exit 1, nothing written to the output directory. A pack that exists means the run's artifacts match their digest and every declared fidelity check passed. `ship` does not re-check the golden baseline, and it does not re-check that the bundle's documents are still the ones the run was made from. Those are `verify`'s legs, so run `verify` first, or let `fluxsim run . --verify --ship` run the steps in that order.

```console
$ fluxsim ship . --run-id 8dec55892096169f -o out2   # exit 1
FLX-8001: nothing packed -- FLX-2110: run artifacts do not reproduce the recorded run digest -- ...
...
```

## Exit codes and JSON

| Code | Meaning |
|---|---|
| `0` | The command ran and succeeded. |
| `1` | The command ran and failed. A catalogued failure carries an `FLX-nnnn` code. |
| `2` | A usage error. Nothing ran. |
| `3` | `fluxsim gate decide` only: the request was decided, and the policy said no. |

With `--json`, every command prints one envelope: `stage`, `exitCode`, `ok`, `artifacts`, `error`.

```console
$ fluxsim --json validate .
{
...
  "ok": true,
...
}
```

See [the command reference](../reference/cli/README.md) for each flag, [the seam](./the-seam.md) for what the attestation file holds, and [the evidence pack](./the-evidence-pack.md) for what `ship` writes.
