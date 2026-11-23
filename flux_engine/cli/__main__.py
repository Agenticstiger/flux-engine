# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``python -m flux_engine.cli`` -- the same entry point ``fluxsim`` (the installed
console script) resolves to. Exists so the CLI is invocable without an
installed entry point, which is how ``tests/cli`` drives it as a real
subprocess rather than through an in-process import."""

from __future__ import annotations

import sys

from flux_engine.cli import main

if __name__ == "__main__":
    sys.exit(main())
