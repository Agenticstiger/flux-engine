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
    "validate": (
        Example("fluxsim validate ./bundle", "check a bundle; nothing is written"),
        Example(
            "fluxsim validate ./bundle --profile profile.json",
            "also refuse what plan refuses about the profile the bundle pins (FLX-3105, 3109, 3110, 3113)",
        ),
    ),
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
    validate_p.add_argument(
        "--profile",
        default=None,
        metavar="PATH",
        help=(
            "the profile document the Simulation pins (docs/spec/PROFILE-SPEC.md): also refuse, "
            "as plan does, an overlay or a parameter of the register that names a table or column "
            "it does not describe, and a profile that is not the pinned one (FLX-3105, 3109, "
            "3110, 3113); a profile given to a Simulation that pins none and the grade floor "
            "(FLX-4005) are left to plan; without it no profile is read"
        ),
    )
    _add_json_flag(validate_p)

    validate_p.set_defaults(stage_name="validate")


def _cmd_validate(args: argparse.Namespace) -> int:
    from flux_engine.stages.validate import validate

    return _finish(args, validate(args.bundle, profile=args.profile))
