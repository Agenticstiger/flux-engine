---
title: "Reference"
description: "The command line, every error code, the Python API and the schemas this version validates against."
---

# Reference

Lookup pages. Most are generated from the code, so they cannot drift from it.

- [Command line](./cli/README.md): every `fluxsim` command, its options, exit codes and environment.
- [Error codes](./errors/README.md): every `FLX-nnnn` code, one page each. `fluxsim explain FLX-nnnn` prints the same text offline.
- [Python API](./python-api.md): `flux_engine.api` and `flux_engine.engine`, name by name.
- [Schemas](./schemas.md): the FLUX and FLUID schemas this version validates against, pinned to a specification version.
- [The plan and the run directory](./plan-and-run-directory.md): what `plan.json` holds, the keys that appear only when a plan needs them, the files of a run directory and the ledger in `init --json`.
- [How init reads a profile](./how-init-reads-a-profile.md): the rules `fluxsim init` applies to a contract and a profile, and the register source each gives.
- [Specifications](../spec/README.md): the rules behind the engine, one document each: the kernel, the seam, the expression languages, the profile and its request, the parameter register and the scores.
