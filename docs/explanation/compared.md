---
title: "Where FLUX fits"
description: "What FLUX is good at, where it is not, how it compares with other synthetic data tools, and where it is heading."
---

# Where FLUX fits

FLUX makes synthetic rows from numbers a bundle declares, under a FLUID data contract, and never by fitting a
model to real records. Most synthetic data tools take the opposite route: they train a model on real data and
sample from it. This page says what FLUX is good at, where it is not, and how it compares with the other tools,
so you can pick the right one for the job.

The facts here were checked on 25 November 2026 against FLUX 0.11.1 and the [sources](#sources) at the end, and
they are checked again at each FLUX release.

## What FLUX is good at

- **No real records.** Rows come from numbers a bundle declares, or from a profile of aggregates the data owner
  publishes. FLUX never fits a model on records ([ADR 0003](../adr/0003-sdv-bsl-and-aggregate-only-rule.md)), so
  there is nothing to memorise. One optional check, `--committed-slice`, still reads a file of records you pass.
- **The same bytes every time.** The same bundle and seed give the same rows and digests on Linux and macOS, which
  CI checks on every change, and `replay` refuses a run that misses the digest you give it. A generator plugin
  declares its own reproducibility. See [determinism and the seed tree](./determinism-and-the-seed-tree.md).
- **Evidence with every run.** Each shipped run carries `EVIDENCE.md`, which ends in "What Was Not Proven", a
  privacy grade that states what was checked ([ADR 0020](../adr/0020-privacy-grades.md)), and two unsigned in-toto
  statements. See [the evidence pack](./the-evidence-pack.md).
- **Governed by a contract.** A bundle names a FLUID data contract and pins its bytes, so the rows have the shape
  the contract expects. See [the FLUID seam](./the-seam.md).
- **Deep on time and relations.** Lifetimes, seasonal rates, versioned entities with as-of reads, snapshots and
  change events, counted children that can follow a parent column, linked rows, consumer-query calibration and
  sealed forecasts.
- **Offline by default.** No network is needed to produce results, there is no telemetry, and no AI model is
  embedded. CI blocks sockets, runs a journey with no network, and scans the source for network and model clients.
  See [runs offline](./offline.md).

## What FLUX is not good at

- **Realism on complex correlations.** Trained models learn joint patterns of any order from the data. FLUX
  reproduces only the dependence a profile publishes or a bundle declares: noisy multi-column marginals from a
  differentially private profile, number and timestamp columns through a Gaussian copula of published rank
  correlations, a column drawn given another, and children per parent that follow a parent column. A pattern that
  nothing publishes or declares is not reproduced.
- **Getting started.** Point a trained-model tool at a table and it learns. FLUX needs a contract and a bundle,
  written by hand or drafted by `fluxsim init` from a profile
  ([write a bundle from a profile](../how-to/write-a-bundle-from-a-profile.md)).
- **Breadth.** No GANs, VAEs, diffusion models or LLM text generation in the core. A learned generator plugin,
  `flux-learned`, is designed but deferred.
- **Many-core scaling.** A run is quick on one core, but extra workers stop paying off: the 536,500-row links
  world takes 3.64 seconds on one job and 1.09 on 16, and on 2.68 million rows 16 jobs are no faster than 12.
  About 0.6 seconds of every run does not split across workers. The other tools have not been benchmarked on the
  same worlds. The [speed roadmap](#roadmap-faster-on-many-cores) lists the fixes.
- **Not evidence, and not a privacy tool by itself.** The rows reproduce the scenario you describe, not the real
  world. Only a P3 run carries a formal privacy guarantee: the profiler adds the noise, and FLUX checks the stated
  budget.
- **Unfinished parts.** A bundle asking for `fidelity: stochastic` is refused, the agentic tier is not built, and
  generator plugins are not sandboxed. The 0.11.0 known issues, which the 0.11.1 fixes do not address, include a
  bundle that `validate` passes but `plan` refuses.

## The comparison

As published on 25 November 2026, from each project's own documentation and, for commercial products, the
vendor's. **Reproducible** means documented to give identical output for the same inputs and seed. **Claimed**
means advertised but not documented. **Unverified** means no fetched source says either way. None of the tools was
run offline for this page. The notes under the table give the evidence behind each cell.

| Tool | Approach | Licence | Needs real records | Reproducible | Formal privacy (DP) | Multi-table | Time and sequence | Audit evidence | Runs offline |
|---|---|---|---|---|---|---|---|---|---|
| **FLUX** 0.11.1 | Declared rules and published aggregates | Apache-2.0 | never to generate | yes | via profile | yes | yes | yes | yes |
| **SDV** 2.2.0 | Copula, GAN and deep models | BUSL-1.1 | to train | resampling | no | yes | yes | quality score | yes |
| **synthcity** 0.2.12 | GANs, VAEs, flows, diffusion | Apache-2.0 | to train | unverified | some models | not stated | yes | metrics | yes |
| **MOSTLY AI SDK** 6.2.1 | Deep model (TabularARGN) | Apache-2.0 | to train | unverified | option | yes | yes | reports | local mode |
| **NeMo Data Designer** 1.0.1 | LLM with samplers | Apache-2.0 | no | not documented | no | not stated | not stated | validators | not by default |
| **NeMo Safe Synthesizer** 0.2.0 | Fine-tuned LLM | Apache-2.0 | to train | unverified | opt-in | not stated | experimental | report | local GPU |
| **Gretel** 0.22.20, archived | LSTM and GANs | source-available | to train | unverified | option | unverified | yes | unverified | unverified |
| **fg-data-synthetic** 2.1.0 | GANs and Gaussian mixtures | MIT | to train | unverified | none documented | not stated | yes | unverified | yes |
| **DataSynthesizer** 0.1.13 | Bayesian network | MIT | to describe | seeded | noise | no | no | none | yes |
| **SmartNoise Synth** 1.0.8 | DP marginals and GANs | MIT | to train | unverified | epsilon | not stated | no | none | yes |
| **private-pgm** mbi 2.1.0 | Model from noisy marginals | Apache-2.0 | noisy marginals | unverified | caller's job | not stated | no | none | yes |
| **REaLTabFormer** 0.2.4 | GPT-2 transformer | MIT | to train | unverified | none documented | two tables | not stated | none | yes |
| **GReaT** 0.0.14 | Fine-tuned small LLM | MIT | to train | mock only | no | not stated | not stated | metrics | yes |
| **Faker** 40.63.0 | Field providers | MIT | no | same version | n/a | no | date fields | none | yes |
| **Mimesis** 22.2.0 | Field providers and schemas | MIT | no | seeded | n/a | yes | date fields | none | yes |
| **Hypothesis** 6.170.17 | Test-data strategies | MPL-2.0 | no | per version | n/a | composite | date fields | counterexamples | yes |
| **dbldatagen** 0.4.0.post1 | Spark rules | Databricks License | optional | seeded | n/a | yes | date ranges | none | needs Spark |
| **Synth** 0.6.9, dormant | Declarative schema | Apache-2.0 | on import | seeded | no | yes | series | none | yes |
| **Snaplet Seed** 0.98.0 | Database seeding | MIT | no | claimed | n/a | yes | unverified | none | mostly |
| **Mockaroo** hosted | Field types and formulas | Proprietary | no | unverified | n/a | datasets | date fields | unverified | claimed |
| **Tonic** | Masking, subsetting, generators | Proprietary | most modes | seeded | some generators | yes | unverified | privacy report | self-hosted |
| **Synthesized** 1.187 | Masking and generation | Proprietary | optional | seeded | unverified | yes | unverified | audit log | documented |
| **SAS Data Maker** | Bayesian network, MST, SMOTE | Proprietary | to train | unverified | listed | claimed | claimed | unverified | unverified |
| **K2view** TDM 10.1.1 | Masking, rules, trained model | Proprietary | AI mode | unverified | unverified | claimed | unverified | reports | unverified |
| **MOSTLY AI platform** 5.10.0 | Deep models per table | Proprietary | to train | unverified | Opacus | yes | sequences | QA reports | claimed |

<details>
<summary>Notes behind each cell</summary>

- **FLUX** (flux-sim-engine 0.11.1)
  - Approach: Declared rules and statistics from a contract and an aggregate profile; joint patterns from a DP profile's noisy multi-column marginals through a junction tree, and number and timestamp columns together through a Gaussian copula of published rank correlations
  - Needs real records: Generation reads only the bundle or an aggregate profile; fitting on records is ruled out (ADR 0003). An optional check, `--committed-slice`, reads a file of records you pass; ADR 0003 moves it into the profiler.
  - Reproducible: Every run is fingerprinted, and `replay` refuses a run that does not reach the digest you give it. A generator plugin declares its own reproducibility, and a plugin run that cannot be generated again is refused.
  - Formal privacy (DP): Grade P3 only when the profile states one DP release, declares its public structure and holds only noisy statistics, with a few more conditions (ADR 0020); anything else grades P1. The profiler adds the noise, and its unit of privacy, such as a customer, can cover that unit's child rows too. FLUX checks the stated budget.
  - Multi-table: Keys, links, and counted children whose number can follow a parent column; a child reads its parent's values through links and conditions. Patterns across tables come only from what the profile publishes or the bundle declares.
  - Time and sequence: Lifetimes, rates, versions, as-of reads, snapshots and events.
  - Audit evidence: EVIDENCE.md with "What Was Not Proven", two unsigned in-toto statements, a privacy grade.
  - Runs offline: Network is used only for destinations you configure. CI blocks sockets in tests, runs a journey with no network, and scans the source.
- **SDV** (2.2.0; CTGAN 0.13.0 is the latest)
  - Approach: Trained models: Gaussian copula, CTGAN and other deep models
  - Licence: Source-available; each version becomes MIT after 4 years.
  - Needs real records: Train once on real data, then sample from the saved model.
  - Reproducible: A fitted synthesizer repeats its output after `reset_sampling()`; fitting is not documented as deterministic.
  - Formal privacy (DP): None in the open-source package; a paid Enterprise bundle adds DP (DPGCSynthesizer).
- **synthcity** (0.2.12)
  - Approach: Many trained models: GANs, VAEs, flows, diffusion, PrivBayes
  - Formal privacy (DP): DP-GAN, PATE-GAN, PrivBayes; accounting detail unverified.
  - Time and sequence: Time series and survival data.
  - Audit evidence: Fidelity and privacy metric suite.
- **MOSTLY AI SDK** (mostlyai 6.2.1)
  - Approach: Trained deep model (TabularARGN); an LSTM, or optionally a fine-tuned language model, for text
  - Formal privacy (DP): DP training listed; details unverified.
  - Audit evidence: Fidelity and privacy HTML reports.
  - Runs offline: Local is the default; an air-gapped install is documented.
- **NeMo Data Designer** (NVIDIA, data-designer 1.0.1)
  - Approach: LLM and sampler pipeline with column dependencies
  - Needs real records: Seed data optional.
  - Audit evidence: Validators, LLM judges and a column-statistics report (HTML or SVG).
  - Runs offline: Default providers are hosted APIs; telemetry on by default.
- **NeMo Safe Synthesizer** (NVIDIA, nemo-safe-synthesizer 0.2.0)
  - Approach: Fine-tunes an LLM on tabular data after PII replacement
  - Formal privacy (DP): DP-SGD, off by default; epsilon defaults to 8, delta is set automatically, Rényi DP accounting.
  - Time and sequence: Event sequences and time series; its docs mark temporal support experimental.
  - Audit evidence: HTML report plus quality and privacy scores.
  - Runs offline: NVIDIA A100 class or larger; local vLLM by default.
- **Gretel** (gretel-synthetics 0.22.20, archived)
  - Approach: LSTM, ACTGAN, and a DoppelGANger time-series GAN
  - Licence: Repository archived 18 Feb 2026. NVIDIA reportedly acquired the company in March 2025, and gretel.ai now redirects to NVIDIA.
  - Time and sequence: Time-series DGAN (DoppelGANger).
- **fg-data-synthetic** (2.1.0; formerly ydata-synthetic)
  - Approach: GANs, Gaussian mixtures, TimeGAN, DoppelGANger
  - Licence: The LICENSE file and GitHub say MIT; the PyPI classifier says GPLv3+.
- **DataSynthesizer** (0.1.13)
  - Approach: Describes the data, then samples (independent or Bayesian network)
  - Reproducible: A seed parameter, 0 by default, seeds Python's and NumPy's generators; identical output is not promised.
  - Formal privacy (DP): Laplace noise under an epsilon (0.1 by default; 0 turns it off), documented in its code rather than its README.
- **SmartNoise Synth** (OpenDP, 1.0.8)
  - Approach: Marginal (AIM, MST, MWEM), neural (DP-CTGAN, PATE-CTGAN and others) and hybrid DP synthesizers
  - Formal privacy (DP): Takes an epsilon; its docs show a spent-budget tracker; the composition method is not described.
- **private-pgm** (mbi 2.1.0 on PyPI)
  - Approach: Graphical models estimated from noisy marginals; a building block
  - Needs real records: Closest in spirit to FLUX: works from measurements, not rows.
- **REaLTabFormer** (0.2.4)
  - Approach: GPT-2; Seq2Seq for parent and child tables
- **GReaT** (be_great 0.0.14)
  - Approach: Small LLM fine-tuned on rows written as text
  - Needs real records: `fit()` trains on real rows; `mock()` needs none.
  - Reproducible: Only `mock()` documents a seed.
  - Formal privacy (DP): Distance and membership metrics.
  - Runs offline: Downloads models.
- **Faker** (40.63.0)
  - Approach: Per-field providers
  - Reproducible: Its docs warn output may change across patch versions.
- **Mimesis** (22.2.0)
  - Approach: Per-field providers and schemas
  - Reproducible: Its docs warn that some provider methods cannot be seeded.
  - Multi-table: Related schemas with foreign keys (SchemaBuilder, since 20.0.0).
- **Hypothesis** (6.170.17)
  - Approach: Property-based test data strategies
  - Reproducible: Replay blobs are tied to one version; derandomized runs hold until Hypothesis, Python or the test changes.
- **dbldatagen** (Databricks Labs, 0.4.0.post1)
  - Approach: Spark data generator
  - Licence: Use is limited to Databricks Services; not OSI-approved.
  - Needs real records: An experimental mode generates from an existing dataset or schema.
  - Reproducible: "Repeatable, predictable data".
  - Multi-table: Keys, CDC, merge scenarios.
- **Synth** (v0.6.9, 2022, dormant)
  - Approach: Declarative schema (Rust CLI)
  - Needs real records: Can infer distributions from a real database.
  - Reproducible: Default seed 0.
  - Time and sequence: Incrementing, Poisson and cyclical series generators.
  - Runs offline: Telemetry is on by default; `synth telemetry disable` turns it off.
- **Snaplet Seed** (0.98.0, 2024; repo under supabase-community)
  - Approach: TypeScript seeding for databases
  - Licence: The repository LICENSE says MIT; the npm package metadata says FSL-1.1-MIT.
  - Reproducible: README says "fully deterministic".
  - Runs offline: Its AI feature needs a hosted API key.
- **Mockaroo** (hosted; private-cloud image)
  - Approach: Schema and field types, with formulas and uploaded datasets; web and API
  - Needs real records: Its generators need none. It also lists a De-Identify feature, whose page could not be read.
  - Reproducible: No seed option found in its docs.
  - Multi-table: Keys shared through uploaded datasets; no relational generation documented.
  - Time and sequence: Date formulas only.
  - Runs offline: A Docker image for private clouds is advertised; setup docs not found.
- **Tonic** (Structural, Textual, Fabricate)
  - Approach: Masking and de-identification with column generators, and subsetting; Fabricate drafts generation rules from prompts with an LLM
  - Needs real records: Structural transforms source data; its data-free generators need none.
  - Reproducible: A statistics seed gives consistent values across runs, for generators with consistency enabled.
  - Formal privacy (DP): A DP option on the categorical and continuous generators, off by default (epsilon 1, delta 1/(10n)); data-free generators always apply DP. No privacy accounting described.
  - Multi-table: Subsetting keeps foreign keys, including virtual ones.
  - Audit evidence: A privacy report per job (CSV and PDF), an audit-event API for user access and permission changes, and audit logs in Fabricate and Textual.
  - Runs offline: Self-hosted on Docker or Kubernetes; air-gapped use not documented.
- **Synthesized** (platform 1.187)
  - Approach: Masking, subsetting, and generation from learned column distributions or from a schema and parameters
  - Needs real records: Learns from input data, or generates from a schema and user parameters.
  - Reproducible: A global seed gives the same output for the same input, configuration and version.
  - Formal privacy (DP): Not found in its docs.
  - Multi-table: Foreign keys point to existing parent rows.
  - Audit evidence: Audit and activity logs; SOC 2 Type 2 in progress, with the report expected in December 2026.
  - Runs offline: A documented offline installation; the licence is checked offline.
- **SAS Data Maker** (includes Hazy technology, acquired Nov 2024)
  - Approach: Trained generators: Bayesian network, MST, SMOTE
  - Needs real records: Its listed methods are all fitted on source data.
  - Formal privacy (DP): Differential privacy is listed among its methods; mechanism and accounting not found.
  - Multi-table: Advertised in its November 2025 launch release: multitable data.
  - Time and sequence: Advertised in its November 2025 launch release: time-series data.
  - Runs offline: Its product page describes it on Microsoft Azure; no offline install found.
- **K2view** (TDM 10.1.1)
  - Approach: Masking and entity-based subsetting; rule-based generation, and AI generation from a model you train on source entities
  - Needs real records: The AI mode trains on source entities; the rule mode's needs are not stated.
  - Multi-table: Cross-application integrity is described; the mechanism is not.
  - Audit evidence: Masking policy and execution reports, described as audit-ready, and audit-level security logging; both are off by default.
- **MOSTLY AI platform** (5.10.0, Jan 2026; Syntho bought the MOSTLY AI trademark and related assets in June 2026)
  - Approach: Trained generative models per table; language models for text
  - Needs real records: Trains on source tables.
  - Formal privacy (DP): Training uses the Opacus library, with a maximum epsilon as a stopping threshold and a delta (default 1e-5); the training log shows epsilon and delta.
  - Audit evidence: Model and data QA reports with distance-to-closest-record and identical-match checks, which are not DP.
  - Runs offline: Its privacy and security page advertises on-premises, air-gapped and private-cloud installs; the install docs read for this page do not mention air-gapped use.

</details>

## Roadmap: faster on many cores

A run is already quick on one core. What holds it back is the part of a run that does not split across workers,
so extra jobs stop paying off, and the smaller the world, the sooner. Measured on 0.11.0, on an Apple M4 Max with
16 CPUs, best of 2, in wall seconds:

| World | Rows | 1 job | 16 jobs | Faster by |
|---|--:|--:|--:|--:|
| links | 536,500 | 3.64 | 1.09 | 3.3× |
| large world | 416,000 | 3.12 | 0.85 | 3.7× |
| temporal | 181,000 | 1.27 | 0.66 | 1.9× |
| counts | 145,800 | 1.73 | 0.88 | 2.0× |
| history | 54,954 | 1.40 | 1.04 | 1.3× |

The 0.11.0 changelog names the causes and logs them for the next release. The aim is about 8 times faster at 16
jobs, with every run still byte for byte what it was.

- **Shrink the serial phases.** Start-up, load, simulate and digest take about 0.6 seconds on one process whatever
  the job count, and generation itself stops gaining past 8 to 12 workers.
- **Stop rebuilding layouts in every worker.** A shaped counted layout costs about 390 MB per million parents
  (measured on 0.10.0) and, like a windowed table, is built again in each worker.
- **Cut the run overhead.** At 16 jobs, `fluxsim run` spends about 0.4 seconds more than `fluxsim plan` on the
  same world.
- **Plan time rules faster.** `lifetimes` and `within` cost about 2 ms per child row at plan time (measured on
  0.10.0).
- **Verify large runs faster.** `verify` scores rows grouped by bins one row at a time.

Available today: `apply --shard I/N` makes the I-th of N shares of every emit's rows, and the shares joined in
order are the bytes of the run's emits, so a large world can be split across machines. A share carries no run
digest or receipt of its own. See [size and split a run](../how-to/size-and-split-a-run.md).

Not planned: a compiled accelerator. A Rust line renderer was built and measured. Rendering is 12.4 % of a run,
and fed the engine's rows, the Rust renderer took 1.788 µs a row against the engine's 1.218. It is revisited only
if the engine comes to hold columns as arrays
([ADR 0010](../adr/0010-faster-generation-through-a-second-kernel.md)).

## Roadmap: from a static dataset to a living clone

This part is a proposal. Every tool in the table above produces a dataset: a snapshot that stops the moment it is
written. The proposal is that FLUX goes further and runs a **living clone** of the specs: the contract's world
kept moving on a clock, its entities ageing, changing state and producing events, reacting to scenarios, and
replayable to the byte.

FLUX already holds most of the pieces. Lifetimes, rates, versions and as-of reads describe how entities move
through time; overlays describe scenarios; sealed forecasts and outcome scoring describe how a prediction is
checked against what happened; the seed tree makes every draw replayable. What is missing is the clock that keeps
the world running.

A bundle says which mode its clone runs in. The deterministic mode needs nothing beyond FLUX. The model-assisted
mode adds a small language model (SLM) of your own for more fidelity. The two share the clock and the contract;
the model-assisted mode is a separate package, and removing it leaves the deterministic mode untouched.

### Deterministic mode: the highest sovereignty

Rules and declared statistics only, as every FLUX run without a generator plugin is today
(`fidelity: deterministic`).

- No model and no network. It keeps FLUX's offline guarantee, which CI checks today by running FLUX with the
  network removed.
- A run is fully determined by its bundle and seed: rerun it on any system CI checks and the bytes are identical.
- Nothing leaves your machine unless you name a sink that writes elsewhere, and nothing in a run depends on
  anyone's model.

### Model-assisted mode: more fidelity, opt-in

Your SLM proposes what rules can only approximate; the engine keeps every proposal within the bounds it enforces,
and only the engine changes the world's state (`fidelity: agentic`).

- The model is yours, and pinned. FLUX ships no model and calls no hosted service unless you name one; run the
  model locally and the clone stays offline.
- Prompts carry the contract and the clone's own synthetic state, never real rows.
- Every call is to pass a gate with a hard token budget, and its decision is to be recorded in
  `decisions.ndjson`, which the run's digests already cover. The gate and the log exist today; no run calls them
  yet.
- Each answer is to be stored under the call's seed path, the model's pin, the prompt's digest and the policy's
  digest, so a replay reads it back without calling the model. This cache is designed
  ([ADR 0004](../adr/0004-concordia-tinytroupe-adapt-the-pattern.md)), not built. Without recorded answers a rerun
  can differ: a model can answer the same prompt differently from one call to the next, even at temperature 0.

| What you can rely on | Deterministic mode | Model-assisted mode |
|---|---|---|
| A model in the loop | None | Your SLM, pinned |
| Network | Never needed | Not needed when the model runs locally |
| Rerun from bundle and seed alone | Byte-identical | Can differ, because model output varies |
| Replay of a recorded run | Byte-identical | Designed to be byte-identical, from the recorded answers |
| What can change the world's state | Only the bundle's rules and statistics | Only the engine, which may accept a model's proposal within the contract's bounds |
| Where a run may execute | The bundle's sovereignty zones (`allowedZones`), enforced today | The same zones |

A run that declares a zone outside its `allowedZones` is blocked or audited, as the bundle says.

Status: deterministic runs ship today, and the clock is proposed. The model-assisted mode is designed
([ADR 0004](../adr/0004-concordia-tinytroupe-adapt-the-pattern.md)) and partly in place: the core already has the
propose-only model port, the gate with its token budgets, the decision log file and the sovereignty zones, but no
run calls the gate yet and the answer cache is not built. No model adapter ships beyond a development stub, and
today's engine refuses `fidelity: agentic` unless the separate package is installed.

Borrowed patterns: Temporal replays a workflow by reading recorded results instead of calling out again;
TinyTroupe caches model calls and simulation state so a rerun can skip calls it has already made; Concordia has a
game master entity that resolves the players' actions, a role the FLUX engine plays itself, without a model.

The proposed steps:

1. **Deterministic mode: the clock.** Add integer ticks to today's time model, so a clone emits events and state
   changes continuously, ordered by time and then by id, and replays from its bundle and seed.
2. **Deterministic mode: scenarios on a running clone.** Apply overlays while the clone runs (a price change, an
   outage, a new product), and score its forecasts against what it then produces.
3. **Model-assisted mode: your SLM proposes, the engine decides.** Ship the separate package with an adapter for a
   model you run yourself. Each proposal stays within bounds the engine enforces, and each call is budgeted, gated
   and recorded.
4. **Model-assisted mode: agents with memory and goals.** Let entities act as agents with their own memory and
   goals, in the style of Concordia, under the same rules: proposals only, a hard token budget, and a replay that
   reads the recorded answers.

Risks, stated up front:

- Model output can change with server load, because inference servers batch requests differently; the recorded
  answers, not pinning the model, are the guarantee.
- Cost: calling a model at every step is expensive. The rule-only mode costs nothing extra, and the SLM mode calls
  the model only for the decisions rules cannot make well.
- Privacy: a hosted model would send contract text off the machine, so hosted backends stay off unless named.

## Roadmap: learned generators, kept inside your boundary

For data whose patterns are too rich to declare by hand, FLUX plans an optional plugin, `flux-learned`: a
separate package that trains inside the data owner's own environment under differential privacy. Installing the
engine installs none of it.

- **Kept out of the engine.** The engine never fits a model to records and takes no dependency on one; the plugin
  is installed beside it.
- **Declared and stored.** Like any generator plugin, it declares its grade, its privacy account, the digest of
  its model and how reproducible it is
  ([ADR 0032](../adr/0032-a-generator-plugin-is-declared-graded-and-stored.md)).
- **Graded honestly.** A differentially private model from a plugin is grade P4, and a learned model with no
  privacy account is P5 ([ADR 0020](../adr/0020-privacy-grades.md)). The engine's own guarantees do not reach the
  rows it draws, which are a function of the records; what the plugin says about its output is its own.

Status: designed and deferred; the build is not yet scheduled
([ADR 0003](../adr/0003-sdv-bsl-and-aggregate-only-rule.md), amended 23 November 2026).

## Sources

Fetched on 23 November 2026; FLUX and the tool rows re-checked on 25 November 2026.

- FLUX: README, docs/explanation (determinism, the evidence pack, offline), ADR 0003, ADR 0010, ADR 0014, ADR 0020, ADR 0021, ADR 0022, ADR 0032, ADR 0035, ADR 0036, ADR 0037, docs/spec/PROFILE-SPEC.md, docs/spec/PROFILE-REQUEST-SPEC.md, docs/reference/cli/replay.md, docs/reference/cli/apply.md, CHANGELOG, at tag v0.11.1 (579df3a)
- [SDV sampling](https://docs.sdv.dev/sdv/single-table-data/sampling/sample-realistic-data), [SDV DP bundle](https://docs.sdv.dev/sdv/explore/sdv-bundles/differential-privacy), [github.com/sdv-dev/SDV](https://github.com/sdv-dev/SDV), [pypi sdv](https://pypi.org/pypi/sdv/json), [pypi ctgan](https://pypi.org/pypi/ctgan/json)
- [github.com/vanderschaarlab/synthcity](https://github.com/vanderschaarlab/synthcity)
- [github.com/mostly-ai/mostlyai](https://github.com/mostly-ai/mostlyai), [pypi mostlyai](https://pypi.org/pypi/mostlyai/json)
- [github.com/gretelai/gretel-synthetics](https://github.com/gretelai/gretel-synthetics), [TechCrunch, 19 Mar 2025](https://techcrunch.com/2025/03/19/nvidia-reportedly-acquires-synthetic-data-startup-gretel)
- [github.com/NVIDIA-NeMo/DataDesigner](https://github.com/NVIDIA-NeMo/DataDesigner), [github.com/NVIDIA-NeMo/Safe-Synthesizer](https://github.com/NVIDIA-NeMo/Safe-Synthesizer), [Data Designer analysis report](https://github.com/NVIDIA-NeMo/DataDesigner/blob/main/packages/data-designer-config/src/data_designer/config/analysis/utils/reporting.py), [Safe Synthesizer data synthesis](https://nvidia-nemo.github.io/Safe-Synthesizer/latest/product-overview/data_synthesis/)
- [github.com/Data-Centric-AI-Community/fg-data-synthetic](https://github.com/Data-Centric-AI-Community/fg-data-synthetic), [pypi fg-data-synthetic](https://pypi.org/pypi/fg-data-synthetic/json)
- [github.com/DataResponsibly/DataSynthesizer](https://github.com/DataResponsibly/DataSynthesizer), [DataSynthesizer 0.1.13 on PyPI](https://pypi.org/project/DataSynthesizer/0.1.13/) (DataDescriber.py)
- [github.com/opendp/smartnoise-sdk](https://github.com/opendp/smartnoise-sdk)
- [github.com/ryan112358/mbi](https://github.com/ryan112358/mbi) (formerly private-pgm), [pypi mbi](https://pypi.org/pypi/mbi/json)
- [github.com/worldbank/REaLTabFormer](https://github.com/worldbank/REaLTabFormer)
- [github.com/tabularis-ai/be_great](https://github.com/tabularis-ai/be_great)
- [Faker docs](https://faker.readthedocs.io/en/master/), [github.com/lk-geimfari/mimesis](https://github.com/lk-geimfari/mimesis) ([relational data](https://github.com/lk-geimfari/mimesis/blob/master/docs/relational.rst))
- [github.com/shuttle-hq/synth](https://github.com/shuttle-hq/synth) ([series](https://github.com/shuttle-hq/synth/blob/master/docs/docs/content/series.md)), [Hypothesis API](https://hypothesis.readthedocs.io/en/latest/reference/api.html)
- [github.com/databrickslabs/dbldatagen](https://github.com/databrickslabs/dbldatagen) ([generating from existing data](https://databrickslabs.github.io/dbldatagen/public_docs/generating_from_existing_data.html)),[github.com/supabase-community/seed](https://github.com/supabase-community/seed), [npm @snaplet/seed](https://registry.npmjs.org/@snaplet/seed), [Mockaroo help](https://www.mockaroo.com/help)
- Tonic: [differential privacy](https://docs.tonic.ai/app/generation/generators/generator-characteristics/differential-privacy), [privacy report](https://docs.tonic.ai/app/generation/privacy-report), [subsetting](https://docs.tonic.ai/app/generation/subsetting/subsetting-about), [self-hosted deployment](https://docs.tonic.ai/app/admin/on-premise-deployment), [statistics seed](https://docs.tonic.ai/app/workspace/workspace-configuration-settings/advanced-overrides), [Fabricate](https://docs.tonic.ai/fabricate/about-fabricate/data-agent-about), [audit-event API](https://docs.tonic.ai/app/admin/tonic-monitoring-logging/audit-event-api-endpoint), release notes for [Fabricate](https://www.tonic.ai/product-release-notes/fabricate) and [Textual](https://www.tonic.ai/product-release-notes/textual)
- Synthesized: [how generation works](https://docs.synthesized.io/tdk/latest/user_guide/030_core_concepts/how_generation_works), [offline installation](https://docs.synthesized.io/tdk/latest/user_guide/050_deployment_operations/production_setup/offline), [security and compliance](https://docs.synthesized.io/tdk/latest/user_guide/060_security_compliance/), [FAQ](https://docs.synthesized.io/tdk/latest/faq/synthesized-platform), [compliance](https://docs.synthesized.io/tdk/latest/user_guide/060_security_compliance/compliance), [Governor changelog](https://docs.synthesized.io/tdk/latest/changelog/governor)
- SAS Data Maker: [support page](https://support.sas.com/en/software/data-maker-support.html), [product page](https://www.sas.com/en_ie/software/data-maker.html), [Hazy acquisition, Nov 2024](https://www.sas.com/en_gb/news/press-releases/2024/november/hazy-syntheticdata.html), [Microsoft Marketplace launch, Nov 2025](https://www.sas.com/en_ca/news/press-releases/2025/november/data-maker-launches-on-microsoft-marketplace.html)
- K2view: [TDM overview](https://support.k2view.com/Academy/articles/TDM/tdm_overview/01_tdm_overview.html), [synthetic data generation](https://support.k2view.com/Academy/articles/TDM/tdm_gui/19_task_synthetic_data_generation.html), [TDM 10.1.1 release notes](https://support.k2view.com/Academy/Release_Notes/TDM-V10.1/TDM_Release_Notes_V10_1_1.pdf.html), [TDM 10.1 release notes](https://support.k2view.com/Academy/Release_Notes/TDM-V10.1/TDM_Release_Notes_V10_1.pdf)
- MOSTLY AI platform: [differential privacy](https://mostly.ai/docs/generators/configure/enable-differential-privacy), [quality reports](https://mostly.ai/docs/generators/evaluate-quality), [release notes](https://mostly.ai/docs/whats-new), [privacy and security](https://mostly.ai/privacy-and-security), [Syntho acquires the MOSTLY AI brand](https://www.syntho.ai/syntho-acquires-mostly-ai-trademark-and-related-assets/)
- NVIDIA: [NeMo data synthesis](https://docs.nvidia.com/nemo-platform/documentation/synthesize-safe-data/about/data-synthesis), [Data Designer docs](https://docs.nvidia.com/nemo/datadesigner/getting-started/welcome)
- [github.com/google-deepmind/concordia](https://github.com/google-deepmind/concordia), [pypi gdm-concordia](https://pypi.org/pypi/gdm-concordia/json)
- [github.com/microsoft/TinyTroupe](https://github.com/microsoft/TinyTroupe), [Temporal event history](https://docs.temporal.io/encyclopedia/event-history/event-history-python), [Defeating Nondeterminism in LLM Inference](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/) (Thinking Machines, 10 Sep 2025)
- FLUX: ADR 0004, `flux_engine/governance`, `flux_engine/api/agentic.py` and `flux_engine/stages/pipeline.py` at tag v0.11.1 (579df3a)
- [github.com/mesa/mesa](https://github.com/mesa/mesa), [SimPy time and scheduling](https://simpy.readthedocs.io/en/latest/topical_guides/time_and_scheduling.html)
- [github.com/AgentTorch/AgentTorch](https://github.com/AgentTorch/AgentTorch), [github.com/tsinghua-fib-lab/AgentSociety](https://github.com/tsinghua-fib-lab/AgentSociety), [github.com/joonspk-research/generative_agents](https://github.com/joonspk-research/generative_agents)
