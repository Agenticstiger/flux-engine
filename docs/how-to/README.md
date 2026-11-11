---
title: "How-to guides"
description: "Steps for one goal each: model customers, add a journey, use the FLUID seam, calibrate from aggregates, ship with evidence, load a simulation into a database."
---

# How-to guides

Each guide gets one job done and assumes you have already done the
[tutorial](../tutorial/first-evidence-pack.md). They are independent: every guide starts
from a fresh project made with `fluxsim init`.

- [Model your customers](./model-customers.md): change the population, add a persona, test a segment expression.
- [Add a journey](./add-a-journey.md): a new signal, detector and journey, wired in through a module.
- [Use the FLUID seam](./use-the-fluid-seam.md): change the contract the rows follow and keep the pinned digest honest.
- [Calibrate from aggregates: declare a scorecard](./calibrate-from-aggregates.md): add a Playback and read back its declared scorecard. Nothing is fitted in this release.
- [Ship with evidence](./ship-with-evidence.md): the fidelity gate, committed slices, signing, and sending the pack on.
- [Load a simulation into a database](./load-a-simulation-into-a-database.md): write the rows to Parquet or Arrow IPC files, or into SQLite, DuckDB, PostgreSQL, Snowflake or BigQuery.

For why things work the way they do, see [Explanation](../explanation/README.md).
