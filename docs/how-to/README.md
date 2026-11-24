---
title: "How-to guides"
description: "Steps for one goal each: model customers, add a journey, use the FLUID seam, calibrate from aggregates, draw a column given another, draw a column given the bin of another, draw number columns together, ship with evidence, write a bundle from a profile, score a run against a profile, seal and score a prediction, forecast the next state and score it, load a simulation into a database, size a run, split it and time it, publish a catalogue whole, give every parent its children, check consumer queries and calibrate to them, fix values and seed rows, give an entity a history, project a history, keep dates inside their parent's lifetime."
---

# How-to guides

Each guide gets one job done and assumes you have already done the
[tutorial](../tutorial/first-evidence-pack.md). They are independent: every guide starts
from a fresh project made with `fluxsim init`.

- [Model your customers](./model-customers.md): change the population, add a persona, test a segment expression.
- [Add a journey](./add-a-journey.md): a new signal, detector and journey, wired in through a module.
- [Use the FLUID seam](./use-the-fluid-seam.md): change the contract the rows follow and keep the pinned digest honest.
- [Calibrate from aggregates: declare a scorecard](./calibrate-from-aggregates.md): add a Playback and read back its declared scorecard. This path fits nothing; fitting is in [Check consumer queries](./check-consumer-queries.md).
- [Draw a column given another](./draw-a-column-given-another.md): declare a histogram drawn given a categorical column, from a grouped histogram's counts, and see the fidelity gate check the link.
- [Draw a column given the bin of another](./draw-a-column-given-the-bin-of-another.md): declare a column drawn given the bin of a number column, from a table grouped by that column's bins, and see the fidelity gate check the link with `groupByBins`.
- [Draw number columns together](./draw-number-columns-together.md): declare a Gaussian copula so two number or date columns are drawn with a stated rank correlation, keep each column's histogram, and score the correlation against a profile.
- [Ship with evidence](./ship-with-evidence.md): the fidelity gate, committed slices, signing, and sending the pack on.
- [Write a bundle from a profile](./write-a-bundle-from-a-profile.md): turn a FLUID contract and a profile of the data into a bundle that runs, every number labelled by what it rests on.
- [Score a run against a profile, in full or sampled](./score-a-run-against-a-profile.md): pin a data owner's profile, score a run against it, and check its means, histograms and quantile bins on a sample of blocks.
- [Seal and score a prediction](./seal-and-score-a-prediction.md): register a prediction before its outcome is known, score it against what was observed, and publish the outcomes.
- [Forecast the next state and score it](./forecast-the-next-state.md): declare a response family and forecasts, make them before the next profile exists, score them against it with an interval score, see what drift flags, and read every forecast in the evidence pack.
- [Size a run, split it and time it](./size-and-split-a-run.md): plan a multiple of the bundle's rows with `--scale`, make a run in shares with `--shard`, and see where the time went with `--timings`.
- [Publish a catalogue whole](./publish-a-catalogue.md): declare a small table as a reference expose, so its rows are the rows the profile publishes and the tables that point at it pick only those.
- [Give every parent its children](./give-every-parent-its-children.md): declare how many children each parent has with a `counts` entry, so no invoice is without a line, the histogram of children per parent is met class for class, a parent's summary holds, and a child table comes out grouped by parent.
- [Check consumer queries and calibrate to them](./check-consumer-queries.md): declare the SQL your consumers run, have it checked and counted on a run in a locked DuckDB, read the `queries` dimension of the scorecard, and fit declared numbers to the queries' rates, with held-out queries deciding whether the result is called calibrated.
- [Fix values and seed rows](./fix-values-and-seed-rows.md): declare `conditions` so a group of rows holds fixed values and the rest of each row is drawn given them, exactly, with seed rows as groups of one, and see what the evidence pack says about them.
- [Give an entity a history](./give-an-entity-a-history.md): declare a chain of versions for each entity of a table with a validity window, read the previous version in a rule, and let a dated row read the version valid on its date.
- [Project a history](./project-a-history.md): derive a snapshot, a change event stream and periodic rows from a history, with row counts worked out at plan time, and grow the window without rewriting a row.
- [Keep dates inside their parent's lifetime](./keep-dates-inside-their-parents-lifetime.md): read what `fluxsim init` writes from a profile's date pairs, calendars and exposure (lifetimes, dates within a parent's lifetime, seasonal rates, exposure), see what it left unused and why, read the `lifetime:`, `within:`, `calendar:` and `exposure:` lines of `fluxsim verify`, and adjust a fact that fails.
- [Load a simulation into a database](./load-a-simulation-into-a-database.md): write the rows to Parquet or Arrow IPC files, or into SQLite, DuckDB, PostgreSQL, Snowflake or BigQuery.

For why things work the way they do, see [Explanation](../explanation/README.md).
