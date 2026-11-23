---
title: "The evidence pack, and what it does not claim"
description: "What fluxsim ship writes, what a recipient can check offline, and the plain list of things the pack does not say."
---

# The evidence pack, and what it does not claim

`fluxsim ship` ends in one archive. It is meant to be handed to someone who was not
there: a reviewer, a colleague on another team. This page says what is in it, what that
person can check for themselves, and what the pack leaves unsaid on purpose. The last
part matters most.

```console
$ fluxsim init demo
...
$ cd demo
$ fluxsim run . --verify --ship -o out
...
shipped out/demo.flux.tgz
run 205ba6e504c40820 complete; shipped to out
```

## What is in it

`out/` holds four files. The archive is the pack; the other three are the statements
about it, kept beside it so they can travel separately.

```console
$ ls out
demo.attestation.json
demo.flux-manifest.json
demo.flux.tgz
demo.runtime-evidence.json
```

Inside the archive are the bundle's documents, the whole run (the rows, the events, the
plan, the receipt, the seam attestation, the lineage), a manifest, and the report:

```console
$ tar tzf out/demo.flux.tgz | grep -E 'EVIDENCE|manifest|seam-attestation'
demo/.flux/runs/205ba6e504c40820/EVIDENCE.md
demo/.flux/runs/205ba6e504c40820/seam-attestation-payment_recovery_moment.json
demo/flux-manifest.json
demo/run/EVIDENCE.md
demo/run/seam-attestation-payment_recovery_moment.json
```

The run is in the archive twice. `run/` is the copy `ship` makes on purpose. `.flux/runs/`
is there because the example ran inside the bundle directory, so the state directory was
packed with the bundle; pass `--state-dir` outside the bundle if you want it left out.

A run whose Simulation pins a profile, shipped with `--profile`, also carries
`run/scorecard.json`: the run's scores against that profile, per property, per table, per
dimension and overall, how much of the contract those scores cover, any declared
thresholds, and the shape flags of every profiled column ([SCORECARD-SPEC.md](../spec/SCORECARD-SPEC.md)). The pack holds the bytes `ship`
computed: the scores the gate decided on, and beside them the shape flags, which gate nothing.

A run whose Simulation declares a parameter register also carries
`run/parameter-register.json`: every number the register counts, each with what it rests on, a
statistic of the pinned profile, a place in the contract, an assumption and its reason, or
a default ([PARAMETER-REGISTER-SPEC.md](../spec/PARAMETER-REGISTER-SPEC.md)). The plan holds its digest,
`parameterRegisterDigest`, so `ship` and `verify` read it the way they read the column record
below, and the report states the shares under "What Was Not Proven".

The manifest lists every file with its digest and rolls them into one Merkle root. Two
in-toto statements describe the pack. The *build* statement says what the offline
validator found; the *runtime evidence* statement carries the run digest and the version
of the engine that produced it. They answer different questions and neither restates the
other.

## What a recipient can check

The specification's own bundle tool re-computes every digest and the Merkle root and
compares them with the manifest packed inside the archive. It comes from the
specification package, `flux_spec`, which the engine depends on. In an environment where
the engine was installed with `pip`, the command below works as it stands. After
`uv tool install` the package sits in the tool's own environment, so a machine that only
has the archive needs `flux-spec` installed first.

```console
$ python -m flux_spec.bundle verify out/demo.flux.tgz
[ok] out/demo.flux.tgz ... merkle root verified, attestation reproduced (profile=core)
...
```

Change one byte of one file and it says which:

```console
$ mkdir ../tamper
$ tar xzf out/demo.flux.tgz -C ../tamper
$ echo '# edited' >> ../tamper/demo/world.flux.yml
$ COPYFILE_DISABLE=1 tar czf ../tampered.tgz -C ../tamper demo
$ python -m flux_spec.bundle verify ../tampered.tgz   # exit 1
[FAIL] ../tampered.tgz
       digest mismatch: world.flux.yml
```

(`COPYFILE_DISABLE=1` stops macOS `tar` from adding `._` files, which the tool would count
as a second directory.)

That shows the files in the archive agree with the manifest packed with them. It does not
show that this is the archive you packed: anyone who edits a file can rebuild the
manifest and pack again, and the result verifies. To tie an archive to its sender, send
the Merkle root or the archive's SHA-256 by another route, or sign the manifest. The next
two sections say what else is missing.

## Who made it

A pack records *what* was run. It does not, by default, record *who* ran it. The record
that backs a fidelity check is signed, and without a key of yours the engine signs with
one it generates for that single run. The report says so in its own words:

```console
$ grep 'Signature:' .flux/runs/205ba6e504c40820/EVIDENCE.md
- Signature: this run's fidelity records were signed with a key generated in memory for this invocation and checked against that same key. It attributes the records to no operator; pass `--signing-key` to sign with one that does, and `--trust-key` with the public half the gate should check against.
```

