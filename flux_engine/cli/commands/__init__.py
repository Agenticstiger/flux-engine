# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One module per ``fluxsim`` command, in the order ``fluxsim --help`` lists them.

Each module defines ``configure(sub)``, which adds its command, and any subcommands, to
the ``fluxsim`` parser and declares the ``stage`` each one reports; ``EXAMPLES``, the
worked invocations its ``--help`` leads with; and the ``_cmd_*`` functions
:func:`flux_engine.cli.main` dispatches to. The layout follows conda's
``conda/cli/main_*.py`` (a ``configure_parser`` and an ``execute`` per command) and pip's
ordered command registry, where the order of the registry is the order ``--help`` shows.
"""

from __future__ import annotations

from flux_engine.cli.commands import (
    accuracy,
    apply,
    calibrate,
    describe,
    explain,
    expr,
    forecast,
    gate,
    init,
    plan,
    plugins,
    replay,
    run,
    ship,
    story,
    validate,
    verify,
)

#: The commands, in the order ``fluxsim --help`` lists them.
COMMANDS = (
    init,
    validate,
    plan,
    apply,
    run,
    verify,
    ship,
    replay,
    story,
    calibrate,
    accuracy,
    forecast,
    gate,
    expr,
    describe,
    plugins,
    explain,
)
