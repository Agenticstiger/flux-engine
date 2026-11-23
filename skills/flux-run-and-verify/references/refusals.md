# Refusals

A refusal carries an `FLX-nnnn` code. The thousands digit says which part of the engine raised it. For any
code: run `fluxsim explain FLX-nnnn` (offline, the same text as the page), then, if needed, open
its page, which `fluxsim explain` prints as the last line, or the index of
[all error codes](https://github.com/Agenticstiger/flux-engine/blob/v0.10.0/docs/reference/errors/README.md). `fluxsim explain --list`
lists every code in the installed engine. Apply the remedy it prints, then run the same step again, except where a remedy asks for the person's own decision (see "Handling a refusal"). Never
weaken a check, never edit a recorded plan or digest, never record past a refusal.

## Code families

| Family | What it usually means | Next step |
|---|---|---|
| 1000 canonicalisation | a value cannot be written in the engine's canonical form (a float, a very large integer, a key collision) | the value is in a document of the bundle; the `flux-author-bundle` skill fixes the document |
| 2000 plan and replay binding | the run no longer matches its plan or bundle, or there is no such plan or run (`FLX-2101` bundle moved, `FLX-2102` plan edited, `FLX-2107` no plan at that id, `FLX-2109` no applied run, `FLX-2110` artifacts do not match the run digest, `FLX-2113` run folder does not match its plan) | plan again after a bundle change and use the new run id; check the run id and `--state-dir`; if artifacts moved, find what edited them and report it |
| 2114 golden baseline | the run digest moved from the recorded baseline | find what changed (the engine, a document or an input; `verify` names the component) and report it; never record a new baseline unless the user asks |
| 3000 seam and conformance | the bundle and its contract do not fit: a document does not validate (`FLX-3105`), the contract digest or reference is wrong, a column has no source, an unknown file in the bundle folder (`FLX-3108`), a run file would land inside the bundle (`FLX-3106`), a profile that does not pair (`FLX-3109`, `FLX-3110`) | the `flux-author-bundle` skill for a bundle or contract fix; for `FLX-3108` or `FLX-3106` move the file or the state folder out of the bundle; for a profile mismatch ask the user for the right profile |
| 3000 (shard, scale, sinks) | `FLX-3404` and `FLX-3405` are a bad `--shard` or `--scale`; `FLX-3501` to `FLX-3507` are sink refusals (missing extra, no exact form, a target it cannot use) | the `flux-scale-and-ship` skill |
| 4000 governance and sovereignty | the declared zone, an agent policy, a jurisdiction, or the run's privacy grade is below a column's minimum (`FLX-4005`) | the `flux-privacy-and-profiling` skill for `FLX-4005` and any grade or profile question; never lower a grade or change a sensitivity label |
| 5000 numerics | a number or a declaration is out of its domain: the fixed-point grid, draw parameters, the fidelity block (`FLX-5030`), a committed slice that cannot be read (`FLX-5035`), a profile that is refused (`FLX-5036`) or does not answer a property (`FLX-5037`) | the `flux-author-bundle` skill for a declaration; for a slice or profile file ask the user for the right one |
| 6000 expression language | an expression or a rule in the bundle or contract does not parse, type-check or fit its column | the `flux-author-bundle` skill; do not loosen the contract |
| 7000 calibration | a prediction, an observed readout or an accuracy record is refused (seal, order, shape, a missing file) | `fluxsim explain` for the code, then the page; a sealed prediction is never changed after the fact |
| 8000 evidence and pack | the ship gate withheld the bundle (`FLX-8001`), a fidelity record is malformed or its signature does not verify (`FLX-8002`), or report prose has a reserved word (`FLX-8030`) | the `flux-scale-and-ship` skill; `FLX-8001` names the check that failed, so report it as it is |

Finding a page for a code, a guide or a specification belongs to the `flux-docs-lookup` skill.

## Handling a refusal

1. Quote the code and its one-line meaning to the user.
2. Name what `fluxsim explain` says to do.
3. If the remedy is in a file of the bundle, change only what the user's request covers; if it is a decision
   (a grade, a label, a baseline, a key, a profile, a generator plugin), ask the user. For a plugin, a code such as
   `FLX-3119` has a remedy that adds a flag to allow it. Never pass `--allow-generator` on your own judgement: only the person running `fluxsim` decides that, so stop and ask them, naming the plugin.
4. Run the step again. If it refuses with another code, repeat from the top; do not loop on the same code
   without a change.

A usage error (exit code 2, no code) is not in the table: the installed engine differs from this skill.
Read `fluxsim <command> --help`.

```bash
fluxsim explain FLX-2101
```
