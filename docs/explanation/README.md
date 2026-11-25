---
title: "Explanation"
description: "Background on the architecture and its patterns, the six-step loop, determinism and the seed tree, the FLUID seam, the evidence pack, running offline, and where FLUX fits among other tools."
---

# Explanation

These pages answer "why is it like this?". They connect ideas rather than give steps; for
steps, see the [how-to guides](../how-to/README.md).

- [Architecture and patterns](./architecture.md): how the parts fit together, the rules between them, the life of a run, and the design patterns behind it. Start here for the whole picture.
- [The six-step loop](./the-six-step-loop.md): what validate, plan, apply, run, verify and ship each do, and why they are separate.
- [Determinism and the seed tree](./determinism-and-the-seed-tree.md): why the same bundle and seed give the same bytes, and what can still differ.
- [The FLUID seam](./the-seam.md): why a simulation refers to a contract and never restates it.
- [The evidence pack, and what it does not claim](./the-evidence-pack.md): what `ship` writes, what a recipient can check, and what it leaves unsaid.
- [Runs offline: your data and your model stay yours](./offline.md): where the network can be touched, how to keep it off, how object store targets treat instance metadata, and how CI checks it.
- [Where FLUX fits](./compared.md): what FLUX is good at, where it is not, how it compares with other synthetic data tools, and the roadmap.
