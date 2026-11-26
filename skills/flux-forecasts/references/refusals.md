# Refusals of forecasts

For any code: run `fluxsim explain FLX-nnnn` (offline, the same text as its page), apply the remedy, then run the same step again. `fluxsim explain --list` lists every code of the installed engine. The index of
[all error codes](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/reference/errors/README.md)
links each page. Never weaken a check, never change a registered forecast, never record past a refusal.

## Code families

| Codes | What they mean | Next step |
|---|---|---|
| `FLX-3105` | a key the kernel does not read: kernel 0.1.0 with `responses` or `forecasts` | use kernel 0.2.0, the default; the message names the kernel |
| `FLX-3660` to `FLX-3665` | a response family: the block, an id, a document it names, its segments, its estimate, or a journey whose reactions the World's temperature dials | fix the family the message names; the shapes are in the declarations reference of this skill |
| `FLX-3666` to `FLX-3669` | the forecasts block: its keys or its missing responses, an item's id, a response, arm, segment or basis that does not exist, a playback or confidence that is not usable | fix the item named |
| `FLX-3670` | a sweep too short for the confidence | lengthen `spec.sweep`, or lower the confidence, with the user's agreement |
| `FLX-3109` | the bundle pins a profile and none was passed | pass that profile with `--profile` |
| `FLX-3671` | responses declared, no profile pinned | pin the profile and pass it with `--profile` |
| `FLX-3672`, `FLX-3673` | the response estimates a plan records are not well formed, or a replicate does not fit the families | plan again from the bundle; report it if it repeats |
| `FLX-3674` | a confidence needing more sweep runs than the engine allows | lower the confidence |
| `FLX-7140` | too few replicates with a value for an interval | a larger segment or arm, or a longer sweep; nothing was registered |
| `FLX-7141` | no forecasts to make | declare them |
| `FLX-7142` | the horizon ends before the registration | ask the user for the right instants |
| `FLX-7143` | the next state is the state the forecasts were made on | pass the later release |
| `FLX-7144` | no forecast is registered for the bundle | run `forecast make` first, with the same bundle and state directory |
| `FLX-7145` | the Playback's drift declaration cannot be read | the declaration is the bundle author's to fix |
| `FLX-7146` | the calibration on the pinned state is stale and `onBreach` is `fail`; every verdict was written | report it as a finding; see the scores reference |
| `FLX-7001` | an observation predates its registration | the instant is wrong; ask the user |
| `FLX-7007` | a different forecast under a used id | a registered forecast stands; never replace it after the next state is seen |
| `FLX-4005` | the run's grade is below a column's minimum | the `flux-privacy-and-profiling` skill; never lower a grade or change a label |

## Handling one

1. Quote the code and its one-line meaning to the user.
2. Name what `fluxsim explain` says to do.
3. If the remedy is a file of the bundle and the user's request covers it, change only that. If it is a decision (an instant, a confidence, a basis, a threshold, a profile), ask the user.
4. Run the step again. Do not loop on one code without a change.

A usage error (exit code 2, no code) means the installed engine differs from this skill. Read `fluxsim <command> --help`.
