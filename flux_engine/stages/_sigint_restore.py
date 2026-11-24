# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The last module a forkserver imports: it gives ``SIGINT`` back its interpreter handler.

See :mod:`flux_engine.stages._sigint_guard`. The server goes on to ignore ``SIGINT`` itself and
to hand each worker the handler it found here, which is what a worker has always had.
"""

import signal

signal.signal(signal.SIGINT, signal.default_int_handler)
