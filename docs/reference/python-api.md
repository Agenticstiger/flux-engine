---
title: "Python API"
description: "flux_engine.api and flux_engine.engine: the governed extension contract and the library entry point, name by name."
---

# `flux_engine.api` — the governed extension contract

This package is the surface a third-party implementation targets and an
in-process consumer imports against. Everything outside `flux_engine.api`
and `flux_engine.engine` (the facade described under "The engine facade"
below) is internal and may move without notice; everything inside this
package is governed by `__api_version__`, which is `"0.1"` today and
moves on its own schedule — removing or renaming anything listed below is
a breaking change to that number regardless of what the engine's own
version does.

The package holds Protocols and frozen records only. There is no logic
here to disagree with the kernel about, no runtime import into the kernel,
and no number that is not a `Decimal` or an `int`.

## Rules the surface keeps

- `VerifyVerdict` keeps two answers apart: whether a run reproduces, and whether the
  scenario describes the world. It has no `__bool__`, so `if verdict:` is always true,
  and no `passed` or summary accessor. Read the two fields.
- `Scorecard` records where a dial's setting came from. A grade that was never computed
  is reported as absent, never as a default number.
- `AccuracyGate` fixes an order: register a prediction before its horizon, score it
  against the readout afterwards, record the verdict either way, misses included.
- Every record is frozen, every plural field is a tuple, and no field holds a float.

Why the two verdict fields stay apart is in
[the evidence pack](../explanation/the-evidence-pack.md).

**Four ports are reserved**: `AccuracyGate`, `ExpressionEvaluator`, `FidelityProver` and
`SkillRunner`. This build publishes their shape, ships no implementation of it, and has
no parameter through which a consumer could inject one. They are shapes to design
against, not extension points to ship against, and the tables below mark them.

## The names

### Digests

| Name | What it is |
|---|---|
| `DigestRef` | A digest together with the domain it was taken in and what it was taken over. The unit every port speaks in, so two digests of different kinds are never silently compared. |
| `DIGEST_DOMAINS` | The three domains a digest can belong to: file bytes, a canonical document, an emitted stream. |

### Expressions

| Name | What it is |
|---|---|
| `ExpressionEvaluator` | *Reserved.* The shape of a checker for a segment or guard expression: it compiles against the attribute model and returns a predicate, and every way an expression can be wrong is reported at compile time, not when it runs. The engine's own compiler is a pair of module-level functions of a different shape. |
| `ExpressionScope` | The three scopes an expression can be written against: a subject, an event, an observed readout. |

### Emitting rows

| Name | What it is |
|---|---|
| `EmitHeader` | The declared shape of one emitted stream: the expose, its columns in declared order, their types, and the contract they were declared under. |
| `EmitSink` | Somewhere rows are written — opened once, written to, closed once, including when the run fails. The stream's digest is taken above the sink, so which sink is in use cannot change what the run is answerable for. |

### Calibration

| Name | What it is |
|---|---|
| `Calibrator` | Turns aggregate statistics into dial settings and reports how much those settings are worth. Aggregates only: individual records never reach it. |
| `Scorecard` | Where a setting came from, how it was graded, over what period, and how much weight that supports. Reports absence as absence. |
| `Provenance` | Declared, fitted, or scored. |
| `Credibility` | The band the evidence supports: not established, declared, backtested, or validated against a live comparison. |

### Outcome accuracy

| Name | What it is |
|---|---|
| `AccuracyGate` | *Reserved.* Register a prediction, score it against what was observed, record the verdict. The `fluxsim accuracy` commands do this work; they do not go through this shape. |
| `PredictionRecord` | A statement about the future, fixed before the future arrives. |
| `ObservedReadout` | What actually happened, read after the horizon, with the sample it rests on. |
| `AccuracyVerdict` | The comparison of one prediction with one readout, carrying what it rests on so it still says so when quoted alone. |
| `AccuracyOutcome` | Within the declared interval, outside it, or not scored. |

### Claims

| Name | What it is |
|---|---|
| `Claim` | One sentence the engine will stand behind, with the basis it rests on and the digests a reader can re-take. |
| `ClaimType` | The four questions a claim can answer: conformance, reproducibility, fidelity, validity. |
| `ClaimBasis` | What a claim rests on: a live randomised comparison, a historical period held out of the inputs, or nothing. The third is a stated position, not a missing field. |

### The fidelity check

| Name | What it is |
|---|---|
| `FidelityStatement` | What is being checked and against which committed summary of the source. |
| `FidelityProver` | *Reserved.* The shape of something that runs where the source data lives and produces a backing artefact if its backend has one. In v0.1 no backend does, so nothing implements it. |
| `FidelityVerifier` | Runs anywhere; re-evaluates the statement from what crossed the boundary and returns the claim it supports. |
| `ProofSystem` | Names the backend a record was produced under. In v0.1 there is one, called `none`, and the record states it rather than omitting it. |

### The agentic tier

| Name | What it is |
|---|---|
| `ModelPort` | The one place a language model can be reached. It may only return content; it has no path to the scheduler, the trace, or an emitted row. |
| `SkillRunner` | *Reserved.* Runs a skill through a model port under a token budget, reusing an earlier answer when the call is identical. |
| `SkillInvocation` | One call, addressed by everything that makes it repeatable: the seed path, the request, and the governing policy. |
| `SkillProposal` | Content offered to the engine, which decides what to do with it — including whether the content was reused rather than asked for again. |

