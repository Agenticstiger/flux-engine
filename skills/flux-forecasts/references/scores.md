# Reading the scores

What `fluxsim forecast score` prints, what the drift line means, how the evidence pack shows the forecasts, and how to walk forward over a series of states. The worked page is
[forecast the next state](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/how-to/forecast-the-next-state.md).

## A verdict

One line per forecast. `score` prints them ordered by id, not in item order (`make` prints in item order):

- `within-interval` or `outside-interval`, the observation and the sample size it rests on;
- the absolute error between the observation and the forecast;
- the interval score of Gneiting and Raftery (2007) at the level alpha of one minus the confidence: the width of the interval, plus `2 / alpha` times the distance below the interval when the observation falls under it, or above it when it falls over. Lower is better, a narrow interval pays for every miss, and an honest interval has the lowest expected score;
- no grades on the printed line: the grade of the pinned profile and of the next profile is in `--json` (`artifacts.grades`) and the verdict file, so read grades from `--json`;
- or `not scored` with the reason: a withheld cell, or a cell with no records.

Report each as printed. A miss is a result, and so is a forecast not scored. Do not call a forecast good or bad from one score, and do not average scores into a statement of accuracy: the published figure is the measured interval coverage below.

## Drift between the two states

When it scores, the engine compares the two states over every statistic both hold with the same definition, levels or bins, and grouping: each histogram of a column, and each query cell the families read. A statistic with a withheld cell in either state is left out and listed.

- The metric is the one the Playback named by `forecasts.playback` declares: `psi`, `kl` or `ks`, with its `threshold` and `onBreach`.
- A statistic whose value passes the threshold has drifted. One drifted statistic marks the calibration made on state k stale.
- The printed line names the metric, the threshold, the largest value, the statistic it was found at, and whether the calibration is stale. Every verdict of the pass records the same.
- The score is recorded either way. With `onBreach: warn`, the default, the command exits 0; with `fail` it exits 1 with `FLX-7146` after every verdict is written.

A stale calibration is a finding to hand the user, with the statistic: it means the calibration made on state k no longer describes the data. The user decides to calibrate again on the next state (the consumer queries and the calibration patch are the `flux-consumer-queries` skill's). Never raise the threshold or change `onBreach` to make it pass. A Playback's drift is read only when a forecast names it.

## The evidence pack

`fluxsim ship` reads the outcome-accuracy store and adds a Forecast Accuracy section to `EVIDENCE.md`, after Fidelity, when the store holds at least one forecast registered for the run's input digest, scored or not. It lists every such forecast, ordered by id: metric, state digest, registration time, forecast and interval, observation, inside or outside, interval score, absolute error and drift. A forecast with no verdict yet is listed as pending. Then the totals: scored, inside their interval, the measured coverage and the Playback's declared coverage. The engine never rewrites the declared figure.

The section also reports, per state digest, how many forecasts the store holds on that state across every bundle, and the grade of each profile read. `forecast-accuracy.json` in the run directory holds the same rows and is packed with the run. A run with no forecast registered for its input digest keeps its `EVIDENCE.md` and its pack as they were. The published `accuracy-record/1` does not change: it counts every prediction of its window, misses included, and carries no value.

Say plainly: measured coverage over a few forecasts is a small number of trials, not a result about the twin.

## Grades

A family, a forecast and a declaration never enter a grade. The grade of a run is read from the profile it pins; the next state's profile is graded under the same rule when it scores, each verdict states both, and `ship` states the weakest of every profile the run read. A profile that publishes its query cells exactly is graded P1 by that rule.

## Walking forward

For states 1 to `n`, the bundle pinned to state `k` forecasts state `k + 1`, and state `k + 1` scores it, for `k` up to the one before the last. For each pass:

1. get a bundle pinned to state `k`: a copy of the previous pass's bundle, re-pinned to the new profile's digest (`fluxsim init` does not carry `responses`, `forecasts`, `sweep` or the Treatment, Experiment and Journey documents). No fluxsim command re-pins, and you never open profiles, so ask the user to make or approve the re-pin; [the how-to page](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/how-to/forecast-the-next-state.md) shows a script;
2. make the forecasts with the instants the user gives for `k`;
3. score them when state `k + 1` exists, with the instants the user gives;
4. ship only if the user asks.

The instants must increase from pass to pass, and each registration is sealed before the next state is released. Each pass has its own bundle and so its own input digest, so each pass's pack has its own section; the count of forecasts on a state spans every bundle. Over the record's window, the share of scored forecasts inside their interval is the measured coverage the pack reports beside the declared one.

Each pass is a separate step the user confirms; never run several passes in one go, and never make a forecast for pass `k + 1` from a profile you have not been given.
