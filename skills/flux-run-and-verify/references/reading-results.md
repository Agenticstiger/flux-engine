# Reading results

What `fluxsim verify` and `fluxsim run --verify` print, and how to report it. Pages this one draws on:
[the six-step loop](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/explanation/the-six-step-loop.md),
[the evidence pack](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/explanation/the-evidence-pack.md) and
[the command reference](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/reference/cli/README.md).

## The run summary

- `plan` prints `plan written to ...` with the run id and the plan digest, then says how many rows it staged.
- `apply` prints `applied to ...` with the run digest, then one line per stream: its name, its row count and
  how the rows were made.
- `run` prints both, then each optional step you asked for (`--verify`), and ends with `run RUN_ID complete`.
  It stops at the first step that exits non-zero, and the output of the steps that did run is still there.

## One entity's history

`fluxsim story --run-id RUN --emit EXPOSE --key VALUE` tells the history of one row of a committed run: the rows that link to it and the rows it links to, to `--depth` hops, ordered by date, one line per row. It checks each stream against the receipt's `outputDigest`, reads the committed streams and writes nothing. For a composite key pass `--key COLUMN=VALUE` once per column. It is a way to explain a run to the user, so quote its lines as printed. Its rows are synthetic: say so.

## The two verdicts

The first `verify` line reads `reproducible: ... | validity: ...`.

- `reproducible` prints as `yes`, `no` or `not checked` (for example `reproducible: yes | validity: not
  asserted`); the `--json` values are `reproducible`, `not-reproducible` and `not-checked`. It compares the run's digest with a
  recorded baseline. With no baseline it says `not checked`. It looks at the golden leg only, so it can read
  `reproducible` over a run that another leg failed. The exit code covers every leg.
- `validity` reads `not asserted`. Nothing in the engine measures the simulation against the world. Report it as
  it is, and never turn it into "valid".

## The five legs

Each prints one line.

| Leg | What it checks | When the line is not a pass |
|---|---|---|
| artifacts | every component of the run digest is recomputed from the files on disk | names the components that do not reproduce: a stream was edited after `apply`, or files moved |
| golden | the run digest against the recorded baseline | `not checked` with no baseline; a mismatch is a refusal with its own code |
| rows | each stream's row count against the receipt, and each row's width against its seam attestation | names the stream and the difference |
| rfc-04 vectors | the specification's published cases for the enforcement gate, replayed through the gate | counts failures; it tests the gate, not the run's rows |
| fidelity | each emit that declares a fidelity check, against a slice or a profile | `not declared`, or `declared, not checked`, or `passed` or a failure with the property named |

"Not checked", "not declared" and "declared, not checked" mean no evidence either way. Report them as gaps,
and give the user the flag that would run the leg (the line names it).

Before the legs, `verify` confirms the run still belongs to the bundle. A refusal there is one of the
binding codes (the 2000 family in the refusals reference), for example a bundle changed since the plan
(`FLX-2101`) or a run directory that does not match its plan (`FLX-2113`).

## Fidelity and the slice

A fidelity block in the Simulation says which statistics of an emit are compared and how close they must be.
`--committed-slice EXPOSE_ID=file://PATH` names, once per expose, a file of real-data rows that the user
holds; it is the reference for that expose. Pass the path only. Never open, sample or print that file.
Without a slice or a profile the leg reads `declared, not checked`.

## The scorecard

A run whose plan pins a profile is scored against it. Pass the profile file the user names:
`fluxsim verify bundle --run-id RUN_ID --profile orders.profile.json`, or `--profile` on `fluxsim run`. The
file stays outside the bundle folder (`FLX-3108`) and its digest must be the pinned one (`FLX-3110`).

- The scorecard gives scores from 0 to 1, where 1 is the same value, per property, per table, per
  dimension and overall. It also says how much of the contract the scores cover (columns and relationships),
  and which dimensions were not measured. A high overall score over a small coverage says little. Report
  both numbers.
- A declared threshold that is missed, or a score below an emit's minimum credibility, fails the stage.
- Shape flags are listed per profiled column. They fail nothing unless the plan declares a limit on them.
- A property that reads a statistic the profile does not hold is refused (`FLX-5037`), not scored. A
  statistic the profile's output checks withheld is left out of the score and listed as censored.
- With a plan that pins a profile and no `--profile`, the scorecard reads `not checked` and the line says to
  pass it. The fidelity leg prints `declared, not checked` when a check is declared and no slice or profile is
  passed.

Format and rules: [the scorecard specification](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/spec/SCORECARD-SPEC.md).
Worked steps: [score a run against a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/how-to/score-a-run-against-a-profile.md).

## Sampling

`--sample FRACTION` compares each emit's means, histograms and quantile bins with the pinned profile on a
sample of blocks of records, each estimate decided by its interval. `--sample-block N` and `--sample-seed N`
set the block size and the seed, and both need `--sample`. A sampled verdict is a statement about the
sample; say so.

## EVIDENCE.md

`ship` writes `EVIDENCE.md` into the run folder and into the pack. The `flux-scale-and-ship` skill runs
`ship`. To read the file, know its sections:

- What This Bundle Is: the bundle, its contract and its Simulation.
- Conformance: whether the run's rows match the contract.
- Reproducibility: the golden leg's result as `ship` saw it. It reads `not-checked` there, because `ship` does
  not re-check the baseline; `verify` does.
- Fidelity: each declared check, its reference, and the scorecard if there is one.
- Privacy Grade: the grade read from the pinned profile, and each column's minimum.
- Column Provenance: how each column was made (`declared`, `copied`, `derived`, `default` or `profile`).
  A `default` column holds a value nobody described.
- The last section lists what the report does not show: that validity is not asserted, which numbers are the
  author's own dials, and which columns fell back to a default. Read it aloud to the user; never summarise it
  as "passed".

The report says who signed a fidelity record. With no key of the user's, the record is signed with a key
generated for that single run and attributes the run to nobody. The user supplies a signing key as a file;
you never read, print, copy or create it.
