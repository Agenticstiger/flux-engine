# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

# The local mirror of CI.
#
# Every CHECK in .github/workflows/ci.yml is invoked as one of the targets
# below. That is what keeps this file honest: a check CI has and
# `make ci-local` does not cannot be added without adding a target here.
# Each target names the CI job it mirrors.
#
# The workflow does run a handful of commands that are not `make` targets,
# and none of them is a check. Counted from the workflow at the time of
# writing, they are: three `pip install -e ".[dev]"` steps, two assertions
# that the sibling FLUX checkout is on disk (a contributor does not need it,
# see below), one step printing the runner's cpu_count, and the `CI gate`
# job's own aggregation of its dependencies' results. Setup and plumbing, in
# other words, not verdicts about the tree.
#
# Job names in .github/workflows/ci.yml:
#   Gates                     lint, format-check, typecheck, imports,
#                             register-lint, guardrail, reuse, describe,
#                             gate-conform
#   Test (os, py, suite)      typecheck, test-fast / test-fast-cov,
#                             test-vectors, test-digests, test-slow,
#                             describe, gate-conform
#   Slow suite (shard)        slow-partition, test-slow-acceptance,
#                             test-slow-rest
#   Packaging (wheel smoke)   packaging-smoke
#   Proof material            proof-guard
#   CI gate                   aggregate only, no target of its own

.PHONY: ci-local lint format-check typecheck imports register-lint guardrail reuse proof-guard \
        test test-fast test-fast-cov test-vectors test-digests test-determinism \
        test-slow test-slow-acceptance test-slow-rest slow-partition \
        describe gate-conform packaging-smoke lineage-extra third-party-licenses \
        release-guard first-run docs-gen docs-check docs-test check bench

# Local runs use the repository virtualenv. CI installs into the runner's own
# interpreter and sets BIN= (empty) in the workflow env, so every tool below
# resolves on PATH there. GNU Make's ?= leaves an environment-defined variable
# alone even when its value is empty; verified on GNU Make 3.81, the macOS
# default. If that override ever failed to apply, the recipe would invoke a
# .venv path that does not exist on a runner and the step would fail loudly
# rather than skip quietly.
BIN ?= .venv/bin/

SHELL := /bin/bash
# Deliberately NOT `.SHELLFLAGS := -eu -o pipefail -c`: GNU Make 3.81 has no
# SHELLFLAGS variable at all, so a recipe that leans on it for pipefail
# silently gets none and a failing command on the left of a pipe is masked.
# Probed on this machine: `make -p` returns zero matches for SHELLFLAGS, and a
# recipe reading `false | cat` under that line still exits 0.
#
# Every recipe below is written without a load-bearing pipe instead. Each
# command is its own recipe line, and make stops on the first non-zero exit.

PARALLEL ?= -n auto --dist loadscope

# A separate, narrower width for the selections that carry the slow marker.
# Those tests spawn `fluxsim` as a real subprocess and materialise 25,000-subject
# worlds, so each one is already using a core of its own; running sixteen of
# them at `-n auto` on a sixteen-core machine double-books every core. Measured
# here: a whole-suite run at `-n auto` took 38m43s and killed a `fluxsim run`
# subprocess on its 600-second timeout (SIGKILL, returncode -9), while the same
# slow selections at this width were measured at 140s and 113s. `-n 2` is the
# width the shard timings in pyproject.toml were taken at, so it is the one the
# numbers describe.
PARALLEL_SLOW ?= -n 2 --dist loadscope
COVERAGE := --cov=flux_engine --cov-report=term-missing --cov-fail-under=70
CI_SCRATCH := build/ci

# The three selections that carry the slow marker between them. They are
# complementary by construction, not by an enumerated list, so a new slow test
# lands in exactly one of them with nothing to update here: anything under
# tests/acceptance goes to the acceptance shard, anything under tests/golden
# goes to the test cells, anything else goes to the rest shard. The
# `slow-partition` target checks that on every run.
DIGESTS := tests/golden tests/acceptance/test_one_byte.py

