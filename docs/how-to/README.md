---
title: "How-to guides"
description: "Steps for one goal each: model customers, add a journey, use the FLUID seam, calibrate from aggregates, draw a column given another, ship with evidence, write a bundle from a profile, score a run against a profile, seal and score a prediction, load a simulation into a database."
---

# How-to guides

Each guide gets one job done and assumes you have already done the
[tutorial](../tutorial/first-evidence-pack.md). They are independent: every guide starts
from a fresh project made with `fluxsim init`.

- [Model your customers](./model-customers.md): change the population, add a persona, test a segment expression.
- [Add a journey](./add-a-journey.md): a new signal, detector and journey, wired in through a module.
- [Use the FLUID seam](./use-the-fluid-seam.md): change the contract the rows follow and keep the pinned digest honest.
- [Calibrate from aggregates: declare a scorecard](./calibrate-from-aggregates.md): add a Playback and read back its declared scorecard. Nothing is fitted in this release.
- [Draw a column given another](./draw-a-column-given-another.md): declare a histogram drawn given a categorical column, from a grouped histogram's counts, and see the fidelity gate check the link.
- [Ship with evidence](./ship-with-evidence.md): the fidelity gate, committed slices, signing, and sending the pack on.
- [Write a bundle from a profile](./write-a-bundle-from-a-profile.md): turn a FLUID contract and a profile of the data into a bundle that runs, every number labelled by what it rests on.
- [Score a run against a profile, in full or sampled](./score-a-run-against-a-profile.md): pin a data owner's profile, score a run against it, and check its means on a sample of blocks.
- [Seal and score a prediction](./seal-and-score-a-prediction.md): register a prediction before its outcome is known, score it against what was observed, and publish the outcomes.
- [Load a simulation into a database](./load-a-simulation-into-a-database.md): write the rows to Parquet or Arrow IPC files, or into SQLite, DuckDB, PostgreSQL, Snowflake or BigQuery.

For why things work the way they do, see [Explanation](../explanation/README.md).
