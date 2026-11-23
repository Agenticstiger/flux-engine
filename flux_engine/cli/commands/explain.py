# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim explain``: print the catalog entry for one ``FLX-nnnn`` code, or list them all."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine._links import error_url
from flux_engine.cli._args import _add_json_flag
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "explain": (
        Example("fluxsim explain FLX-1001", "print one code's explanation"),
        Example("fluxsim explain --list", "list every code"),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim explain`` to *sub*, the commands of the ``fluxsim`` parser."""
    explain_p = sub.add_parser(
        "explain",
        help="print the catalog entry for one FLX-nnnn code, or list them all",
        description=(
            "Print the title and operator-facing explanation of one FLX-nnnn code. "
            "With --list, print every registered code instead, so the catalog is "
            "discoverable without grepping the source."
        ),
    )
    explain_p.add_argument(
        "code",
        nargs="?",
        default=None,
        help="the FLX-nnnn code to explain (case-insensitive); omit it with --list",
    )
    explain_p.add_argument(
        "--list",
        dest="list_codes",
        action="store_true",
        help="print every registered code as `code  [family]  title`",
    )
    _add_json_flag(explain_p)

    explain_p.set_defaults(stage_name="explain")


def _cmd_explain(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.core.errors import (
        catalog,
        ensure_core_codes_loaded,
        explain,
        is_registered,
    )

    ensure_core_codes_loaded()
    if args.list_codes:
        entries = catalog()
        return _print_result(
            StageResult(
                stage="explain",
                exit_code=0,
                _artifacts={
                    "codes": [
                        {"code": entry.code, "family": entry.category, "title": entry.title}
                        for entry in entries
                    ]
                },
                stdout="\n".join(
                    f"{entry.code}  [{entry.category}]  {entry.title}" for entry in entries
                ),
            ),
            as_json=args.json,
        )
    if args.code is None:
        return _print_result(
            StageResult(
                stage="explain",
                exit_code=2,
                stderr=(
                    "explain takes an FLX-nnnn code, or --list for every registered one; "
                    "for example `fluxsim explain FLX-3105`"
                ),
            ),
            as_json=args.json,
        )
    # Upper-cased and stripped before the lookup: `flx-3105` is the same
    # code as `FLX-3105`, and answering "not a registered FLX code" to a
    # code the catalog holds sent readers hunting for a code that exists.
    code = args.code.strip().upper()
    if not is_registered(code):
        return _print_result(
            StageResult(
                stage="explain",
                exit_code=1,
                # `requestedCode`, not `code`: the code the user asked about
                # is this command's INPUT. Reporting it as `code` made a
                # machine consumer record the unregistered code as the reason
                # that same code could not be explained.
                _artifacts={"requestedCode": args.code},
                stderr=(
                    f"{args.code} is not a registered FLX code; "
                    "see `fluxsim explain --list` for every one that is"
                ),
            ),
            as_json=args.json,
        )
    return _print_result(
        StageResult(
            stage="explain",
            exit_code=0,
            _artifacts={"code": code},
            stdout=f"{explain(code)}\n\n{error_url(code)}",
        ),
        as_json=args.json,
    )