# Runs on the digests cell alongside DIGESTS, and deliberately NOT part of it.
# The closed-set rule turns a stray file in a bundle directory into a refusal,
# and macOS is the platform that writes .DS_Store, so the macOS cell has to run
# these. They are not slow-marked, and DIGESTS is also one third of the
# slow-marker partition checked by `slow-partition`, so folding them into
# DIGESTS makes that check fail with them as extras.
DIGESTS_EXTRA := tests/stages/test_input_digest_closed_set.py
SLOW_ACCEPTANCE := -m slow tests/acceptance --ignore=tests/acceptance/test_one_byte.py
SLOW_REST := -m slow --ignore=tests/acceptance --ignore=tests/golden

# Exactly what CI runs, target for target, and nothing CI does not run. It
# used to include `test`, the whole suite fast and slow together in one
# invocation, which no CI job performs. That configuration is the one that
# oversubscribes: it failed here at 38m43s with three Hypothesis deadline
# misses and a `fluxsim run` subprocess killed on its timeout. A gate that fails
# on a configuration nothing ships is worse than no gate, so ci-local now runs
# the CI selections and `test` stays available for a developer who wants it.
ci-local: lineage-extra lint format-check typecheck imports register-lint guardrail reuse \
          proof-guard describe gate-conform slow-partition test-fast-cov test-digests \
          test-slow-acceptance test-slow-rest packaging-smoke

# The sibling ../FLUX checkout is optional: only the tests that read its example
# bundles need it, and without it they are skipped. A missing one must still
# fail the local mirror the same way it fails CI, where this is set at workflow
# level, so a green ci-local never has those tests skipped. A bare `make test`
# stays permissive for a developer who has not cloned the specification
# repository.
# Target-specific exports propagate to prerequisites, and running a
# prerequisite on its own does not see them; both verified on GNU Make 3.81.
ci-local: export FLUX_EXAMPLES_REQUIRED := 1

# The gate a contributor runs before opening a pull request: every static
# check, then the fast tests. CONTRIBUTING.md names it. Not a CI job of its
# own, and not a new check: it only composes targets that already exist, so
# the slow shards and the digest cells stay with `ci-local` and CI.
check: lint format-check typecheck imports register-lint guardrail reuse proof-guard test-fast

# ---------------------------------------------------------------- Gates
# Mirrors the `Gates` job. ruff, black and import-linter run there once for
# the whole pipeline: their verdicts are fixed by configuration rather than by
# the running interpreter (ruff and black pin target-version, and the import
# graph carries no interpreter branching).

lint:
	$(BIN)ruff check flux_engine tests scripts

format-check:
	$(BIN)black --check flux_engine tests scripts

# Mirrors `Gates`, and is repeated on every `full` cell of the `Test` matrix.
# This is the one linter not consolidated away: mypy pins python_version for
# the language level, but third-party stub resolution still follows the
# running interpreter and numpy is range-pinned.
# scripts/ is type-checked too, not just the package. Three of the four files
# there ARE gates (register_lint.py, release_guard.py, third_party_licenses.py),
# so the code deciding whether a release or a merge may proceed was the code no
# type gate covered. Adding it surfaced one real annotation error on the day it
# was switched on.
typecheck:
	$(BIN)mypy flux_engine scripts

imports:
	$(BIN)lint-imports

# Mirrors `Gates`. Absorbs the whole former `register-lint` job.
register-lint:
	$(BIN)python scripts/register_lint.py

# Mirrors `Gates`. The positive side of the same discipline: register_lint.py
# bans a fixed vocabulary, this checks the names in use are the canonical ones.
# --self-test runs first, and on its own recipe line, because a corrupt
# registry must fail loudly rather than quietly bless the tree.
#
# CI ran this before `make ci-local` did. Adding it here closes that gap.
guardrail:
	$(BIN)python tools/product_guardrail/check.py --self-test
	$(BIN)python tools/product_guardrail/check.py

# Every file states its licence and copyright holder: a header in each source
# file, and REUSE.toml for the files that cannot take one (see CONTRIBUTING.md).
reuse:
	$(BIN)reuse lint

