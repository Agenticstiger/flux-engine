# ADR 0006: `inputDigest` covers a closed set of files, and the loader refuses anything else

## Status

Accepted (2026-09-07)

## Context

A bundle directory had two `inputDigest` values, and with them two
`runDigest` values, for one authored bundle.

How it was found: on 2026-09-07 a developer ran the FLUX specification's
own bundle packer (`flux_spec.bundle.pack`, vendored at
`flux_engine/_vendor/flux_spec/bundle.py`) against the public example in
place, and then compared the resulting run with CI's. Every behavioural
component digest was identical: population, events, journeys, delivery,
assignment, the emit digest, the seam attestation and the 8415-row count
all matched. Only `inputDigest` had moved, and it took a day of diffing
component digests to find out why.

The cause was the file selection. `flux_engine/seam/resolve.py` loaded a
bundle as every regular file directly inside the directory, and
`flux_engine/stages/pipeline.py` fed all of them except the FLUID
documents and `flux.lock` into `build_input_digest`. That is a deny-list.
The packer writes `flux-manifest.json` into the directory it packs, the
spec repository gitignores it (`examples/**/flux-manifest.json`), so the
file was present on one machine and absent on the other. The manifest has
no timestamp, so the move happens once rather than on every pack, but the
same bundle now digested to two values depending on whether a derived
file happened to be on disk.

Two things sharpened the problem. First, the manifest is computed from
the digests of the very files being hashed, so folding it into
`inputDigest` makes a bundle's digest depend on a description of itself.
The FLUX bundle format already excludes the manifest from its own `files`
map and Merkle root for exactly that reason. Second, the same three-way
filtering existed twice, byte for byte, in `stages/pipeline.py` and
`stages/verify.py`, with no shared helper, so a fix applied to one of
them would have reported a moved input digest on every run.

