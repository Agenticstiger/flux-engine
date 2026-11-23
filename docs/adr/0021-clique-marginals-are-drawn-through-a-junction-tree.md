# ADR 0021: noisy clique marginals are drawn through a junction tree

## Status

Accepted 2026-11-21. Builds on [ADR 0019](./0019-one-privacy-budget-per-profile-release.md), which
lets a profile carry noisy clique marginals and checks that they agree, and on
[ADR 0014](./0014-init-reads-every-statistic-a-profile-publishes.md), whose Chow-Liu forest is the
degree-one case of this record.

## Context

Borrow-before-build search: "private-pgm junction tree synthetic data" (2026-11-21); fetched
https://github.com/ryan112358/private-pgm (Apache-2.0) and its `src/mbi/junction_tree.py`
(https://raw.githubusercontent.com/ryan112358/private-pgm/master/src/mbi/junction_tree.py).
Private-PGM measures marginals of cliques of columns under noise, and builds a junction tree
over them: a graph of the maximal cliques with edges weighted by the number of columns two
cliques share, whose maximum spanning tree is the tree. Its construction chooses no root. The
repository was read for the pattern. No code of it is used, copied or depended on, and it is not
a dependency of either distribution. The Chow-Liu forest of `flux_engine.calibrate.structure` is
the degree-one case of the same construction (cliques of two columns), so the machinery is
reused: Kruskal's rule with deterministic ties and a walk from a root.

## Decision

1. **The tree.** `fluxsim init` arranges the cliques of one table into the maximum-weight
   spanning forest of the graph of its cliques, each weighted by the number of columns two cliques
   share (`flux_engine.calibrate.junction`). The cliques have a junction tree exactly when this
   forest has the running intersection property: for every column, the cliques that hold it
   are connected. A set of cliques that fails is refused (`FLX-3402`), naming two cliques that
   hold one column with nothing between them that does.
2. **The root and the order are chosen without a choice.** Each tree's root is its clique with
   the most columns, and of those the one whose sorted column names come first. The walk is breadth
   first from the root, children in the order of their rank by the same key, so the profile's
   order of cliques changes nothing.
3. **Each column is drawn given its separator.** A clique introduces the columns its parent does
   not hold, in the table's declared order. The column at position `j` is drawn given the
   separator and the columns the clique introduced before it (`new[:j]`), so the product of the
   conditionals is the joint the cliques describe.
4. **A conditional is a distribution by one rule.** For each combination of levels of the
   columns it is given, the vector of the clique's noisy counts, summed over the clique's other
   columns, is made a distribution by the rule of PROFILE-SPEC.md section 3a (projected onto the
   non-negative vectors that add up to the vector's noisy total, then normalised). A vector whose
   total is not above zero has equal shares, which the bundle states as an assumption.
5. **Scope.** The columns are categorical. A column that a rule of the contract sets, a key, or a
   column with a declared null fraction is not drawn from a clique. The levels a clique lists for
   a column are the levels the contract declares for it, when it declares them.

## Consequences

- The chain of conditionals reproduces each clique's own distribution when the cliques agree on
  their separators, which the profile reader checks (PROFILE-SPEC.md section 5a). The projection of
  a negative count is the one place where it need not.
- A table with cliques and a table with grouped statistics are drawn by separate rules: columns
  in a clique are never drawn by the Chow-Liu forest.
