# ADR 0020: a run is graded from the profile it pins

## Status

Accepted 2026-11-21. Builds on the second amendment of
[ADR 0003](./0003-sdv-bsl-and-aggregate-only-rule.md) (what a run built from a differentially
private release carries) and [ADR 0019](./0019-one-privacy-budget-per-profile-release.md) (how the
release is accounted for). Amended by
[ADR 0032](./0032-a-generator-plugin-is-declared-graded-and-stored.md): `P4` is a differentially
private model from a generator plugin and no longer reserved, `P5` is added, and the run's grade is
the weakest of the profile's and every declared plugin's.

## Context

Borrow-before-build search: "NIST SP 800-226 differential privacy guarantees" (2026-11-21); fetched
https://csrc.nist.gov/pubs/sp/800/226/final (NIST Special Publication 800-226, Guidelines for
Evaluating Differential Privacy Guarantees, final, March 2025) and its PDF
(https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-226.pdf). It asks that a
guarantee be stated with its privacy parameters (epsilon and delta, or the value of rho under
zero-concentrated differential privacy), its unit of privacy (the definition of neighboring
datasets) and the way the loss was accounted for (the variant and how the parts compose), and it
warns that guarantees with different units or different delta values are not directly comparable.

Search: "ONS synthetic data spectrum" (2026-11-21); fetched
https://www.ons.gov.uk/methodology/methodologicalpublications/generalmethodology/onsworkingpaperseries/onsmethodologyworkingpaperseriesnumber16syntheticdatapilot
(Office for National Statistics, Methodology Working Paper 16, Synthetic data pilot). Its spectrum
has six levels, from structural data that keeps a format and data types to a replica that keeps joint
distributions and missingness, and the closer the synthetic data is to the original, the more
analytic value and the more disclosure risk it has. It orders data by closeness. It does not say
what stands behind a number.

Search: "EDPB anonymisation singling out linkability inference" (2026-11-21); fetched
https://www.edpb.europa.eu/system/files/2026-09/edpb_guidelines_202602_anonymisation_v1_en_0.pdf
(European Data Protection Board, Guidelines 02/2026 on anonymisation, version 1.0, adopted on 7 July
2026 for public consultation). Its framework tests whether data is anonymous with three criteria: no
record isolation (singling out), no linkage (linkability) and no inference. The three risks come from
the Article 29 Working Party, Opinion 05/2014 on Anonymisation Techniques (WP216), adopted on 10 April
2014. The guidelines were adopted for consultation, so both the criteria and the document may change.

FLUID 0.7.6's published schema, `$defs/sensitivityLevel`, names the twelve sensitivities a column may
carry (`none`, `internal`, `confidential`, `restricted`, `pii`, `phi`, `cleartext`, `treated`,
`anonymized`, `pseudonymized`, `tokenized`, `encrypted`) at `exposes[].contract.schema[].sensitivity`.

A reader of an evidence pack is told how close a run is to a profile (the scorecard) and, since
ADR 0003's second amendment, can be told a formal bound. What it lacks is one word for what the run
rests on, so that a data owner can set a floor on it and a reviewer can see it. NIST says what a
formal statement contains, the ONS spectrum says that closeness and risk rise together, and the EDPB
criteria say what a risk assessment tests. None says what stands behind a number.

## Decision

1. **Five patterns.** A run rests on one of:
   - **P0, assumed.** No profile was pinned and every parameter is declared by hand. The engine read
     no data. It cannot confirm that no data shaped the numbers a person typed, and it states no
     guarantee.
   - **P1, checked.** A profile of aggregates under the profiler's output checks (ADR 0003's first
     amendment). The checks carry no formal guarantee.
   - **P3, differentially private marginals.** A profile that states one release under differential
     privacy (ADR 0019) and holds only noisy statistics, as below.
   - **P4, a differentially private model** (amended by ADR 0032: this was reserved). A learned model
     from a generator plugin, whose bundle declares the account of its training.
   - **P5, a learned model with no privacy account** (added by ADR 0032). The weakest pattern.

   P2 is not defined. The numbers are stable, so a pattern keeps its number whatever is added, and
   P2 is unassigned.
2. **One grade for the run, from the pinned profile.**
   - No profile pinned: **P0**.
   - A profile pinned: **P3** only when it states an account of a release under differential
     privacy and declares `publicStructure` (the levels of its histograms, their bin edges and its
     `sampleSize` bands were chosen without the data, or are part of the release), and **every**
     statistic the engine can read from it is noisy under that account. That is: no `rowCount`, no
     column metric (counts, sums, minima, maxima, quantiles), no statistic without a `noise` block,
     and no pair, period, relationship, query or fact statistic, none of which can be released with
     noise in format 1-2-0.
   - Otherwise **P1**, and `EVIDENCE.md` lists why the profile is not P3.

   The bundle's register, its labels and its assumptions do not enter the grade. A pinned profile
   whose document is not at hand cannot be shown to be better than P1.
3. **What P3 rests on, said plainly.** The engine checks the account's arithmetic, that no share is
   below what its mechanism spends, and that the cliques agree. It does not check that noise was
   applied, and it does not check the declaration about the structure. Both are the profiler's
   statement, and the report says so: "Formal guarantee, as stated by the profile's release. The
   engine checks that the stated shares add up, not that noise was applied."
