# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shared machinery for the accuracy program: tiers, seeds, the result log, the statistics.

Nothing here is engine code and nothing under ``flux_engine/`` imports it. The
modules that need the test-only ``accuracy`` dependency group (scipy) import
it themselves; :mod:`.tier`, :mod:`.deps` and :mod:`.record` stay
standard-library only, so ``tests/accuracy/conftest.py`` can load them in an
environment that does not have the group installed.
"""
