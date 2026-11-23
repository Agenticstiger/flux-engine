---
title: "Seal and score a prediction"
description: "Seal a prediction about a run before its outcome is known with fluxsim accuracy register, score it against what was observed, and publish an outcomes-only accuracy record."
---

# Seal and score a prediction

A run shows what a scenario predicts. It does not show whether the prediction comes true,
which is why every report says `validity: not asserted`. The `fluxsim accuracy` commands
keep that score, apart from the evidence pack:

1. `register` seals a prediction before its outcome is known;
2. `score` checks it against what was observed, once the outcome is known;
3. `record` publishes the outcomes, misses included, for everything registered at one time.

## 1. Start from a shipped run

A prediction names the run it rests on by the run's digest. Ship a run and keep its id and
digest in variables, as the [tutorial](../tutorial/first-evidence-pack.md) does:

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
$ fluxsim run . --verify --ship -o ../out
...
run ... complete; shipped to ../out
$ RUN=$(ls .flux/runs)
$ DIGEST=$(grep 'Run digest' .flux/runs/$RUN/EVIDENCE.md | grep -o 'sha256:[0-9a-f]*')
```

## 2. Seal the prediction

Write the prediction down before its horizon ends. `predicted` is the value you expect,
and `interval_low` and `interval_high` are the range you claim it falls in; a prediction
without an interval is never scored. Numbers are decimal strings, and times are RFC 3339.
The `<<EOF` here, without quotes, lets the shell put the run's digest in:

```console
$ cat > ../prediction.json <<EOF
{
  "prediction_id": "recovery-rate-q3",
  "run_digest": "$DIGEST",
  "metric_id": "payment_recovery_rate",
  "statement": "Of the subscribers the detector flags in the third quarter, 62 in 100 pay within seven days.",
  "predicted": "0.62",
  "interval_low": "0.55",
  "interval_high": "0.69",
  "horizon_end": "2026-09-30T23:59:59Z",
  "registered_at": "2026-06-30T09:00:00Z"
}
EOF
$ fluxsim accuracy register --file ../prediction.json
registered recovery-rate-q3 (sha256:...)
```

`register` prints the prediction's seal: a digest of the prediction, its registration time
included. The record is written once, into the state directory, which is `./.flux` unless
`--state-dir` names another:

```console
$ ls .flux/accuracy/predictions
recovery-rate-q3.json
```

Publish the seal where others can see it when you register, in a ticket or a commit for
example. The seal is a digest, not a signature: anyone who can write the state directory
can write a matching one, so it shows that a record is the one first registered only to a
reader who has the digest from before the outcome was known.

Registering the same claim again writes nothing:

```console
$ fluxsim accuracy register --file ../prediction.json
recovery-rate-q3 is already registered with this seal (sha256:...); nothing was written
```

A different claim under the same id is refused, so a prediction cannot be swapped once its
outcome is known:

```console
$ sed 's/"0.62"/"0.60"/' ../prediction.json > ../changed.json
$ fluxsim accuracy register --file ../changed.json   # exit 1
FLX-7007: prediction id is already registered with a different seal -- ...
more: fluxsim explain FLX-7007  ...
```

Register a changed prediction under a new `prediction_id`.

## 3. Score it once the outcome is known

After the horizon, write down what was observed. `basis` says how it was measured:
`live-ab`, `historical-holdout`, or `none` when nothing was measured against the world.

```console
$ cat > ../readout.json <<'EOF'
{
  "prediction_id": "recovery-rate-q3",
  "metric_id": "payment_recovery_rate",
  "observed": "0.58",
  "sample_size": 1840,
  "basis": "historical-holdout",
  "observed_at": "2026-10-07T12:00:00Z"
}
EOF
$ fluxsim accuracy score --prediction-id recovery-rate-q3 --readout ../readout.json
recovery-rate-q3: within-interval
```

0.58 lies between 0.55 and 0.69, so the prediction is `within-interval`. Outside them it
would be `outside-interval`. A prediction with no interval, or a readout whose basis is
`none`, is `not-scored`: nothing is passed or failed without a measurement. The verdict is
written beside the prediction, under `.flux/accuracy/verdicts/`.

`score` refuses a readout observed before the prediction was registered
([FLX-7001](../reference/errors/FLX-7001.md)), because it could have shaped the prediction,
and one whose `prediction_id` or `metric_id` is not the prediction's
([FLX-7002](../reference/errors/FLX-7002.md)).

## 4. Publish the record

`record` writes one accuracy record for every prediction registered at the time you give,
exactly as the predictions spell it:

```console
$ fluxsim accuracy record --registered-at 2026-06-30T09:00:00Z --out ../accuracy.json
wrote ../accuracy.json
$ grep -E '"(passed|predictions_registered|predictions_scored)"' ../accuracy.json
  "passed": 1,
  "predictions_registered": 1,
  "predictions_scored": 1,
```

The record holds outcomes only: how many predictions were registered, how many could be
scored and how many were within their interval, the metrics, and the seal of each
prediction, so a reader can check each record against the digest published at
registration. It holds no predicted or observed value and no statement. A prediction whose
horizon has not closed is counted as registered and not as scored.

## When the store refuses

Every record is checked against its seal each time it is read. A
record edited by hand, for example to move its interval after the outcome is known, is
refused by `score` and by `record`, which names every record that fails:

```console
$ sed -i.bak 's/"0.55"/"0.50"/' .flux/accuracy/predictions/recovery-rate-q3.json
$ fluxsim accuracy record --registered-at 2026-06-30T09:00:00Z --out ../edited.json   # exit 1
FLX-7008: sealed prediction record does not verify -- ...
more: fluxsim explain FLX-7008  ...
$ mv .flux/accuracy/predictions/recovery-rate-q3.json.bak .flux/accuracy/predictions/recovery-rate-q3.json
```

The other refusals:

- [FLX-7003](../reference/errors/FLX-7003.md): a `prediction_id` that is not one plain
  identifier, such as one that climbs out of the store with `../`.
- [FLX-7004](../reference/errors/FLX-7004.md): `record` was given a registration time that
  no prediction carries.
- [FLX-7005](../reference/errors/FLX-7005.md): a prediction or readout that is not the
  right shape; the message lists every field at fault.
- [FLX-7006](../reference/errors/FLX-7006.md): no file at the path given.
- [FLX-7009](../reference/errors/FLX-7009.md): a symbolic link, or the
  wrong kind of file, where a record or one of the store's directories belongs. The store
  follows no link below `<state-dir>/accuracy`.

See [the evidence pack](../explanation/the-evidence-pack.md#how-a-claim-could-grow) for how
this fits beside the pack, and the [`accuracy` reference](../reference/cli/accuracy.md) for
every option.
