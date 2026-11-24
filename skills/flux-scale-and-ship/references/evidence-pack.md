# The evidence pack

Offline first: `fluxsim ship --help`, then `fluxsim explain FLX-8001`. The pages: [Ship with evidence](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/how-to/ship-with-evidence.md) and [The evidence pack](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/explanation/the-evidence-pack.md).

## Building it

```bash
fluxsim ship bundle --run-id RUN_ID -o OUT
fluxsim run bundle --verify --ship -o OUT
```

`ship` needs a run applied whole (never only shares). It runs the fidelity gate over every emit that declares a fidelity block and packs only if none is withheld. `OUT` is a folder the user names; keep it outside the bundle when you can, or ship leaves that folder out of the archive.

## What is in it

- `OUT` holds the archive (`NAME.flux.tgz`), a manifest, a build statement and a runtime statement, both in-toto statements about the archive's digest.
- The archive holds the bundle's documents, the whole run (rows, events, plan, receipt, seam attestation, lineage), the manifest with a Merkle root of every file, `run/EVIDENCE.md` and the fidelity records.
- A run that pins a profile, shipped with `--profile PATH`, also carries `run/scorecard.json`. A run that declares a parameter register carries `run/parameter-register.json`. A run delivered to a sink carries `sink-receipts.json`.
- `EVIDENCE.md` is the report. Reading it in depth is the `flux-run-and-verify` skill.

## Slices

A fidelity block needs a committed slice per expose, `--committed-slice EXPOSE_ID=file://PATH` (repeatable), or `--profile` when the plan pins a profile: the profile then gates and scores the run in place of a slice. The file is a slice the user holds and names; one JSON object per line keyed by contract column names. Never open or summarise it. Without one, the gate withholds (FLX-8001).

## Signing and trust keys

- Without `--signing-key`, ship signs the fidelity record with a per-invocation key attributable to nobody, and the report says it attributes the records to no operator.
- `--signing-key SIGNING_KEY` takes a file the user names, holding a 32-byte ed25519 seed. Never read, print, copy or create key material, never generate a key for the user, and never pass a key as text. If the user has no key file, say so and offer the unsigned form.
- With a signing key alone, the report calls the signature self-attested: checked against the public half of the same key.
- `--trust-key PATH` takes a file holding a 32-byte ed25519 public key, the `publicKey` a fidelity record prints. The gate then checks the signature against that key, and the report says it was checked against a trust anchor supplied separately. A record signed by a different key is withheld.
- The manifest and the two statements are not signed by the engine; ship prints how to sign them separately.

## When ship withholds

Exit code 1 and `FLX-8001`: the gate writes nothing to `OUT`. The reasons name each failed check, for example a missing committed slice, a property outside its margin, a record whose signature does not match the trust key, a profile that is not the pinned one (FLX-3109, FLX-3110) or a state that is too old. Run `fluxsim explain FLX-8001`, then:

- report the reasons to the user in their own words;
- change nothing to make the gate pass: not the margin, not the slice, not the declared numbers. Those belong to the user and the contract owner;
- if the remedy is to apply the run again and ship the new run, do that.

`fluxsim --json ship bundle --run-id RUN_ID -o OUT` gives one JSON document: `ok` and `exitCode` repeat the gate, and `artifacts.withheld` lists the withheld exposes.

## What the pack leaves unsaid

The pack records what was run, not who ran it (unless a signing key was used), and a passed check says the rows sit inside the declared margins against the slice it was given and nothing about rows the slice does not hold. State that when handing a pack over; do not state more.