# Mirrors the `Proof material` job. The engine ships the ProofSystem port and the
# no-op NoProofSystem; proof-system backends are out of scope for this repository
# (CONTRIBUTING.md), and this fails on anything that looks like one. It reads the
# files git knows, so a .venv or a build directory is never looked at.
proof-guard:
	$(BIN)python scripts/check_proof_material.py

# ------------------------------------------------------------- Releasing
# Deliberately NOT part of `ci-local`, which mirrors ci.yml's job list
# exactly and must keep doing so. The controls reach the pull-request path
# through tests/release/test_release_guard.py, which the fast subset
# collects like any other test; this target is for a releaser who wants the
# refusal on their own machine rather than on a pushed tag.
#
# Same shape as `guardrail` above, and for the same reason: --self-test on
# its own recipe line, first, so a guard that has stopped firing fails
# loudly rather than quietly blessing a release.
#
# This does NOT check KERNEL_VERSION, and must not start. That constant is
# normative kernel behaviour hashed into inputDigest, not the package
# version, and `make test-digests` is a far stronger guard over it.
release-guard:
	$(BIN)python scripts/release_guard.py --self-test
	$(BIN)python scripts/release_guard.py --check-tree

# ----------------------------------------------------------------- Test
# Mirrors the `Test (os, python, suite)` matrix.

# Mirrors a `full` cell. Every cell but one runs this uncovered form.
test-fast:
	$(BIN)pytest -m "not slow" $(PARALLEL)

# Mirrors the single ubuntu 3.12 `full` cell, the only one carrying the
# coverage floor. Coverage is a property of the source rather than of the
# interpreter, and measuring it roughly doubles the run. Note that
# [tool.coverage.report] fail_under = 70 enforces the same floor even without
# the flag; the flag is kept so the number is visible where it is enforced.
test-fast-cov:
	$(BIN)pytest -m "not slow" $(PARALLEL) $(COVERAGE)

# Mirrors the macOS `digests` cell. The `golden` marker and the tests/golden
# directory do NOT overlap: no test under tests/golden carries that marker, so
# the determinism cell needs both selections rather than one.
test-vectors:
	$(BIN)pytest -m golden $(PARALLEL)

# Mirrors a step that runs on EVERY cell of every matrix shape. The pinned
# plan.json, run-digest.json, component digests, artifact-size baseline and
# one-byte differentials. These are deselected by `-m "not slow"`, so before
# this pipeline they only ever ran in one job on one operating system.
test-digests:
	$(BIN)pytest $(DIGESTS) $(DIGESTS_EXTRA) $(PARALLEL_SLOW)

test-determinism: test-vectors test-digests

# ------------------------------------------------------------ Slow suite
# Mirrors the `Slow suite (shard)` matrix, one target per shard.

test-slow-acceptance:
	$(BIN)pytest $(SLOW_ACCEPTANCE) $(PARALLEL_SLOW)

test-slow-rest:
	$(BIN)pytest $(SLOW_REST) $(PARALLEL_SLOW)

# Mirrors the `pytest, slow subset` step on the six-cell release shape, where
# the slow suite runs whole rather than sharded.
test-slow:
	$(BIN)pytest -m slow $(PARALLEL_SLOW)

# The whole suite in ONE invocation, fast and slow together. No CI job does
# that, and neither does ci-local: at `-n auto` this oversubscribes badly
# enough to fail on timing rather than on behaviour. Kept as a developer
# convenience, and if you run it, pass a narrower width:
#     make test PARALLEL='-n 4 --dist loadscope'
test:
	$(BIN)pytest $(PARALLEL) $(COVERAGE)

