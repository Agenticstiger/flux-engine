# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim gate``: the RFC-04 enforcement gate (decide, conform)."""

from __future__ import annotations

import argparse
import json
from typing import Dict, Tuple

from flux_engine.cli._args import _add_json_flag
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "gate": (Example("fluxsim gate conform", "run the published conformance vectors"),),
    "gate decide": (
        Example("fluxsim gate decide --request request.json", "exit 0 allowed, 3 denied"),
    ),
    "gate conform": (Example("fluxsim gate conform", "run the published conformance vectors"),),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim gate`` to *sub*, the commands of the ``fluxsim`` parser."""
    from flux_engine.cli._documents import GATE_REQUEST_FIELDS, field_list_help

    gate_p = sub.add_parser(
        "gate",
        help="RFC-04 enforcement gate: decide, conform",
        description="Decide one AI-call request against a policy, or run the conformance vectors.",
    )
    gate_sub = gate_p.add_subparsers(dest="gate_command", title="commands", metavar="<command>")
    decide_p = gate_sub.add_parser(
        "decide",
        help="decide one AI-call request against a policy",
        description=(
            "Decide one request against one policy. Exit 0 allowed, 3 denied, 1 the "
            "request could not be decided at all."
        ),
    )
    decide_p.add_argument(
        "--request",
        required=True,
        help=f"the request to decide, as JSON -- {field_list_help(GATE_REQUEST_FIELDS)}",
    )
    _add_json_flag(decide_p)
    conform_p = gate_sub.add_parser(
        "conform",
        help="run the RFC-04 conformance vectors against the enforcement gate",
        description="Run the published RFC-04 conformance vectors and report the pass count.",
    )
    conform_p.add_argument(
        "--vectors",
        default=None,
        help="path to an RFC-04 vectors file (default: the published set)",
    )
    _add_json_flag(conform_p)

    decide_p.set_defaults(stage_name="gate-decide")
    conform_p.set_defaults(stage_name="gate-conform")


#: Distinct from the usage-error/run-failure convention (2/1/0): a
#: `gate decide` decision is a *policy answer*, not a command outcome --
#: "the policy said no" and "this command could not run" are different
#: facts, and conflating them under exit 1 would make a caller
#: unable to tell a denial from a crash without reading stderr. `fluxsim gate
#: decide` reserves exit code 3 for a well-formed, successfully-computed
#: denial; 1 stays for a command that could not compute an answer at all
#: (a malformed request document, a policy shape error).
_GATE_DECIDE_DENIED_EXIT_CODE = 3


def _cmd_gate_decide(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.cli._documents import (
        GATE_REQUEST_FIELDS,
        DocumentProblems,
        read_json_object,
    )
    from flux_engine.governance.gate import decide

    try:
        document = read_json_object(
            args.request, what="the gate request", fields=GATE_REQUEST_FIELDS
        )
    except DocumentProblems as problems:
        # No FLX code yet: the governance band's codes are owned by
        # `flux_engine.governance.gate`, which this CLI does not write. The
        # message is the fix that matters -- every problem at once, and the
        # required field list is in `--request`'s own --help.
        return _print_result(
            StageResult(stage="gate-decide", exit_code=1, stderr=str(problems)),
            as_json=args.json,
        )
    decision = decide(dict(document["policy"]), dict(document["request"]), document.get("skill"))
    allowed = bool(decision.get("allow"))
    return _print_result(
        StageResult(
            stage="gate-decide",
            exit_code=0 if allowed else _GATE_DECIDE_DENIED_EXIT_CODE,
            _artifacts=dict(decision),
            stdout=json.dumps(decision, indent=2, sort_keys=True),
        ),
        as_json=args.json,
    )


def _cmd_gate_conform(args: argparse.Namespace) -> int:
    from pathlib import Path

    from flux_engine.api.stages import StageResult
    from flux_engine.governance.gate import run_conformance_vectors
    from flux_engine.spec import default_vectors_path

    vectors_path = Path(args.vectors) if args.vectors else default_vectors_path()
    total = len(json.loads(vectors_path.read_text())["vectors"])
    failures, messages = run_conformance_vectors(vectors_path)

    if failures:
        return _print_result(
            StageResult(
                stage="gate-conform",
                exit_code=1,
                _artifacts={"total": total, "failures": failures, "messages": messages},
                stderr="; ".join(
                    [
                        *(f"FAIL: {m}" for m in messages),
                        f"{total - failures}/{total} RFC-04 conformance vectors passed",
                    ]
                ),
            ),
            as_json=args.json,
        )

    return _print_result(
        StageResult(
            stage="gate-conform",
            exit_code=0,
            _artifacts={"total": total, "failures": 0, "messages": []},
            stdout=f"{total}/{total} RFC-04 conformance vectors passed",
        ),
        as_json=args.json,
    )