4. **`EVIDENCE.md` states it.** A section, "Privacy Grade", gives the run's grade and what it rests
   on. For a run graded P3 it states the guarantee in the terms NIST SP 800-226 asks for: the unit of
   privacy, the privacy loss (epsilon and delta, or rho), and the accounting, which is the definition
   and how the parts compose (ADR 0019), with what is not covered (ADR 0003's second amendment).
   The mandatory last section, "What Was Not Proven", then opens with "Beyond the guarantee stated
   under Privacy Grade, ...". A run that is not P3 says why, and a profile that carries an account
   but is not P3 says that the account gives the run no guarantee.
5. **What a grade is not.** It is a statement about what the run's numbers rest on. It is not a
   finding that the data is anonymous, which is a controller's assessment against criteria such as
   the EDPB's three, and it is not an ONS level: the spectrum orders data by closeness to the
   original, and the patterns say what stands behind a number. A bound of differential privacy limits
   what any analysis of the output, singling out, linkage and inference included, can learn about
   whether one unit was in the data, for that unit and loss only. Nothing here says more.
6. **Per-column grading is a later refinement.** A column's grade could follow the statistics that
   inform it (its own marginal, any clique or `given` it is in, the inputs of a derive), and a run
   with one exact statistic would then grade its other columns higher. It needs the number-to-statistic
   link to come from the profile and not from the bundle, and this release does not build it. Today the
   run has one grade, and each column's minimum is compared with it.

7. **Sensitivity sets a minimum grade.** A column's FLUID `sensitivity` sets the minimum grade a
   run needs to emit it: `pii`, `phi`, `restricted`, `pseudonymized` and `tokenized` require `P3` or
   stronger, and `P0` meets it too, since no data was read; `confidential` and `internal` require `P1`
   or stronger; every other label, and a column with no label, allows any pattern. Each labelled
   column's minimum is compared with the run's one grade. P0 is not a formal guarantee: it is the
   engine's statement that it read no data, and the table says so.
8. **Which gate.** Governance refuses a run in this engine today at `plan`, before a row is drawn and
   before anything is written: the sovereignty gate refuses a zone outside `allowedZones`
   (`FLX-4001`), and the skill bindings are checked against `agentPolicy` beside it. The minimum grade
   is one more check of that gate, in `flux_engine.governance.privacy`, called from `plan` after the
   parameter register is read against the pinned profile, and it refuses with a new code,
   **`FLX-4005`**, which names every labelled column below its minimum with the run's grade. A plan
   that pins a profile records the grade as `privacyGrade`. `apply` enforces the minimum too, from
   the recorded grade, so a fresh plan, `apply --run-id` and `replay` meet it, as does a plan made by
   an engine that did not check: a plan that pins a profile and records no grade is read as `P1`.
   `ship` is handed the profile and grades it again, and it takes the weaker of that and the recorded
   grade. A labelled column that is not a column of the run is refused, not skipped. The AI-call
   gate (`AIGate`, `FLX-4002`) is a different gate and is not touched.
9. **A bundle may only tighten the mapping.** A Simulation can raise a label's minimum under
   `extensions['com.agentics.flux/1'].privacy.minimumGrade`, for instance `confidential: P3`, or ask
   for `P4` or `P0`. A value below the built-in floor of its label is refused with `FLX-3105`, the
   namespace's own refusal, by `validate` and by every command that reads the bundle. A label that is
   not one of FLUID's and the unassigned `P2` are refused the same way. The declaration is a bundle
   edit, so `inputDigest` covers it.

## Consequences

- Every evidence pack states a pattern. A pack shipped before this section existed is unchanged.
- A profile that mixes noisy and exact statistics is P1 for the whole run, however much of it is
  noisy. It is conservative, and a profiler that wants P3 releases everything it publishes under
  noise, or publishes less.
- The grade reads the profile alone. A bundle cannot change it by what it declares about itself:
  relabelling the numbers that cite the profile as assumptions leaves the grade and the refusal as
  they were.
- A contract that labels a column `pii` and is generated from a profile that holds one exact
  statistic is refused until the profile is released wholly under noise, or the label is corrected.
  That is the gate working, and it is blunt: every column pays for one exact statistic, until
  per-column grading exists.
- Unpinning the profile yields P0, and P0 meets every floor. The engine cannot tell whether numbers
  written by hand came from data, so it states no guarantee for them and does not refuse them. A
  data owner who needs to know where hand-written numbers came from sets that outside the engine.
- A plan that pins a profile records one more key, `privacyGrade`, so the plan digest of such a plan
  differs from one made before. A plan that pins none is unchanged.

## Alternatives considered

- **One flag, formal or not.** Rejected: it hides the difference between a run that reads no data
  (P0) and one that reads a noisy release (P3), which a data owner may treat differently.
- **Grade each column from the register.** Rejected: the register records where a bundle says each
  number comes from, and a bundle writes it as it likes. Relabelling the numbers that cite the
  profile as assumptions graded the run P0 and cleared a floor it should not have. The grade is
  read from the one input a bundle cannot rewrite, the profile its Simulation pins by digest.
- **Refuse at `ship` only.** Rejected: a refusal after the run is drawn spends the work and leaves a
  run directory that was never allowed. `plan` refuses before any row is drawn, and `apply` and
  `ship` repeat it.
- **Let a bundle lower a floor.** Rejected: the declaration is read by the people a refusal would
  stop.
- **Map patterns to the ONS spectrum.** Rejected: the spectrum measures closeness to the original,
  which a scorecard already reports, and a mapping would imply a risk statement this engine does not
  make.