To attach the pack to a person or a team, sign with a key that people know: pass
`--signing-key`, and give the recipient the public half to pass as `--trust-key`. The
manifest and the two in-toto statements are not signed by the engine either; `ship` prints
how to sign them separately (`ssh-keygen` or `cosign`). [Ship with evidence](../how-to/ship-with-evidence.md)
walks through both.

## Which privacy pattern the run rests on

The report's **Privacy Grade** section gives the run one grade, read from the profile the
Simulation pins and from the generator plugins it declares, and from nothing else the bundle says
about itself ([ADR 0020](../adr/0020-privacy-grades.md), [ADR 0032](../adr/0032-a-generator-plugin-is-declared-graded-and-stored.md)). The bundle's register labels a number "profile" or
"assumption", and a bundle can write either, so a grade read from them would be a grade the
bundle chooses. The pinned profile is bound by its digest. There are five patterns:

- **P0, assumed.** No profile was pinned and every parameter is declared by hand. The engine read
  no data, and it cannot confirm that no data shaped the numbers a person typed.
- **P1, checked.** A profile of aggregates under the profiler's output checks (small cells
  suppressed, counts rounded). The checks carry no formal guarantee.
- **P3, differentially private marginals.** The profile states one release under differential
  privacy, declares that its levels, bin edges and sample-size bands were chosen without the data,
  and holds only noisy statistics: no row count, no column metric, nothing without noise. The
  report states the guarantee as the profile's own statement, in the terms NIST SP 800-226 asks for
  (the unit of privacy, the privacy loss, and the accounting). The engine checks that the stated
  shares add up and that the cliques agree, not that noise was applied.
- **P4, differentially private model.** A learned model from a generator plugin, whose bundle
  declares the account of its training: the unit of privacy, the privacy loss and the accounting.
  The report states the account as the bundle declares it. The engine checks the account's form,
  not how the model was trained.
- **P5, learned model, no privacy account.** A learned model from a generator plugin whose privacy
  rests on empirical checks alone. It is the weakest pattern, below every built-in minimum.
  P2 is not assigned, and the numbers are stable.

The run's grade is the weakest of its profile's and every declared plugin's, in the order P5, P1, P3,
P4, P0. A run that pins no profile and declares one P4 plugin is P4.

A profile that is not P3 is P1, and the section says why. A grade for each column, following the
statistics that inform it, is a later refinement. A column's sensitivity label sets a minimum grade
(`pii`, `phi`, `restricted`, `pseudonymized` and `tokenized` need P3 or stronger, `confidential` and
`internal` need P1 or stronger), the section lists every column with its minimum and whether the run
meets it, and a run below a minimum is refused by `plan`, `apply` and `ship`
([FLX-4005](../reference/errors/FLX-4005.md)).

## How each column was made

The report's **Column Provenance** section says, for every column of every exposed table,
how it was produced and from what. A column is one of five things:

- `declared`: the bundle's `fidelity.columns` entry for it decides the draw.
- `copied`: an attribute of the simulated world passed through as it is, such as
  `subject.id`. The report says whether the emit's `mapping` block, the vertical's alias
  table or the column's own name picked the attribute.
- `derived`: computed, never drawn. An identifier column is a function of the seam digest
  and the row index.
- `default`: nothing the engine could produce the column from, so the type's own default
  fills it. For an optional column nothing maps onto that is `null`; for a date or
  timestamp column that declares no draw of its own it is uniform within the simulation's
  time bounds.
- `profile`: a column of a reference expose, whose value the pinned profile publishes for
  its row in a table released whole (`references`, ADR 0023), decided by `reference`. A
  bundle that `fluxsim init --profile` wrote declares its other columns from the profile,
  so they read `declared`, and its parameter register says which numbers rest on the
  profile.

```console
$ sed -n '/^## Column Provenance/,/^## What Was Not Proven/p' .flux/runs/205ba6e504c40820/EVIDENCE.md
## Column Provenance

This is what `apply` recorded for the run. The run's plan holds a digest of it, and `ship` checked the record against that digest; it does not work out again how each column was produced.

### `payment_recovery_moment`

4 columns: 4 copied.

- `subscriber_id` (STRING): copied from `subject.id` (named by the vertical's alias table)
- `aborted_at` (TIMESTAMP): copied from `event.time` (named by the vertical's alias table)
- `amount` (DECIMAL(18,4)): copied from `event.data.amount` (named by the vertical's alias table)
- `market` (STRING): copied from `subject.market` (named by the vertical's alias table)

## What Was Not Proven
```

Only `default` is a gap, and it is the one the report names again under "What Was Not
Proven". `fluxsim plan` also names those columns, and `--strict-coverage`, or `strictCoverage: true` in
the bundle, refuses the run instead ([FLX-3304](../reference/errors/FLX-3304.md)). The same record is in the archive as
`run/column-provenance.json`, shaped as an OpenLineage column-lineage facet for the columns
that were copied, so a tool that reads lineage can read it. A column that a rule copies from a linked
row lists no input field, because the record does not name the parent expose.

