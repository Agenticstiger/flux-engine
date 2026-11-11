# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Output sinks: where a simulation's emitted rows land beyond the run directory.

The run directory always holds each stream as NDJSON, the digest's own
pre-image. The modules here put the same rows into columnar files and
databases, fast and exactly, without ever touching what the run is
answerable for: KERNEL-SPEC §8 computes the ``outputDigest`` above the sink.

* :mod:`flux_engine.sinks.arrow` -- rendered rows to typed Arrow batches and
  back, and :func:`~flux_engine.sinks.arrow.output_digest_of` to re-digest
  what a destination holds. Needs ``flux-sim-engine[arrow]``.

Every module that needs an optional library imports it when the module
itself is imported, through :func:`flux_engine.sinks._optional.require`,
which fails with ``FLX-3501`` and the install line when the extra is
missing. This package's own ``__init__`` imports nothing optional, so the
engine's core and its CLI's cold path stay free of pyarrow.
"""
