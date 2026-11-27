# Making and scoring forecasts

The two commands, their options and what each leaves behind. The pages are
[forecast the next state](https://github.com/Agenticstiger/flux-engine/blob/v0.13.0/docs/how-to/forecast-the-next-state.md)
and [the command reference](https://github.com/Agenticstiger/flux-engine/blob/v0.13.0/docs/reference/cli/forecast.md).
For the options of the installed engine, `fluxsim forecast make --help` and `fluxsim forecast score --help` are the authority.

## `fluxsim forecast make`

```bash
fluxsim forecast make bundle --profile orders.profile.json --registered-at REGISTERED_AT --horizon-end HORIZON_END
```

- `--registered-at` is the instant the forecasts are sealed at, and `--horizon-end` the instant after which the next state can be read, both RFC 3339 and both required. The engine reads no clock, so both are the user's. A score may not observe anything before `--registered-at` (`FLX-7001`).
- `--profile` is the profile the Simulation pins; always pass it for a bundle that pins one, or `make` refuses with `FLX-3109`. It is refused too when its digest is not the pinned one.
- `--simulation-id` picks one Simulation when the bundle holds several. `--jobs` sets the processes for rows drawn from statistics; the digests are the same whatever the number. `--state-dir` names the run-state folder: pass the same one to every step of one forecast. `--json` gives the machine form.

What it does: it runs the sweep as replicates, and for each item registers the median and the interval in the outcome-accuracy store under the state directory, sealed and write-once.

- The prediction's id is the item's id, a dot and the first 16 hexadecimal digits of the pinned profile's digest, so an item has one prediction per state.
- It holds the forecast, the interval, the horizon, and two more sealed members: the pinned profile's digest and the bundle's input digest.
- The same forecast again prints `already registered` and changes nothing. A different forecast under a used id is refused (`FLX-7007`).
- Nothing is registered for any item when one cannot give its interval (`FLX-7140`). A Simulation that declares no forecasts has nothing to make (`FLX-7141`); a horizon that ends before its registration is refused (`FLX-7142`).

After `make`, tell the user to publish each seal where others can see it before the next state is released. That publication, with the time given, is what makes the later score a test the forecast could fail. The page on sealing a prediction is
[seal and score a prediction](https://github.com/Agenticstiger/flux-engine/blob/v0.13.0/docs/how-to/seal-and-score-a-prediction.md).

## `fluxsim forecast score`

```bash
fluxsim forecast score bundle --profile orders.profile.json --next next.profile.json --observed-at OBSERVED_AT
```

- `--profile` is the pinned state the forecasts were made on; `--next` is a later release of the same request. A next state that is the pinned one is refused (`FLX-7143`).
- `--observed-at` is the instant the next state was read, RFC 3339. It may not predate a forecast's registration (`FLX-7001`); if it does, report that the instant is wrong and ask the user for the real one.
- No forecast registered for the bundle gives `FLX-7144`.

It reads the next state's profile and gives every forecast registered for the bundle's input digest a verdict, none skipped. The observation is the next state's cell for the forecast's query and segment: its matches over its records, with the records as the sample size. A withheld cell, or one with no records, leaves the forecast not scored, with the reason in its verdict.

## `--json`

Add `--json` before or after the command name for one envelope: `ok`, `exitCode`, `stage`, `error` (`code` and `message`, or null) and `artifacts`. Read the exit code and `ok`, not only the printed text.

## What you hand back

After `make`: every item's id, forecast and interval, and the instants you were given. After `score`: every verdict, with the drift line, the grades and the exit code. Do not leave a forecast out of the report because it missed.
