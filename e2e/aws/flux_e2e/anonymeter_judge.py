# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Anonymeter's singling out, linkability and inference attacks, as the privacy judge.

Anonymeter is not a dependency of this harness and never becomes one. Its licence
(Clear BSD) grants no patent rights, and its code is patent-pending. Install
``anonymeter`` (and ``pyarrow``) into a private environment of your own and run
this file with that interpreter, as a single file::

    <private-python> -I anonymeter_judge.py --ori ORI --syn SYN --control CONTROL \
        --spec spec.json --out privacy.json

``ori`` is the person view of the data FLUX was built from, ``syn`` the synthetic
copy's and ``control`` the held-out people's. ``ori`` is sampled down to the control
set's size, so that the two are compared at one size. Each attack's risk is Anonymeter's
``(main - control) / (1 - control)`` with its confidence interval, next to the
success rates of the main, baseline (random guess) and control attacks. An attack
that does no better than its baseline cannot be trusted, and says so.

Criteria: singling out tests isolation, linkability tests linkage and inference
tests inference, as in the EDPB's anonymisation guidance and the Article 29
Working Party's Opinion 05/2014.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

CRITERIA = {
    "singlingOut": "isolation (singling out)",
    "linkability": "linkage (linkability)",
    "inference": "inference",
}
CONFIDENCE = 0.95


def _rate(rate: Any) -> Optional[Dict[str, float]]:
    return None if rate is None else {"value": float(rate.value), "error": float(rate.error)}


def summarise(evaluator: Any, **kwargs: Any) -> Dict[str, Any]:
    """One attack's risk, interval, success rates and baseline check."""
    risk = evaluator.risk(confidence_level=CONFIDENCE, **kwargs)
    results = evaluator.results(confidence_level=CONFIDENCE, **kwargs)
    attack, baseline = _rate(results.attack_rate), _rate(results.baseline_rate)
    return {
        "risk": float(risk.value),
        "ci": [float(bound) for bound in risk.ci],
        "attack": attack,
        "baseline": baseline,
        "control": _rate(results.control_rate),
        "beatsBaseline": attack["value"] > baseline["value"],
    }