`fluxsim ship` was not the source of the incident. `flux_engine/evidence/pack.py`
stages a copy into a temporary directory before calling the packer, and
`tests/evidence/test_pack.py` pins that the source tree is byte-unchanged.
What is not guarded is the pack's *output* directory: `fluxsim ship <bundle>
-o <bundle>` was legal, and it dropped four evidence files into the
directory being attested.

### Borrow before build

The receipts, all of which point the same way:

- **Bazel** hashes explicitly declared inputs, the rule's `srcs`, never a
  directory listing. An undeclared file is neither silently admitted nor
  silently lost, because the declaration is visible in the build graph.
- **Nix's `lib.sources` family** (`cleanSource`, `cleanSourceWith`,
  `nix-gitignore`) exists because copying an unfiltered directory makes
  every file in it a build input, so changing any file changes the
  derivation and cached results stop being reusable. That is this defect,
  described by someone else years earlier.
- **Nix's own evolution ran deny-list to allow-list.** Tweag's
  `lib.fileset` (2023-11-28) replaced the filter-based family because
  filters silently lose or admit files as a tree evolves. The engine's
  rule was a two-clause filter written before anyone ran a packer in a
  bundle directory. It admitted the manifest without anyone deciding it
  should.
- **in-toto and SLSA** keep materials (inputs) and subject or products
  (outputs) as separate fields by design. A derived artifact is never
  counted as a material.

## The three options as argued

**Option A, subtract the derived artifacts by name.** Keep hashing every
file in the directory, and exclude `flux-manifest.json` and
`flux-manifest.json.sig`, with the names read from the bundler this
engine links against rather than copied. Argument for: the smallest
possible change, it adopts the upstream file selection rather than
inventing one, and a file it does not name fails loudly rather than
quietly. Argument against: it closes two names, not the class. Its own
author named `.DS_Store` as the likeliest recurrence, and a measurement
during judging found a second one: after `fluxsim ship <bundle> -o <bundle>`,
none of the four files written is one of the two names, so Option A gave
the identical moved digest the defect produces.

**Option B, a closed allow-list.** Hash exactly the top-level
`*.flux.yml`, `*.flux.yaml`, `*.fluid.yml`, `*.fluid.yaml` and
`flux.lock`, and hash nothing else, reporting what was found and skipped.
Argument for: it closes the class in one move, and it is a repair rather
than a redefinition, because on a bundle holding only specification files
it computes exactly what the deny-list computed. Argument against: a file
the loader reads but the digest does not cover is a silent hole, and the
report that answers it is a log line rather than a byte in a hash.

**Option C, refuse the unrecognised file.** Keep the closed set, and make
the loader refuse a directory holding a file that is neither, under a new
`FLX-3108`. Argument for: every file is then either hashed or named in a
refusal, so nothing is silently included and nothing is silently dropped,
and the rule cannot be defeated by a schema change because the loader
stops rather than guessing. Argument against: a stop-the-world refusal
over a `.DS_Store` is a chore, and an older engine meeting a newer
document kind refuses a legal bundle.

## Decision

Option C, with the grafts both judges required.

A bundle directory holds two classes of file and no third.

- **Inputs** are the documents the loader matched, plus `flux.lock`.
  `*.flux.yml` and `*.flux.yaml` fill `documents`, `*.fluid.yml` and
  `*.fluid.yaml` fill `fluidDocuments`, `flux.lock` fills `lock`. The
  hashed set is taken from the loader's own glob result, not from a
  second match over the directory, so the files a bundle was validated
  against and the files its digest covers are one selection.
- **Products** are `flux-manifest.json` and its detached signature
  `flux-manifest.json.sig`. They are recognised, they are not hashed, and
  they are not refused. The two names are built from
  `flux_engine.spec.bundle.MANIFEST_NAME`, so an upstream rename carries
  here without an edit. They are two exact names and never a `*.sig`
  suffix class.
- **Everything else** ends the command with `FLX-3108`, naming every
  offending file in one refusal and naming the set this engine reads,
  before a digest is taken. This selection reads regular files in the
  bundle directory and does not descend, so a `.flux/` run state directory
  and an extracted archive's `run/` directory stay outside `inputDigest`
  by construction. That bounds this digest and nothing else: the bundle
  format's own file map and Merkle root walk the directory recursively,
  so a pack run over a bundle carrying a state directory ships those
  files as archive members.

There is no `--admit-file` escape hatch. An escape hatch that is not
recorded lets two people compute two digests for one directory under one
engine, which is the incident wearing a flag.

One function, `flux_engine.stages.pipeline.input_digest_of`, makes the
selection, and both `execute` and `verify.recompute_input_digest` call it.

Separately, and in the same branch because the winning rule turns it into
a refusal loop: `pack_evidence` refuses an `out_dir` that is the bundle
directory itself.

### Why C over B, and why not A

Recognising the manifest as a product rather than refusing it is a
condition of this verdict, not a detail. Under a rule that refuses it,
an evidence archive cannot be re-verified from the archive: the shipped
`.flux.tgz` carries `flux-manifest.json` as a tar member, so extracting
it and pointing `fluxsim verify` at the extracted directory would refuse.
Under this decision the extracted archive recomputes to the digest it was
packed from.

Option A was ranked last by both judges. It answers "can two people with
the same declared bundle compute different digests" with yes: it fixes
one filename, and the `fluxsim ship -o .` measurement shows a supported
command that defeats it. Its failure is also blind rather than specific,
because a file outside its two names surfaces as `FLX-2101`, whose
registered explanation says a document changed and names no file, which
is the misdirection that cost a day.

Option B's causal argument is that no property in the FLUX kernel schema
names a file, so a file outside the closed set has no route by which it
could reach a run. That was checked and it very nearly holds: 147
properties, every reference-shaped one a `*Ref` resolving to a document
id. Two are URI-typed, `Playback.sourceStream` and `Signal.dataschema`,
and both patterns admit `file://`. No engine code dereferences either one
today, so B is safe
today, but its safety rests on a wiring gap rather than on structure, and
its residual failure is the silent kind. C reaches B's stability without
that dependency, because a file it cannot classify stops the command
instead of being guessed at.

