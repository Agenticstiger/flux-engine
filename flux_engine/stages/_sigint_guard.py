# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The first module a forkserver imports: it ignores ``SIGINT`` until the heavy imports are done.

``multiprocessing.forkserver.main`` imports its preload modules and only afterwards sets
``SIGINT`` to be ignored, so a Ctrl-C in the first half second of ``fluxsim run --jobs N``
interrupted the server in the middle of an import and printed a ``KeyboardInterrupt``
traceback. This module closes that window. :mod:`flux_engine.stages._sigint_restore`, imported
last, puts the interpreter's own handler back, so the handler the server saves and restores in
each worker is the one it always saved.

It imports the standard library only.
"""

import signal

signal.signal(signal.SIGINT, signal.SIG_IGN)