class _Messages(logging.Handler):
    """Anonymeter reports a query shortfall through logging, not warnings."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.seen: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append(record.getMessage()[:200])


def _attack(name: str, run: Callable[[], Dict[str, Any]], out: Dict[str, Any]) -> None:
    """Run one attack; an attack that cannot run is recorded, and the rest still run."""
    started = time.monotonic()
    messages = _Messages()
    logging.getLogger().addHandler(messages)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            outcome = run()
        except Exception as error:  # one attack failing must not lose the others
            outcome = {"error": f"{type(error).__name__}: {str(error)[:300]}"}
        finally:
            logging.getLogger().removeHandler(messages)
    outcome["warnings"] = sorted({str(item.message)[:200] for item in caught} | set(messages.seen))
    outcome["seconds"] = round(time.monotonic() - started, 1)
    out[name] = outcome


#: The smallest control set the attacks are run against.
MIN_CONTROL = 100


def evaluate(ori: Any, syn: Any, control: Any, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Every attack against the synthetic copy, and the same attack against a verbatim copy.

    The verbatim copy (the sampled built-from rows themselves, standing in for the
    synthetic copy) is the positive control: its risk is what a generator that
    memorised its input would score with these attacks at this size, so a low risk
    for the synthetic copy can be read against it. It is the Anonymeter paper's
    "leaky synthesiser" with every record leaked (Giomi et al., PoPETs 2023).
    """
    import numpy as np
    from anonymeter.evaluators import (
        InferenceEvaluator,
        LinkabilityEvaluator,
        SinglingOutEvaluator,
    )

    if len(control) < MIN_CONTROL or len(ori) < MIN_CONTROL or len(syn) == 0:
        raise ValueError(
            f"too few rows to attack: {len(ori)} built-from, {len(syn)} synthetic, "
            f"{len(control)} held out (at least {MIN_CONTROL} each)"
        )
    seed = int(spec["seed"])
    out: Dict[str, Any] = {"criteria": CRITERIA, "confidence": CONFIDENCE, "attacks": {}}
    # Singling out succeeds more often in a larger set. Anonymeter corrects for a
    # control set of another size by extrapolating a curve fitted on samples of the
    # control set (appendix B of the paper), and an extrapolation is an estimate. So
    # the larger of the two real sides is sampled down to the smaller one's size for
    # every attack.
    if len(ori) > len(control):
        ori = ori.sample(n=len(control), random_state=seed).reset_index(drop=True)
        out["oriSampledTo"] = len(control)
    elif len(control) > len(ori):
        control = control.sample(n=len(ori), random_state=seed).reset_index(drop=True)
        out["controlSampledTo"] = len(ori)
    # Linkability samples its targets without replacement from both real sides.
    attacks = min(int(spec["attacks"]), len(control), len(ori))
    singling = min(attacks, 500)
    out["attacksUsed"] = {"singlingOut": singling, "linkability": attacks, "inference": attacks}
    copy = ori.copy()

    def both(build: Callable[[Any], Any]) -> Callable[[], Dict[str, Any]]:
        def run() -> Dict[str, Any]:
            # Anonymeter samples its targets with numpy's global generator.
            np.random.seed(seed)
            outcome = summarise(build(syn))
            np.random.seed(seed)
            try:
                verbatim = summarise(build(copy))
                outcome["verbatimCopy"] = {"risk": verbatim["risk"], "ci": verbatim["ci"]}
            except Exception as error:  # the synthetic copy's result stands on its own
                outcome["verbatimCopy"] = {"error": f"{type(error).__name__}: {str(error)[:300]}"}
            return outcome

        return run

    for mode in ("univariate", "multivariate"):
        _attack(
            f"singlingOut.{mode}",
            both(
                lambda target, mode=mode: SinglingOutEvaluator(
                    ori, target, n_attacks=singling, control=control, seed=seed
                ).evaluate(mode=mode)
            ),
            out["attacks"],
        )
    for name, key in (("linkability", "linkageSets"), ("linkability.wide", "wideLinkageSets")):
        if not spec.get(key):
            continue  # a spec written before the wide sets existed
        first, second = (list(columns) for columns in spec[key])
        _attack(
            name,
            both(
                lambda target, first=first, second=second: LinkabilityEvaluator(
                    ori, target, aux_cols=(first, second), n_attacks=attacks, control=control
                ).evaluate(n_jobs=-2)
            ),
            out["attacks"],
        )
    auxiliary: Sequence[str] = spec["auxiliary"]
    for secret, regression in spec["secrets"].items():
        _attack(
            f"inference.{secret}",
            both(
                lambda target, secret=secret, regression=regression: InferenceEvaluator(
                    ori,
                    target,
                    aux_cols=list(auxiliary),
                    secret=secret,
                    regression=regression,
                    n_attacks=attacks,
                    control=control,
                ).evaluate(n_jobs=-2)
            ),
            out["attacks"],
        )
    out["headline"] = headline(out["attacks"])
    return out


def headline(attacks: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Per criterion, the highest risk among its attacks that beat their baseline.

    When no attack of a criterion beat its baseline, the highest risk is reported
    with ``beatsBaseline: false``. The interval is the chosen attack's own and is not
    adjusted for choosing the highest of several. ``of`` counts the criterion's
    attacks and ``failed`` those that could not run, which the choice could not see.
    A criterion none of whose attacks ran is kept, with no risk.
    """
    best: Dict[str, Any] = {}
    for criterion in sorted({name.split(".", 1)[0] for name in attacks}):
        mine = [
            (name, outcome)
            for name, outcome in attacks.items()
            if name.split(".", 1)[0] == criterion
        ]
        scored = [(name, outcome) for name, outcome in mine if "risk" in outcome]
        if not scored:
            best[criterion] = {
                "attack": None,
                "risk": None,
                "ci": None,
                "beatsBaseline": None,
                "verbatimCopy": None,
                "of": len(mine),
                "failed": len(mine),
            }
            continue
        trusted = [item for item in scored if item[1]["beatsBaseline"]] or scored
        name, outcome = max(trusted, key=lambda item: item[1]["risk"])
        best[criterion] = {
            "attack": name,
            "risk": outcome["risk"],
            "ci": outcome["ci"],
            "beatsBaseline": outcome["beatsBaseline"],
            "verbatimCopy": outcome.get("verbatimCopy"),
            "of": len(mine),
            "failed": len(mine) - len(scored),
        }
    return best


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for side in ("ori", "syn", "control"):
        parser.add_argument(f"--{side}", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    from importlib.metadata import version

    import pandas as pd

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    columns = spec["columns"]
    frames = {
        side: pd.read_parquet(getattr(args, side))[columns] for side in ("ori", "syn", "control")
    }
    try:
        out = evaluate(frames["ori"], frames["syn"], frames["control"], spec)
    except ValueError as error:
        print(f"anonymeter_judge: {error}", file=sys.stderr)
        return 2
    out["anonymeter"] = version("anonymeter")
    out["rows"] = {side: len(frame) for side, frame in frames.items()}
    args.out.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