The plan holds a digest of the record, `columnProvenanceDigest`, the way it holds the digest of
`seed-paths.ndjson`, so the plan digest, the run id and the run digest cover the record. `ship` and
`verify` read the file back only through that digest. A record that is missing, is not a regular file,
or no longer hashes to it, ends the ship or the verify with
[FLX-2115](../reference/errors/FLX-2115.md), and a ship packs nothing. `apply` makes the same
comparison on the record it is about to write and refuses before it writes a row when the plan names a
different one, which means the plan came from an engine that classified a column differently: plan
again. Both `ship` and `verify` also refuse a run digest
that names a different plan from the one beside it ([FLX-2113](../reference/errors/FLX-2113.md)),
which is the link between the plan's digest and the digest the pack's statement names. The archive
holds the bytes `ship` checked and rendered the report from, not whatever the file holds by the time
the directory is copied. What this shows is that the file is the one the plan's own execution
produced. It does not show that the engine classified each column rightly: `ship` does not work out
again how each column was produced, and the section says so. The receipt, which marks the run as
complete, is not covered by a digest: it is read as that marker and for the row counts `verify`
checks, and the record's binding does not extend to it. A run whose plan was written before the plan
carried the digest has nothing to check a file against, so its section says that no column
provenance is bound to it.

## What the pack does not claim

Every report ends with a section called **What Was Not Proven**. It is generated from a
fixed template, never edited by hand, and always present, so a reader who has seen one
pack knows where to look in the next.

```console
$ sed -n '/^## What Was Not Proven/,$p' .flux/runs/205ba6e504c40820/EVIDENCE.md
## What Was Not Proven

This bundle asserts no privacy or re-identification claim about any source data, no fairness claim about any source system, and no claim about any system trained or tested using its output.

No validity claim is made (validity: not asserted): this run's predicted outcomes have not been measured against the world. In particular, the following are authored dials -- values this bundle's author declared, not values this engine fitted or measured: `temperature.campaignResponse`.

No column fell back to a type default: every column was produced by a declaration, a copy or a derivation.
```

In plain words:

- **It does not say the simulated customers behave like real ones.** The engine runs the
  scenario you described, exactly and repeatably. Whether the description matches the
  world is a different question. In this release the report always answers it `not
  asserted`: nothing here sets validity to anything else.
- **The dials are yours.** Every number in the bundle (a persona trait, a journey
  probability, a temperature) was declared by its author. The engine did not fit or
  measure them. The report names one of them, `temperature.campaignResponse`, from a list
  that is fixed in this release. A Simulation that declares a parameter register gets every
  number listed with what it rests on, and the report states how many come from the pinned
  profile, how many are assumptions (each named, with its reason) and how many are defaults.
  A register says where a number came from, as a declared scorecard says where a dial came
  from; neither makes the number right.
- **It says nothing about privacy, fairness or any system trained on the rows.** The
  rows are synthetic, generated from the dials and distributions the bundle declares,
  but the pack makes no claim that they cannot be linked to anyone, or that they are
  fair, or that a model trained on them will work.
- **Reproducible is a separate line from valid.** `reproducibility` compares a run's
  digest with a baseline you recorded; it does not run anything again. In the pack it
  always reads `not-checked`, because `ship` does not re-check the baseline.
  `fluxsim verify` does, once you have recorded one with `--update-golden --reason
  "..."`, and `fluxsim replay` is the check that runs the plan again. Neither answer says
  anything about the world.
- **A fidelity check is about statistics.** When a bundle declares one, `ship` compares
  properties of the synthetic rows with a committed slice you pass in and refuses to pack
  if they disagree beyond the stated margins. That supports a statistical relation
  between the two under the properties you registered. It is not a statement about
  privacy or fairness either. A passing check does not change `validity: not asserted`,
  and the engine cannot tell whether the slice you pass is real data.
- **It names the columns nobody declared.** The last line says which columns fell back to
  a type default, or that none did, or that the run recorded nothing to tell. A column that
  fell back has a value the bundle never described, so nothing here shows it resembles
  anything.

## How a claim could grow

Measuring the scenario against what actually happened is a separate step, and in this
release it does not feed back into the pack. The `fluxsim accuracy` commands seal a
prediction before its horizon, score it against an observed readout afterwards, and write
the outcomes, misses included, to an accuracy record of their own (`fluxsim accuracy
record --out accuracy.json`). A sealed prediction is written once and checked against its
seal every time it is read, so a prediction changed after its outcome is known is refused
rather than scored or published (`FLX-7007`, `FLX-7008`). The pack's own report stays what
it is: a precise account of what was run, and a plain list of what was not shown.

See also [the six-step loop](./the-six-step-loop.md) for how `ship` fits after `verify`,
and [the command reference](../reference/cli/ship.md) for every flag.
