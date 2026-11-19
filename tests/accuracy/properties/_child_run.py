# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Child process for the same-bytes relation: a hostile ambient ``decimal`` context.

Run as ``python _child_run.py <bundle> <state-dir> [<prec> <rounding>]``. The parent
chooses a different ``PYTHONHASHSEED``. When a precision and a rounding mode are
given (``prec`` 0 means "leave the default"), they are set on the main thread's
ambient ``decimal`` context before the engine runs -- the settings most likely to
move a quantised value if any engine arithmetic leaks out of its own pinned context.
Prints the apply envelope's ``run_dir`` as the last line of stdout.
"""

from __future__ import annotations

import contextlib
import decimal
import io
import json
import sys


def _run(argv):
    from flux_engine.cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["-j", *argv])
    envelope = json.loads(buffer.getvalue())
    if code != 0 or not envelope.get("ok"):
        raise SystemExit(f"flux {argv[0]} failed: {envelope.get('error')}")
    return envelope


def child() -> None:
    bundle, state_dir = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 4 and int(sys.argv[3]) > 0:
        context = decimal.getcontext()
        context.prec = int(sys.argv[3])
        context.rounding = getattr(decimal, sys.argv[4])
    planned = _run(["plan", bundle, "--state-dir", state_dir])
    artifacts = planned["artifacts"]
    run_id = str(artifacts.get("run_id") or artifacts["plan_digest"].split(":", 1)[1][:16])
    applied = _run(["apply", bundle, "--state-dir", state_dir, "--run-id", run_id])
    sys.stdout.write(json.dumps({"run_dir": applied["artifacts"]["run_dir"]}) + "\n")


if __name__ == "__main__":
    child()