# Mirrors the `Slow selections partition the slow marker` step on the
# `Slow suite (acceptance)` shard.
#
# Compares collected node-id SETS, not exit codes. Each collection is its own
# recipe line, so a pytest that fails to collect aborts make rather than
# feeding a short list into a comparison that would then pass.
slow-partition:
	@mkdir -p $(CI_SCRATCH)
	$(BIN)pytest -m slow --collect-only -p no:cacheprovider > $(CI_SCRATCH)/slow-all.txt
	$(BIN)pytest $(SLOW_ACCEPTANCE) --collect-only -p no:cacheprovider > $(CI_SCRATCH)/slow-acceptance.txt
	$(BIN)pytest $(SLOW_REST) --collect-only -p no:cacheprovider > $(CI_SCRATCH)/slow-rest.txt
	$(BIN)pytest $(DIGESTS) --collect-only -p no:cacheprovider > $(CI_SCRATCH)/slow-digests.txt
	$(BIN)python -c "import pathlib; ids=lambda n: {l.strip() for l in pathlib.Path('$(CI_SCRATCH)/'+n).read_text().splitlines() if '::' in l}; parts=[ids('slow-acceptance.txt'), ids('slow-rest.txt'), ids('slow-digests.txt')]; every=ids('slow-all.txt'); dupes=set().union(*[a & b for i, a in enumerate(parts) for b in parts[i+1:]]); assert not dupes, 'selected by more than one job: %s' % sorted(dupes); union=set().union(*parts); assert union == every, 'missing: %s / extra: %s' % (sorted(every - union), sorted(union - every)); print('slow marker partitioned exactly: %d tests across %d selections (%s)' % (len(every), len(parts), ', '.join(str(len(p)) for p in parts)))"

# ------------------------------------------------------- CLI wire claims
# Mirror steps in `Gates` and on every `Test` cell.

# Exit code AND content. This step used to gate on the exit code alone, so
# truncated or malformed output would have passed. The spec_source assertion
# closes a recorded incident where an undeclared backend had CI running the
# vendored flux_spec copy while a development machine ran the installed
# distribution, silently.
describe:
	@mkdir -p $(CI_SCRATCH)
	$(BIN)fluxsim describe --self --json > $(CI_SCRATCH)/describe.json
	@head -3 $(CI_SCRATCH)/describe.json
	$(BIN)python -c "import json; d=json.load(open('$(CI_SCRATCH)/describe.json')); a=d['artifacts']; assert d['ok'] is True and d['exitCode'] == 0, d; missing=[k for k in ('commands', 'capabilities', 'version', 'spec_version') if not a.get(k)]; assert not missing, 'describe --self dropped %s' % missing; assert a['spec_source'] == 'flux_spec', 'running the vendored flux_spec fallback rather than the declared distribution: spec_source=%r' % a['spec_source']; print('describe ok:', a['version'], a['spec_version'], a['spec_source'])"

# The equality catches a vector that fails. The floor catches a vector set
# that silently shrank, which comparing the count to itself would pass. A
# floor rather than an equality means legitimately adding a vector needs no
# edit here.
gate-conform:
	@mkdir -p $(CI_SCRATCH)
	$(BIN)fluxsim gate conform > $(CI_SCRATCH)/conform.txt
	@cat $(CI_SCRATCH)/conform.txt
	$(BIN)python -c "import re; t=open('$(CI_SCRATCH)/conform.txt').read(); m=re.search(r'(\d+)/(\d+) RFC-04 conformance vectors passed', t); assert m, 'gate conform reported no vector count: %r' % t; p, q = int(m.group(1)), int(m.group(2)); assert q >= 34, 'the vector set shrank to %d; the floor is 34' % q; assert p == q, 'only %d of %d vectors passed' % (p, q); print('conformance vectors: %d/%d' % (p, q))"