### Lineage

| Name | What it is |
|---|---|
| `LineageEmitter` | Sends lineage reports somewhere. A receiver that is down loses the report, never the work; whether reports arrived is a question a caller can ask. |
| `LineageEvent` | One report about a run at one point in its life. Carries digests and declarations, never rows. |
| `LineageEventType` | Start, complete, fail, abort. |
| `DatasetRef` | One end of a lineage edge. |

### The verify readout

| Name | What it is |
|---|---|
| `VerifyVerdict` | Two disjoint answers and their claims. `headline()` renders both in one line — `reproducible: yes \| validity: not asserted` — because the second half's absence reads, to every reader, as the stronger claim. |
| `Reproducibility` | Reproducible, not reproducible, or not checked. The third is not a negative answer. |
| `Validity` | Not asserted, supported, or not supported. Not asserted is the default, and the honest answer for a run that measured nothing against the world. |

### Stage results

| Name | What it is |
|---|---|
| `StageResult` | What one stage returned: its exit code, its captured output, and a read-only view of its structured artefacts. |
| `StageName` | The seven stages, in order: validate, plan, apply, run, verify, ship, replay. |
| `EngineError` | Base failure. Catch it to handle any stage. |
| `ValidateFailed`, `PlanFailed`, `ApplyFailed`, `RunFailed`, `VerifyFailed`, `ShipFailed`, `ReplayFailed` | One per stage, so a caller that cares about exactly one — a withheld bundle, say — does not have to match on text. |

`StageResult.success` answers one question about one stage. It is not a
verdict about a run; that is `VerifyVerdict`, and it deliberately does not
reduce to a single boolean.

The stage functions in `flux_engine.engine` do not raise these errors. A stage that
fails returns a `StageResult` whose `success` is false and whose `error_code` names the
failure. Call `result.raise_for_status()` to get the stage's typed exception.

## The engine facade

`flux_engine.api` is the vocabulary; `flux_engine.engine` is the way in.
It is the library entry point a host process imports — one `async def`
per stage, dispatching to the synchronous stage functions via
`asyncio.to_thread`, so an in-process consumer never blocks its event
loop on a run that takes real wall-clock time. It mirrors forge-cli's
`fluid_build.engine` (the Pulumi Automation API pattern).

<!-- invisible-code-block: python
import os, pathlib
from flux_engine.starter import scaffold
os.chdir(workdir)
scaffold(pathlib.Path("bundle"))
-->

```python
import asyncio

from flux_engine.engine import run

result = asyncio.run(run("./bundle", state_dir="./.flux", verify=True))
assert result.success
result.raise_for_status()  # does nothing here; on a failed stage it raises RunFailed
```

Seven callables, one per stage, each returning the `StageResult` named
below: `validate`, `plan`, `apply`, `run`, `verify`, `ship`, `replay`.

Two contracts hold on them:

**Frozen kwarg names**, from that module's first release forward. Adding
an optional keyword argument is not a breaking change; renaming or
removing one is. Every callable takes its bundle directory positionally
and everything else by keyword.

**A soft timeout.** Every callable accepts `timeout: float | None`. When
it fires the awaiter gets `TimeoutError`, but the stage thread keeps
running in the background — Python threads cannot be cancelled — so this
is a bound on how long the *caller* waits, not a kill. Same contract
forge-cli's engine documents.

**More than one job.** `plan`, `apply`, `run` and `replay` take `jobs`
(default 1): how many processes may generate an emit's rows from declared
statistics. The rows and every digest are the ones one job gives. The
worker processes are started with spawn, so each one imports the host's
main module again: a script that passes `jobs` above 1 keeps its work
under `if __name__ == "__main__":`, and whatever it does at the top level,
outside that block, happens again in every worker. The soft timeout stops
neither the stage thread nor its workers; after `TimeoutError` they run on
until the stage ends.

Stability here is governed by the frozen-kwarg promise in that module's
own docstring, not by `__api_version__`: `__api_version__` covers the
names exported from `flux_engine.api`, and the facade exports none of
them — it returns them. Nothing in `flux_engine.engine` is on a digest
path (the import-linter contract in `pyproject.toml` forbids the kernel
packages from importing it), so it is orchestration over
`flux_engine.stages` and nothing else.

## Using it

```python
from flux_engine.api import VerifyVerdict, Reproducibility, Validity

verdict = VerifyVerdict(
    reproducibility=Reproducibility.REPRODUCIBLE,
    validity=Validity.NOT_ASSERTED,
)
assert verdict.headline() == "reproducible: yes | validity: not asserted"
```

Implementing a port is a matter of matching its shape — nothing needs to be
registered or subclassed, and every port can be checked at a boundary with
`isinstance`:

```python
from flux_engine.api import EmitSink

assert not isinstance(object(), EmitSink)  # an object with a sink's methods would pass
```

## Stability

The export list is pinned by a snapshot test that fails on an addition as well as on a
removal. A new name on this surface is therefore a decision, taken together with the
`__api_version__` bump and a row on this page, and never something noticed later. The
same test pins the rules listed at the top: frozen records, no float fields, ports that
can be checked with `isinstance`, and a `VerifyVerdict` with no way to collapse its two
answers into one.
