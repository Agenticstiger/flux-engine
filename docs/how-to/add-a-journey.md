---
title: "Add a journey"
description: "Add a second signal, detector, treatment and journey, wire them through a Module, and fix the two mistakes that stop a journey from running."
---

# Add a journey

Goal: add a second journey to the starter project and see its customers move. A journey and its detector reach a run only through a Module's `spec.resources`, listed by the Simulation's `moduleRefs`.

## 1. Start a project

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
```

## 2. Add the signal, detector and treatment

A repeat support call is the new event. The detector fires when two arrive within a day (86400000 ms). The treatment is a callback.

```yaml
# signal-support.flux.yml
fluxVersion: "0.5.0"
kind: Signal
id: sig-support-call
name: Repeat Support Call Signal
metadata: { owner: { team: growth-lab } }
spec: { envelope: cloudevents/1.0, source: "urn:flux:contact-centre", type: com.telco.support.call_repeated }
```

```yaml
# detector-support.flux.yml
fluxVersion: "0.5.0"
kind: Detector
id: det-support-repeat
name: Repeat Support Call Detector
metadata: { owner: { team: growth-lab } }
spec:
  timeWindowMs: 86400000
  pattern: [{ signalRef: sig-support-call }, { signalRef: sig-support-call }]
```

```yaml
# treatment-callback.flux.yml
fluxVersion: "0.5.0"
kind: Treatment
id: tr-callback
name: Retention Callback
metadata: { owner: { team: growth-lab } }
spec: { actionType: retention_callback, payloadTemplate: { message: "We will call you back today" } }
```

## 3. Add the journey (with a mistake)

The journey moves a customer to a new state when its treatment works. This one names a state, `vip`, that the taxonomy does not have.

```yaml
# journey-support.flux.yml
fluxVersion: "0.5.0"
kind: Journey
id: jr-support-callback
name: Support Callback Journey
metadata: { owner: { team: growth-lab } }
spec:
  trigger: { signalRef: sig-support-call }
  intervention: { treatmentRef: tr-callback, action: offer_callback }
  taxonomyRef: jt-subscriber-lifecycle
  personaReactions:
    persona-pragmatic-family: { successProbability: 0.3, outcome: retained, nextState: vip }
```

```console
$ fluxsim validate .   # exit 1
FLX-3105: bundle does not validate, so its seam cannot be resolved --...
  jr-support-callback: personaReactions['persona-pragmatic-family'].nextState 'vip' is not a state of jt-subscriber-lifecycle
more: fluxsim explain FLX-3105  https://agenticstiger.github.io/flux/engine/reference/errors/FLX-3105.html
```

The engine's own check at plan time for this is [FLX-2302](../reference/errors/FLX-2302.md). The validator catches it first. Fix it by using a state from `taxonomy.flux.yml`. `atrisk` to `active` is an allowed transition:

```yaml
# journey-support.flux.yml
fluxVersion: "0.5.0"
kind: Journey
id: jr-support-callback
name: Support Callback Journey
metadata: { owner: { team: growth-lab } }
spec:
  trigger: { signalRef: sig-support-call }
  intervention: { treatmentRef: tr-callback, action: offer_callback }
  taxonomyRef: jt-subscriber-lifecycle
  personaReactions:
    persona-pragmatic-family: { successProbability: 0.3, outcome: retained, nextState: active }
```

A state that exists but has no edge from where the customer is does not stop the run. In a test with `nextState: churned`, only `atrisk` customers moved, because that is the only state with an edge to `churned`.

## 4. Run: nothing happens

```console
$ fluxsim run .
...
emitted payment_recovery_moment: 154 rows (scenario)
run ... complete
$ grep -c jr-support-callback .flux/runs/*/events.ndjson   # exit 1
0
```

The bundle is valid and the new documents are in it, but the Module does not list them. The run ignores them: still 154 rows, and no event mentions the new journey. Listing a detector under `signalRefs` fails instead, with `resolves to kind Detector, expected Signal`. The route is the Module; the engine's error for a Journey or Detector reached any other way is [FLX-2301](../reference/errors/FLX-2301.md).

## 5. Wire the Module

```yaml
# module.flux.yml
fluxVersion: "0.5.0"
kind: Module
id: module-payment-recovery
name: Payment Recovery Module
version: 2.1.3
metadata: { owner: { team: growth-lab } }
spec:
  resources: [sig-payment-aborted, det-payment-aborted-burst, jr-payment-recovery, det-support-repeat, jr-support-callback]
  binds: [{ port: event-envelope, config: { spec: cloudevents/1.0 } }, { port: fluid-contract }]
```

The Simulation already lists this Module, so it needs no edit. The signal comes in through the detector that names it.

```console
$ rm -rf .flux
$ fluxsim run .
...
emitted payment_recovery_moment: 335 rows (scenario)
run ... complete
$ grep -c jr-support-callback .flux/runs/*/events.ndjson
120
```

Rows went from 154 to 335: the new detector's firings are rows too. The journey produced 120 transition events.
