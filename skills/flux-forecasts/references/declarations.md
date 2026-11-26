# Response families and forecasts

Two keys of the Simulation's namespace (`extensions['com.agentics.flux/1']`), neither read by an engine before this feature. A bundle that declares neither has the bytes and outputs it had. Both are kernel 0.2.0 only; kernel 0.1.0 refuses them (`FLX-3105`, naming the kernel). The keys are closed: a key nothing reads is refused. The page that shows both declarations in a bundle is
[forecast the next state](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/how-to/forecast-the-next-state.md);
copy its shape from there and do not invent a key. The design is
[ADR 0029](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/adr/0029-response-families-forecast-the-next-state-and-every-forecast-is-scored.md).

## A state is a profile release

State k is the profile the bundle pins, named by its digest. State k+1 is a later release answering the same request, given only when a forecast is scored. A response family and a forecast read the pinned profile and nothing else when they are made. `fluxsim init` writes the pin when it authors a bundle from a profile.

## `responses`

A list of 1 to 256 families. Each carries exactly these keys (`FLX-3660`):

| Key | Holds |
|---|---|
| `id` | the family's name, well formed and not repeated (`FLX-3661`) |
| `treatment` | a Treatment of the bundle |
| `experiment` | an Experiment with a variant that names that treatment |
| `journey` | a Journey of the bundle |
| `segments` | a map from each group key of the estimate's queries (a level of their `groupBy` column) to a Segment |
| `estimate` | `treated` and `control`, two calibration queries of the consumers registry with the same `groupBy`, and `confidence` |

A name the bundle does not have, or one of the wrong kind, is refused (`FLX-3662`); malformed segments (`FLX-3663`); an estimate that does not name two grouped calibration queries (`FLX-3664`). A held-out query is refused as an estimate. A family on a journey whose reactions the World's temperature dials is refused (`FLX-3665`).

How a family acts, for the agent to report accurately:

- For each segment, the control cell gives the records and matches of the untreated arm, and the treated cell those of the treated arm. An exact two-proportion readout gives the control rate `p0`, the effect, and its interval at `confidence`.
- A withheld cell, or an arm with no records, gives that segment no estimate; it is reported, and its subjects keep their journey's own probability. A match count of zero is an estimate, a rate of zero.
- In the journey, a subject in a segment succeeds with probability `p0` when untreated, and with `p0` plus the effect (kept between 0 and 1) when its variant names the treatment. A subject in no segment keeps its persona's probability.
- The parameter register lists each estimate with the cells it was read from.

## `forecasts`

A mapping with `playback`, `confidence` and `items` (`FLX-3666`, `FLX-3669`). Each item carries:

| Key | Holds |
|---|---|
| `id` | the item's name, well formed and not repeated (`FLX-3667`) |
| `response` | a family's id |
| `arm` | `treated` or `control` |
| `segment` | a segment key of the family |
| `basis` | optional: `historical-holdout` when absent, the cautious reading; `live-ab` only when the user states that the next state's arms come from a live randomised comparison |

A response, arm, segment or basis that does not exist is refused (`FLX-3668`). `basis` is a statement about the next state's data, so it is the user's to make; never set `live-ab` on your own.

`playback` names the Playback whose `drift` rule is used when forecasts are scored. A Playback's drift is read only when a forecast names it, so a bundle that declares drift and no forecast behaves as it did. `confidence` is an exact decimal, such as 0.9.

## What an item forecasts

The rate the next state's profile will show in the cell of its family's `treated` or `control` query, for its segment. The twin's value in one run is the share, among the segment's subjects in the arm, whose first reaction in the family's journey succeeded. A run with no such subject gives none.

## The sweep

A forecast runs the Simulation's `spec.sweep` as its replicates, each seeded as the sweep already seeds it and each replayable on its own. A Simulation with no sweep long enough is refused (`FLX-3670`). The fewest runs at confidence `c` is 2 divided by (1 minus `c`), rounded up: 20 at 0.9. A confidence needing more than 100,000 runs is refused (`FLX-3674`). Replicate `r` of `R` takes the effect at the midpoint of the `r`-th of `R` equal parts of the effect's interval, so the spread holds both the draws and what state k leaves unknown about the effect.

The forecast is the median of the `R` values. Its interval runs from the value of rank `a` to the value of rank `R + 1` less `a`, with `a` the largest whole number not above `R` times (1 minus `c`) over 2. Fewer values than that give no interval (`FLX-7140`): make the segment or the arm larger, or the sweep longer.

## A forecast is as good as its scenario

The bundle for the next period states what that period holds, such as the experiment's weights and the population's mix, and the forecast assumes it. Say so when you report a forecast. A Wald interval, which the effect's interval is, covers less than it says on small counts; the scores show that.
