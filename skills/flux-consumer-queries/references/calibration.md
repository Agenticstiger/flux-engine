# Calibration and the held-out check

Calibration fits numbers the data does not state to the rates of the calibration queries, and writes the result as a calibration patch. Held-out queries, which no fit sees, decide whether the result may be called calibrated. The worked page is
[check consumer queries](https://github.com/Agenticstiger/flux-engine/blob/v0.12.1/docs/how-to/check-consumer-queries.md).

Two things share the name. `fluxsim calibrate` with no `--fit` reads a Playback's declared scorecard and fits nothing. `fluxsim calibrate --fit BUNDLE` is the fit below. Do not mix their options up.

## What the fit may move

The bundle's `calibration` block lists the parameters. Each names a document of the bundle and a JSON pointer to a number the parameter register lists with the source `assumption` or `default`, with `min`, `max` and `step`. A value the pinned profile or the contract states is refused, and so is one an earlier fit moved (`FLX-3705`). A document outside the bundle's closed set (`FLX-3617`) or a number the bundle does not have (`FLX-3706`) is refused. At most 64 parameters. The block also holds `evaluations`, `scale`, `seed` and `heldOutTolerance`.

Choosing what a fit may move is the user's decision, because it says which numbers are open. Propose a parameter with its register source and reason; do not add one the register states from data.

## The command

```bash
fluxsim calibrate --fit bundle --profile orders.profile.json --held-out held-out.profile.json --out calibrated
```

- `--profile` is the pinned profile, the only one the fit reads. Its digest must be the bundle's pin.
- `--held-out` is the held-out profile. It is opened after the patch is written, and its digest taken.
- `--out` names a folder. `calibrated.patch.json` is always written beside it; the folder holds the calibrated bundle only when the held-out check holds. Neither may exist (`FLX-3710`).
- The fit needs the `calibrate` extra (`FLX-3708`), the queries checked with the sqlglot that is installed (`FLX-3709`), and something to fit (`FLX-3707`).
- `--max-evaluations N` raises the engine's ceiling of 64 up to 4096, for a block that declares no `evaluations` of its own. Ask the user before you raise it: a longer fit is a long job, so say how long it may run.

The optimizer is CMA-ES, seeded; the objective is the sum of squared gaps between each calibration query's rate and its target. The same engine, optimizer version and platform give the same patch, so the patch is the record and not the search.

## The patch

A JSON document. Its header holds the method, the library versions, the seed, the evaluations, the scale, the pinned profile's digest and each calibration query's gap. Its `operations` are RFC 6902 `replace` operations, one per moved value. A `replace` needs its target to exist, so a patch moves a declared number and adds nothing. The input bundle is never changed. Never edit the patch or its digest. A moved value gets the register source `calibrated`, with the queries it was fitted to, the value before and after, the patch and profile digests and the held-out evidence.

## "Calibrated" waits on the held-out queries

A fit is called calibrated only when at least one held-out query has a target and every held-out query with a target falls within its tolerance (its own, or else `heldOutTolerance`). A held-out query with no tolerance to hold it to, or one that could not be counted, is a miss. Otherwise the command writes the patch, names each query that missed with its gap, exits 1 with `FLX-3712`, and writes no bundle. A fit with no held-out target is never called calibrated, and neither is one where no candidate beat the start point, which moves no value.

When it misses:

1. Report the patch, each missed query and its gap, exactly.
2. Say that the held-out queries are never fitted. The honest changes are in what the fit may move, or a better pinned profile from the profiler.
3. If the miss is only that no tolerance is declared, say so and ask the user how close the held-out rate must be. Do not choose a tolerance, and do not pick one that happens to pass.
4. Never change a `heldOut` flag, drop a held-out query, move its target into the pinned profile, or point `--held-out` at a profile made to pass.
5. Run the fit again only with a change the user chose. A fit's `--out` is taken, so name a new folder.

## Records and grades

A calibrated bundle pins the profile that held its targets, so that profile grades it (rule 2 of ADR 0020). The patch is a declaration and enters no grade. The held-out profile is graded under the same rule and its grade stated beside the pinned one's. A register entry that says it is calibrated while its held-out gap is larger than its tolerance is refused (`FLX-3716`).

In prose and in output, call it the calibration patch.