## Consequences

- A bundle's `inputDigest` no longer moves because a pack ran in its
  directory, and a shipped archive extracts to the digest it was packed
  from.
- `tests/golden/telco/` moves, in three lines and no more, because
  `tests/fixtures/telco-fidelity/flux-manifest.json` is a tracked file.
  `inputDigest` goes from `sha256:0b0f79c0...` to `sha256:c056d3d2...` in
  `plan.json` and in `run-digest.json`, and `component-digests.json`
  moves in its `lineage` entry alone. The judging panel predicted
  `component-digests.json` would not move and that
  `seam-attestation.json` would; the regeneration says the opposite, and
  the reason is checkable. The OpenLineage ledger records `inputDigest`
  in its own events (`flux_engine/lineage/emitter.py`), so the `lineage`
  component is a function of it: a real run's `lineage.ndjson` holds the
  receipt's value twice, read back to confirm it. The pinned
  `seam-attestation.json` is a single per-emit attestation document,
  which carries no `inputDigest` field at all. Nothing behavioural moves:
  `run-digest.json`'s own `components` map (population, events, journeys,
  delivery, assignment) is byte-identical, and so are the emit,
  population, events, decisions and suppressions entries of
  `component-digests.json`, and so is `fidelity-record.json`.
  `tests/golden/telco-public/` does not move at all, and that is the
  control: that bundle carries no manifest, and its pinned
  `sha256:c94ca9fe...` reproduces before and after.
- Both committed fixture manifests are kept rather than deleted. They
  stop being accidental scaffolding and become the in-repo witness that
  the product exclusion holds, exercised on every golden run.
- One-time upgrade break, for exactly the operators the defect already
  hit: a plan, receipt or golden recorded against a directory that held a
  non-document file reports `InputDigestMoved` (`FLX-2101`) once, against
  the pre-fix value. Nothing has been distributed, so the affected
  population is bounded.
- `fluxsim validate`, `plan`, `apply`, `run`, `verify`, `calibrate` and the
  two `expr` subcommands all become stricter at once, because all of them
  reach `load_bundle`. A bundle directory carrying a README, a data file
  or an editor dropping stops working until the file is moved out of it.
  This is the cost the decision accepts, and it is loud and specific
  rather than quiet.

## What this decision does not do

- It does not stop the packer writing into a directory it is handed. The
  vendored `flux_spec.bundle.pack` still writes `flux-manifest.json` into
  its argument. This change makes that write digest-neutral; it does not
  prevent it.
- It does not change the shipped archive. `flux-manifest.json` is still a
  tar member of the `.flux.tgz`, covered by the archive's own Merkle root
  and in-toto statement. `inputDigest` is not the digest that covers it,
  and never was.
- It does not make `documents` a semantic set. A `*.flux.yml` file that
  yields no usable document is still hashed by name while contributing
  nothing to a run, so the hashed set stays a superset of the
  load-bearing one.
- It does not close the schema question. If a FLUX document ever
  dereferences a field to a bundle-relative file, `Playback.sourceStream`
  and `Signal.dataschema` being the two near cases today, that file
  becomes an input and the recognised set has to grow with it. `FLX-3108` turns that into a
  refusal naming the file rather than into a digest that quietly covers
  less than it claims, which is why the refusal is the decision and the
  allow-list alone is not.
- It does not add a way to hash a file on purpose. There is no
  `--admit-file`, by design.
- It does not touch `planDigest`, the seed tree, or any component digest.
  `inputDigest` is a binding and gate value; it is not mixed into the
  seed-tree root, so a wrong `inputDigest` never silently changed what a
  run computed. It failed to detect that something else had.
