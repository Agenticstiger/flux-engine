# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How many RFC-04 conformance vectors flux-spec publishes, as the engine's tests check it.

One constant, so a flux-spec release that adds vectors moves one line. A test that finds
another count fails with :data:`TRIPWIRE`: the vectors changed upstream, so read what the new
ones decide before moving the number.
"""

#: The vectors in flux-spec's ``enforcement-vectors.json``.
VECTOR_COUNT = 37

#: What a test says when flux-spec's vectors are not :data:`VECTOR_COUNT`.
TRIPWIRE = "the RFC-04 conformance vector count changed upstream"
