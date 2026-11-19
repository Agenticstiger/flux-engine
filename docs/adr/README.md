# Decision records

Each record says what was decided, why, and what else was considered. A record keeps the
text of its date; a later change is written into it as a dated amendment.

- [ADR 0001](./0001-diverge-from-mesa.md): diverge from Mesa for the simulation substrate. Accepted.
- [ADR 0002](./0002-flux-expr-diverges-from-cel.md): flux-expr/1 diverges from CEL. Accepted.
- [ADR 0003](./0003-sdv-bsl-and-aggregate-only-rule.md): calibrate from aggregates only, with no SDV and no fitting to records. Accepted, amended 2026-11-11 and 2026-11-16.
- [ADR 0004](./0004-concordia-tinytroupe-adapt-the-pattern.md): adapt the patterns of Concordia and TinyTroupe, and depend on neither. Accepted.
- [ADR 0005](./0005-depend-on-flux-spec.md): depend on `flux_spec` rather than reimplement it. Accepted, amended 2026-09-07 and again since.
- [ADR 0006](./0006-input-digest-is-a-closed-set.md): `inputDigest` covers a closed set of files, and the loader refuses anything else. Accepted.
- [ADR 0008](./0008-flux-expr-2-derived-columns-and-rules.md): flux-expr/2 implements FLUID SQL/1 and adds FLUX's extensions, and flux-expr/1 is unchanged. Accepted.
- [ADR 0009](./0009-flux-stats-distribution.md): the statistics are a distribution of their own, flux-stats. Accepted, amended 2026-11-17 and 2026-11-18.
- [ADR 0010](./0010-faster-generation-through-a-second-kernel.md): faster generation comes from a second kernel, with counter-based streams and basic-operation transforms, rather than from a Rust port of the first. Accepted, amended 2026-11-18.
- [ADR 0011](./0011-rows-follow-their-key-entity-and-parent.md): synthetic rows follow their key, the entity they share and the parent row their key names, through declared rules and statistics that `fluid profile` checks over each relationship. Accepted.
- [ADR 0012](./0012-a-quantile-is-published-as-the-bin-that-holds-it.md): a quantile is published as the bin that holds it, when its declaration names bins. Accepted, amended 2026-11-18.
- [ADR 0013](./0013-flux-expr-3-date-parts-date-arithmetic-and-concatenation.md): flux-expr/3 is flux-expr/2 plus a date part, date arithmetic and string concatenation, as FLUX-side rules only. Accepted.
