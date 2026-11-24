# Privacy grades

Read each statement here from the installed engine's own text where it prints one: `fluxsim explain FLX-4005` prints the floors a label sets. The design is in [ADR 0020](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/adr/0020-privacy-grades.md).

## The patterns

- P0, assumed: no profile was pinned and every parameter is declared by hand. The engine read no data. It cannot confirm that no data shaped the numbers a person typed, and it states no guarantee.
- P1, checked: a profile of aggregates under the profiler's output checks. The checks carry no formal guarantee.
- P3, differentially private marginals: a profile that states one release under differential privacy, declares its structure public, and holds only noisy statistics.
- P4, differentially private model: a learned model from a generator plugin whose bundle declares the account of its training under differential privacy. The engine checks the account's form, not how the model was trained.
- P5, learned model with no privacy account: a learned model from a generator plugin whose privacy rests on empirical checks alone. It is the weakest pattern and is below every sensitivity floor.

P2 is not defined. A pattern keeps its number whatever is added. A run's pattern is the weakest of those it rests on, so a run with a plugin of pattern P5 is graded P5 whatever its profile says.

## How a run is graded

- No profile pinned: P0.
- A profile pinned: P3 only when it states an account of a release under differential privacy, declares `publicStructure`, and every statistic the engine reads from it is noisy under that account. A row count, a column metric, a statistic without a `noise` block, a period, a query or a fact statistic makes it not P3. A column pair is noisy only when every statistic in it carries a `noise` block: a Pearson `correlation/1` never does, so it makes the profile not P3, and a rank correlation (`kendallTau/1`, `spearmanRho/1`, profile format 1-4-0) can be noisy. A relationship is noisy when every statistic in it carries a `noise` block, so one with a mean or a quantile is not.
- Otherwise P1, and `EVIDENCE.md` lists why the profile is not P3.

The grade is read from the pinned profile and from every generator plugin the bundle declares, and the run takes the weakest of them. The bundle's register, its labels and its assumptions do not enter it, so a bundle cannot change its own grade by what it says about itself. A profile that mixes noisy and exact statistics is P1 for the whole run. The run has one grade, and each labelled column's floor is compared with it.

P3 rests on the profile's statement. The engine checks that the stated shares add up, that each share is at least what its mechanism spends for the statistic's sensitivity, and that the cliques agree. It does not check that noise was applied, that a float-safe sampler drew it, or the declaration that the structure is public. For a profile of format 1-4-0 that declaration covers the levels and bin edges of its histograms and cliques, the bins of every `groupByBins`, the group keys of its grouped noisy statistics, the `sampleSize` bands, every `rowFloor` and every `maxChildren`.

## When the unit follows child rows

A profile of format 1-4-0 may state a unit of privacy that is one row of a table with the child rows it takes with it (`privacyUnit.follows`, each followed relationship with a public cap `maxChildren`). `EVIDENCE.md` then states the unit with what it follows and names the tables the unit does not reach, whose rows the guarantee treats as fixed. A labelled column of such a table counts as P1 for its minimum grade, so a run that draws on a `pii` or `restricted` column of a table the unit does not follow is not held to P3. Say so to the user when the evidence names fixed tables.

## Generator plugins

A bundle may declare a generator plugin (`fluxsim plugins` lists the installed ones). A plugin is trusted code that runs with the user's rights and is not sandboxed by FLUX (allow only plugins you trust), and a command imports it only when the command line allows it by name. Never pass `--allow-generator` on your own judgement: only the person running `fluxsim` decides that, so stop and ask them, naming the plugin. The grade of a run that uses a plugin is stated in [ADR 0032](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/adr/0032-a-generator-plugin-is-declared-graded-and-stored.md) section 4.

## Where the gate refuses

The governance gate compares each labelled column's floor with the run's grade. `plan` refuses with FLX-4005 before a row is drawn and before anything is written; `apply` and `ship` refuse again, from the recorded grade and the profile. A labelled column that is not a column of the run is refused, not skipped.

## Raising a floor in the Simulation

A bundle may only tighten the mapping. A Simulation can raise a label's floor under the `privacy` key (`minimumGrade`) of its `com.agentics.flux/1` extension namespace, for instance asking a stricter grade for one label. A value below the built-in floor of its label is refused with FLX-3105, as is a label that is not one of FLUID's. The edit is a change to the bundle and belongs to the contract owner and the user. Check it with the `flux-author-bundle` skill. Never use it to lower anything.

## What EVIDENCE.md says

The evidence report has a "Privacy Grade" section. It gives the run's grade and what it rests on, and it lists every labelled column with its floor and whether the run meets it. For a run graded P3 it states the guarantee in the terms NIST SP 800-226 asks for: the unit of privacy, the privacy loss (epsilon and delta, or rho), and the accounting, with what is not covered. A run that is not P3 says why. A profile that carries an account but is not P3 gives the run no guarantee, and the section says so.

## What a grade is not

It is a statement about what the run's numbers rest on. It is not a finding that the data is anonymous, which is a controller's assessment. A bound of differential privacy limits what any analysis of the output can learn about whether one unit was in the data, for that unit and loss only. Say no more than that to the user.

More: [the evidence pack](https://github.com/Agenticstiger/flux-engine/blob/v0.11.1/docs/explanation/the-evidence-pack.md).
