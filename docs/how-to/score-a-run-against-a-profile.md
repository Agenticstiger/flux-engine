---
title: "Score a run against a profile, in full or sampled"
description: "Pin a data owner's profile in the Simulation, score a run against it with verify --profile, and check its means on a sample of blocks with verify --sample."
---

# Score a run against a profile, in full or sampled

Use this page to check a run against a profile, first on every record
and then on a sample of them.

A profile is a JSON document of aggregates about the data a bundle imitates: a row count
per table and, per column, metrics such as a count or a maximum, and statistics such as a
mean ([PROFILE-SPEC.md](../spec/PROFILE-SPEC.md)). The data owner makes it where the data
lives, with [`fluid profile`](https://github.com/Agenticstiger/forge_docs/blob/main/docs/cli/profile.md)
from forge-cli's `profile` extra, so only aggregates leave and the engine never reads a
record of the real data.

**What a profile from `fluid profile` can score.** The fidelity check and the scorecard
compare the statistics that the bundle's declared properties read. From data-product-forge
0.31.0, `fluid profile` writes one: an ungrouped `mean/1` for each number column, unless
the output checks withhold it. A `mean` property over such a column is scored when the
fidelity block's `encoding.fractionalBits` equals the profile's (16, unless
`fluid profile --fractional-bits` set another), and `ship --profile` packs a run that
passes. A property that reads another statistic, such as a support or a mean over a date
column, is refused: `verify --profile` fails the check with
[FLX-5037](../reference/errors/FLX-5037.md), and `ship --profile` withholds the emit with
[FLX-8001](../reference/errors/FLX-8001.md). A mean that the output checks withheld is
censored: it is left out of the score, and when every declared property is censored,
nothing is scored and `ship --profile` withholds the emit too. The example on this page
writes its profile by hand, with one `mean/1` of the kind `fluid profile` writes, to show
the check.

The engine scores a run against a profile. It does not draw rows from one: the numbers
that shape the rows, such as a column's range, are the Simulation's own. `fluxsim init
--contract --profile` writes them from a profile ([Write a bundle from a
profile](./write-a-bundle-from-a-profile.md)), and this page writes them by hand. The
profile is what the run is then checked against.

## 1. Start a project

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
```

## 2. Get the profile

The data owner sends the profile. Keep it outside the bundle directory, which may hold
only the bundle's documents ([FLX-3108](../reference/errors/FLX-3108.md)). This page
writes one by hand, so it runs anywhere. It says the real table has 3980 rows and that
their mean `amount` is about 49.33:

```console
$ cat > ../source.profile.json <<'EOF'
{
  "_producer": "https://example.com/hand-written-profile",
  "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json",
  "encoding": { "fractionalBits": 16, "rounding": "floor" },
  "disclosureRules": {
    "acro-default": {
      "minimumCell": 10,
      "zerosDisclosive": true,
      "roundingBase": 5,
      "dominance": { "n": 2, "k": "0.90" },
      "pRatio": "0.10"
    }
  },
  "tables": {
    "payment_recovery_moment": {
      "rowCount": 3980,
      "columns": {
        "amount": {
          "domain": "number",
          "statistics": [
            {
              "definition": "mean/1",
              "sampleSize": { "min": 3978, "max": 3982 },
              "disclosure": "acro-default",
              "groups": [{ "group": "", "k": 3980, "Sx": "12866905702", "status": "F" }]
            }
          ]
        }
      }
    }
  }
}
EOF
```

A mean is written as `k`, the number of values, and `Sx`, their sum on the grid that
`encoding` names: each amount times 2^16, rounded down. `Sx / k / 65536` is the mean,
49.33.

The `mean` property below reads this `mean/1` statistic. The seven metrics `fluid profile`
writes per column (counts, sum, minimum, maximum and quartiles) are read by the shape flags
in step 4, not by a property, and the FLX-5037 message says which statistic a property was
missing.

## 3. Pin the profile and declare the check

Replace `simulation.flux.yml`. Two things are new beside the starter:

- `profile.digest`, under `extensions` and `com.agentics.flux/1`, beside `emits`, pins the
  profile by its digest. The pin is part of the bundle, so the run is bound to that exact
  profile. `fluid profile` prints the digest when it writes the file. For a profile made
  another way, pin any digest and run `plan`: [FLX-3110](#flx-3110-the-profile-is-not-the-pinned-one)
  names the digest of the file you passed.
- A `fidelity` block for the emit. Its `amount` range, 20 to 80, is written by hand to
  suit what the owner says about real amounts; the engine draws the rows from it. The
  `mean` property is what the profile checks: the run's mean `amount` must be within 2 of
  the profile's.

```yaml
# simulation.flux.yml
fluxVersion: "0.5.0"
kind: Simulation
id: sim-payment-recovery
name: Payment Recovery Simulation
metadata:
  owner:
    team: growth-lab
extensions:
  com.agentics.flux/1:
    profile:
      digest: sha256:a9e72d36cacdb20d8f59617b2ae7df825ccfed288e876548234bc494fa32f86c
    emits:
      payment_recovery_moment:
        rows: 4000
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1000, max: 100000 }
          columns:
            amount:
              range: { min: "20", max: "80" }
              null: exclude
            market:
              support:
                values: ["IE", "GB", "FR", "DE"]
              maxCardinality: 8
              null: fail
            aborted_at:
              range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }
              null: fail
          properties:
            - property: mean
              column: amount
              tolerance: { type: absolute, value: "2" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
spec:
  worldRef: q3-retention-world
  fidelity: deterministic
  seed: 42
  timeBounds:
    start: "2026-07-01T00:00:00Z"
    end: "2026-09-30T23:59:59Z"
  personaRefs: [persona-pragmatic-family]
  signalRefs: [sig-payment-aborted]
  moduleRefs: [module-payment-recovery]
  emits:
    - productRef: telco.gold.payment_recovery_moment
      exposeId: payment_recovery_moment
      fluidVersion: "^0.7.3"
      provenance:
        contractDigest: "sha256:b9b5ee575a8b313f6f72d039653d8be5d76e7ecc0012d7bd6ad2d37e0e60435d"
```

## 4. Run it and score it in full

Hand the profile to the run with `--profile`. `plan` pairs the file with the pin, and
`verify` and `ship` compare the run with it:

```console
$ fluxsim run . --profile ../source.profile.json --verify
plan written to .flux/runs/.../plan.json (runId=..., planDigest=sha256:...)
applied to .flux/runs/... (runDigest=sha256:...)
emitted payment_recovery_moment: 4000 rows (generator)
reproducible: not checked | validity: not asserted
...
  fidelity payment_recovery_moment: passed -- 1 of 1 declared property within the margin against the pinned profile
  scorecard: overall 0.9846 against profile sha256:...
    dimensions: marginals 0.9846; not measured: structure, joint, crossTable, temporal, invariants, queries, defects
    table payment_recovery_moment: 0.9846 (1 of 1 property scored)
    shape: no flags in 1 profiled column (no count table to test)
run ... complete
```

The fidelity line is the check: the run's mean `amount`, 50.25, is within 2 of the
profile's 49.33, so the property passes. The scorecard says how close the two are, on a
scale where 1 is the same value. For a mean it is one less the gap between the means over
the width of the column's declared range: 1 - 0.92 / 60 = 0.9846. The table and overall
scores gather the properties' scores, and the dimensions say which kinds of check were
measured ([SCORECARD-SPEC.md](../spec/SCORECARD-SPEC.md)). The shape flags check every
column the profile describes for cells the run leaves empty, one value holding an unlikely
share of the rows, a constant column, and a distinct count far from the one the profile
predicts. They withhold nothing.

`ship` needs the profile too, and packs the scores as `run/scorecard.json`. A property
outside its margin withholds the ship ([Ship with
evidence](./ship-with-evidence.md#score-against-a-pinned-profile) has thresholds on the
scores).

## 5. Check the mean on a sample

`verify --sample FRACTION` compares the run with the profile on a share
of each emit's blocks instead of every record. A block is `--sample-block` records in a
row, 1000 when not given, and `--sample-seed`, 0 when not given, picks which blocks are
read: the same run, emit and seed always read the same ones. Only the comparison is
sampled. The digests and the rows' conformance still read every record, and `ship` always
reads every record ([SAMPLE-SPEC.md](../spec/SAMPLE-SPEC.md)).

Keep the run's id in a variable. Then read half of the run's 4000 records, in blocks of
500:

```console
$ RUN=$(ls .flux/runs)
$ fluxsim verify . --run-id $RUN --profile ../source.profile.json --sample 0.5 --sample-block 500
reproducible: not checked | validity: not asserted
...
  fidelity payment_recovery_moment: passed -- sampled 4 of 8 blocks (2000 of 4000 records): means 1 within, 0 outside, 0 undecided by the 95 % interval
  scorecard: not checked -- a sampled verify forms no scorecard; verify without --sample for it
```

A sample cannot give the run's mean exactly. It gives an estimate and a 95 % interval
around it: a range made so that, over many samples drawn this way, about 95 in 100 hold
the mean of every record. `--json` shows both:

```console
$ fluxsim --json verify . --run-id $RUN --profile ../source.profile.json --sample 0.5 --sample-block 500 > ../sampled.json
$ grep -m 1 -A 4 '"estimate"' ../sampled.json
                "estimate": "50.14358521270751953125",
                "group": 0,
                "interval": [
                  "49.5011875364...",
                  "50.7859828889..."
```

The estimate is 50.14, and the interval runs from 49.50 to 50.79. The property allows 2 either side of the profile's
mean, so its band runs from 47.33 to 51.33. The interval decides, not the estimate:

- **within**: the interval lies inside the band, as here.
- **outside**: the interval lies wholly below or wholly above the band.
- **undecided**: the interval crosses an edge of the band. Only more records can settle
  it, so it fails the check as outside does.

Fewer blocks give a wider interval. With a quarter of the blocks, two are read, the
interval runs from 46.79 to 52.94, and it crosses both edges:

```console
$ fluxsim verify . --run-id $RUN --profile ../source.profile.json --sample 0.25 --sample-block 500   # exit 1
reproducible: not checked | validity: not asserted
...
  fidelity payment_recovery_moment: failed -- sampled 2 of 8 blocks (1000 of 4000 records): means 0 within, 0 outside, 1 undecided by the 95 % interval
    property 000:mean:amount (mean of amount): undecided at the 95 % interval
...
```

Read more blocks, or verify without `--sample`. This release estimates means only: a
property of another kind is named as not estimated under `--sample`, and is checked by a
`verify` without it. A sampled verify forms no scorecard either; `verify` without
`--sample`, and `ship`, do.

## Troubleshooting

### FLX-3109: the pin and the profile do not pair

`plan`, and `run`, refuse a Simulation that pins a profile when no `--profile` is given, a
`--profile` when the Simulation pins none, and a profile file they cannot read. `ship`
refuses a pinned run without its profile the same way:

```console
$ fluxsim ship . --run-id $RUN -o ../out   # exit 1
FLX-3109: the profile the Simulation pins and the profile supplied do not pair -- sim-payment-recovery: the Simulation pins profile sha256:...; pass that profile with --profile
more: fluxsim explain FLX-3109  ...
```

Pass the profile file the Simulation pins, or pin the digest of the one you mean to use,
or drop whichever of the two is not wanted. Nothing was written. Without `--sample`, `verify` does not
refuse a pinned run handed no profile: it reports the fidelity check as `declared, not
checked` and the scorecard as `not checked`, and names the profile to pass. A sampled verify
compares with the profile, so `--sample` without `--profile` is refused with FLX-3109 too. `apply --run-id` refuses
`--profile` with exit status 2, because the recorded plan was paired with its profile when
it was taken; `apply` reads `--profile` only when it takes a fresh plan.

### FLX-3110: the profile is not the pinned one

The digest of the file you passed differs from the pin. The message names both:

```console
$ sed 's/12866905702/12866905703/' ../source.profile.json > ../other.profile.json
$ fluxsim verify . --run-id $RUN --profile ../other.profile.json   # exit 1
FLX-3110: the supplied profile is not the one the Simulation pins -- sim-payment-recovery: the Simulation pins profile sha256:..., and other.profile.json is sha256:...
more: fluxsim explain FLX-3110  ...
```

Either the profile changed after it was pinned, or it is another profile. Pin the new
digest only once you know the change is meant.

### A number too long to read

A profile writes every number but a count as a decimal string, at most 1000 digits long,
and a longer one refuses the whole profile with
[FLX-5036](../reference/errors/FLX-5036.md). In the records the fidelity check reads, such
as a committed slice, a number that needs more than 1000 digits written out, for example
`"1E+99999999"`, is refused before it is read, with
[FLX-5005](../reference/errors/FLX-5005.md) and the value class `over-long number`. `fluid profile` refuses one the same way.
