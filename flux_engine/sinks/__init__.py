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
* :mod:`flux_engine.sinks.parquet` and :mod:`flux_engine.sinks.arrow_ipc` --
  one stream as zstd-compressed files named after its ``outputDigest``,
  with a manifest of their sha256, published only when the stream is whole.
  Need ``flux-sim-engine[arrow]``.
* :mod:`flux_engine.sinks.receipts` -- what a sink is told before a stream
  (:class:`~flux_engine.sinks.receipts.StreamContext`) and what it reports
  after one (:class:`~flux_engine.sinks.receipts.SinkReceipt`). No extra.

Every module that needs an optional library imports it when the module
itself is imported, through :func:`flux_engine.sinks._optional.require`,
which fails with ``FLX-3501`` and the install line when the extra is
missing. This package's own ``__init__`` imports nothing optional, so the
engine's core and its CLI's cold path stay free of pyarrow.
"""