# ------------------------------------------------------------ Packaging
# Mirrors the `Packaging (wheel smoke)` job.
#
# Every other job runs from an editable install, where the source tree is on
# the path and package data is whatever happens to be on disk. This builds the
# distribution in its own isolated interpreter (so a developer's .venv is
# never mutated), installs the wheel into a second clean one, and runs both
# CLI gates from a directory that is not the source tree, so a broken
# [tool.setuptools.package-data] fails here rather than in a user's
# environment. The last two recipe lines scaffold the starter project from the
# installed wheel and run it through verify and ship: a wheel that lacks the
# starter files installs fine and then fails on the first command anyone types.
packaging-smoke:
	rm -rf $(CI_SCRATCH)/pkg
	mkdir -p $(CI_SCRATCH)/pkg
	$(BIN)python -m venv $(CI_SCRATCH)/pkg/build-env
	$(CI_SCRATCH)/pkg/build-env/bin/pip install --quiet --upgrade pip build
	$(CI_SCRATCH)/pkg/build-env/bin/python -m build --outdir $(CI_SCRATCH)/pkg/dist .
	$(BIN)python -m venv $(CI_SCRATCH)/pkg/venv
	$(CI_SCRATCH)/pkg/venv/bin/pip install --quiet $(CI_SCRATCH)/pkg/dist/*.whl
	cd $(CI_SCRATCH)/pkg && ./venv/bin/fluxsim gate conform
	cd $(CI_SCRATCH)/pkg && ./venv/bin/fluxsim describe --self --json > describe.json
	$(BIN)python -c "import json; a=json.load(open('$(CI_SCRATCH)/pkg/describe.json'))['artifacts']; assert a['spec_version'], a; print('installed wheel describes:', a['version'], a['spec_version'], a['spec_source'])"
	cd $(CI_SCRATCH)/pkg && ./venv/bin/fluxsim init project
	cd $(CI_SCRATCH)/pkg/project && ../venv/bin/fluxsim run . --verify --ship -o out

# ------------------------------------------------------------ Lineage extra
# Mirrors the `Lineage extra` job. Every other target runs without the
# OpenLineage client, so its real-client tests skip and mypy does not see the
# client's annotations. This one needs the `lineage` extra
# (`uv sync --extra dev --extra lineage`) and fails when the client cannot be
# imported, so a mirror run cannot pass by skipping those tests.
lineage-extra:
	$(BIN)python -c "import openlineage.client" || { echo "lineage-extra needs the lineage extra: uv sync --extra dev --extra lineage" >&2; exit 1; }
	$(BIN)mypy flux_engine scripts
	$(BIN)pytest -m "not slow" tests/lineage tests/cli/test_human_surface.py

# ------------------------------------------------------------ First run
# Mirrors the `First run` workflow, .github/workflows/first-run.yml. It is its
# own workflow and not part of `ci-local`: it needs `uv` on PATH, and what it
# produces is a number to watch rather than a verdict about the tree.
#
# Builds the wheel, then does what a newcomer is told to do, through `uvx`, from
# a directory that is not this checkout and with an empty uv cache:
# `fluxsim init demo`, then `fluxsim run . --verify --ship -o out`. Prints the
# time to first success and fails when it passes the five-minute budget.
first-run:
	$(BIN)python scripts/first_run.py

# ----------------------------------------------------------------- Docs
# The pages under docs/reference/cli and docs/reference/errors are generated and
# never written by hand: the commands from the argparse tree, one page per FLX
# code from the error catalog (scripts/gen_docs.py). `docs-check` fails when a
# page is missing, differs from what the code would generate, or is left over
# from a command or code that is gone. It is not a separate CI step:
# tests/docs/test_generated_docs.py runs the same check inside the suite, on
# every cell, which is what turns drift into a red run.
docs-gen:
	$(BIN)python scripts/gen_docs.py

docs-check:
	$(BIN)python scripts/gen_docs.py --check

# Runs every Markdown page as a test (tests/docs, harness.py says what a block
# means): the commands run, the output is compared with the page, the files the
# page tells you to write are written. In CI the page runs sit in the slow shard
# (tests/conftest.py, SLOW_MODULES), so a pull request runs them once; this
# target is the quick way to run only the documentation while writing a page.
# The pages are independent, so they are spread over the workers (`load`) rather
# than kept in one module's worker (`loadscope`, the default above).
docs-test:
	$(BIN)pytest tests/docs -n auto --dist load

# ---------------------------------------------------------------- Other
# Not a CI gate. What the output sinks cost next to generating the rows they
# write (tests/bench): needs the accuracy group for pytest-benchmark
# (`uv sync --extra dev --group accuracy`). One process, because
# pytest-benchmark switches itself off under xdist. FLUX_BENCH_ROWS sets the
# stream length (default 20000).
bench:
	$(BIN)pytest tests/bench -m slow -p no:xdist -p no:cacheprovider --benchmark-only \
	  --benchmark-columns=min,mean,max,rounds --benchmark-sort=name

# Not a CI gate. Regenerates THIRD_PARTY_LICENSES.md on demand.
third-party-licenses:
	$(BIN)python scripts/third_party_licenses.py
