# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Generator plugins: which entry point may load, and nothing that loads one.

:mod:`flux_engine.generators.registry` lists the entry points of the
``flux_engine.generators`` group without importing them, resolves a declared
plugin to exactly one installed entry point, and loads it only after its
origin has been checked. Standard library only.
"""
