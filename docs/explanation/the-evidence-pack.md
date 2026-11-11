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
run 8dec55892096169f complete; shipped to out
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
demo/.flux/runs/8dec55892096169f/EVIDENCE.md
demo/.flux/runs/8dec55892096169f/seam-attestation-payment_recovery_moment.json
demo/flux-manifest.json
demo/run/EVIDENCE.md
demo/run/seam-attestation-payment_recovery_moment.json
```

The run is in the archive twice. `run/` is the copy `ship` makes on purpose. `.flux/runs/`
is there because the example ran inside the bundle directory, so the state directory was
packed with the bundle; pass `--state-dir` outside the bundle if you want it left out.

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
$ grep 'Signature:' .flux/runs/8dec55892096169f/EVIDENCE.md
- Signature: this run's fidelity records were signed with a key generated in memory for this invocation and checked against that same key. It attributes the records to no operator; pass `--signing-key` to sign with one that does, and `--trust-key` with the public half the gate should check against.
```

To attach the pack to a person or a team, sign with a key that people know: pass
`--signing-key`, and give the recipient the public half to pass as `--trust-key`. The
manifest and the two in-toto statements are not signed by the engine either; `ship` prints
how to sign them separately (`ssh-keygen` or `cosign`). [Ship with evidence](../how-to/ship-with-evidence.md)
walks through both.

## What the pack does not claim

Every report ends with a section called **What Was Not Proven**. It is generated from a
fixed template, never edited by hand, and always present, so a reader who has seen one
pack knows where to look in the next.

```console
$ sed -n '/^## What Was Not Proven/,$p' .flux/runs/8dec55892096169f/EVIDENCE.md
## What Was Not Proven

This bundle asserts no privacy or re-identification claim about any source data, no fairness claim about any source system, and no claim about any system trained or tested using its output.

No validity claim is made (validity: not asserted): this run's predicted outcomes have not been measured against the world. In particular, the following are authored dials -- values this bundle's author declared, not values this engine fitted or measured: `temperature.campaignResponse`.
```

In plain words:

- **It does not say the simulated customers behave like real ones.** The engine runs the
  scenario you described, exactly and repeatably. Whether the description matches the
  world is a different question. In this release the report always answers it `not
  asserted`: nothing here sets validity to anything else.
- **The dials are yours.** Every number in the bundle (a persona trait, a journey
  probability, a temperature) was declared by its author. The engine did not fit or
  measure them. The report names one of them, `temperature.campaignResponse`, from a list
  that is fixed in this release; it does not read your bundle to list the rest. A
  declared scorecard says where a dial came from; it does not make the dial right.
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

## How a claim could grow

Measuring the scenario against what actually happened is a separate step, and in this
release it does not feed back into the pack. The `fluxsim accuracy` commands seal a
prediction before its horizon, score it against an observed readout afterwards, and write
the outcomes, misses included, to an accuracy record of their own (`fluxsim accuracy
record --out accuracy.json`). The pack's own report stays what it is: a precise account of
what was run, and a plain list of what was not shown.

See also [the six-step loop](./the-six-step-loop.md) for how `ship` fits after `verify`,
and [the command reference](../reference/cli/ship.md) for every flag.
