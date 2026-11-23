# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim validate``: check a bundle against the FLUX schema and its cross-document rules."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import _BUNDLE_HELP, _add_json_flag
from flux_engine.cli._output import _finish
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "validate": (Example("fluxsim validate ./bundle", "check a bundle; nothing is written"),),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim validate`` to *sub*, the commands of the ``fluxsim`` parser."""
    validate_p = sub.add_parser(
        "validate",
        help="validate a FLUX bundle",
        description=(
            "Check a bundle against the FLUX schema and its cross-document rules. "
            "Reads only; writes nothing."
        ),
    )
    validate_p.add_argument("bundle", help=_BUNDLE_HELP)
    _add_json_flag(validate_p)

    validate_p.set_defaults(stage_name="validate")


def _cmd_validate(args: argparse.Namespace) -> int:
    from flux_engine.stages.validate import validate

    return _finish(args, validate(args.bundle))
