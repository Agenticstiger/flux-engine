# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §11 as two properties, not two examples.

``inputDigest`` = ``jcs-document`` digest of ``{"kernelVersion",
"seedTreeVersion", "traceVersion", "documents": {path: raw-bytes digest,
sorted}, "fluidDocuments": {...}, "lock": ..., "attributeModelDigest"}``.

Two consequences of that construction carry the replay promise, and both
are quantified over inputs rather than demonstrated on one:

1. **Byte sensitivity.** Any difference in any document's bytes moves the
   digest -- including a difference no parser would keep, such as a
   comment or trailing whitespace. There is no exemption for "inert"
   bytes; the raw-bytes leaf of §11 is what makes the promise checkable
   at all, and ``tests/acceptance/test_one_byte.py`` pins what that costs
   (a comment-only edit moves ``runDigest`` while moving no component of
   the run).
2. **Insertion-order invariance.** The mapping is iterated by sorted key
   (§11 "sorted"), so the order a caller happened to build it in is not
   in the digest. A dict-order-sensitive digest would make replay depend
   on filesystem enumeration order.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core.digest import build_input_digest

pytestmark = pytest.mark.property

_KERNEL_VERSION = "0.1.0"
_SEED_TREE_VERSION = "flux-seed/1"
_TRACE_VERSION = "flux-trace/1"

_PATHS = ("a.flux.yml", "b.flux.yml", "c.flux.yml")

#: Bytes a YAML parser would and would not keep, deliberately mixed: the
#: property must hold for both.
_DOCUMENT_BYTES = st.sampled_from(
    [
        b"kind: World\n",
        b"kind: World \n",  # trailing space: parses identically
        b"# a comment\nkind: World\n",  # comment: parses identically
        b"kind: World\n\n",
        b"kind: Simulation\n",
    ]
)


def _input_digest(documents: Mapping[str, bytes]) -> str:
    arguments: Dict[str, Any] = {
        "kernel_version": _KERNEL_VERSION,
        "seed_tree_version": _SEED_TREE_VERSION,
        "trace_version": _TRACE_VERSION,
        "documents": dict(documents),
        "fluid_documents": {"payment.fluid.yml": b"kind: DataProduct\n"},
        "lock": None,
        "attribute_model_digest": None,
    }
    return str(build_input_digest(**arguments).ref.value)


@settings(max_examples=100, deadline=None)
@given(
    left=st.dictionaries(st.sampled_from(_PATHS), _DOCUMENT_BYTES, min_size=1),
    right=st.dictionaries(st.sampled_from(_PATHS), _DOCUMENT_BYTES, min_size=1),
)
def test_input_digest_is_equal_exactly_when_the_bytes_are(
    left: Dict[str, bytes], right: Dict[str, bytes]
) -> None:
    """KERNEL-SPEC §11: the digest is a function of the document bytes, and
    of nothing else -- so it agrees iff the ``{path: bytes}`` maps agree.

    The "only if" half is the one the replay promise needs: a differing
    map must not collide. The "if" half rules out a hidden input (a path
    string's identity, a build-time value) sneaking into the digest.
    """
    assert (_input_digest(left) == _input_digest(right)) == (left == right)


@settings(max_examples=100, deadline=None)
@given(entries=st.dictionaries(st.sampled_from(_PATHS), _DOCUMENT_BYTES, min_size=1))
def test_input_digest_ignores_the_order_the_mapping_was_built_in(
    entries: Dict[str, bytes],
) -> None:
    """KERNEL-SPEC §11 spells the maps "sorted": digest the same documents,
    inserted in the reverse order, and the digest must not move.
    """
    reversed_insertion = {path: entries[path] for path in reversed(list(entries))}
    assert list(reversed_insertion) == list(reversed(list(entries)))
    assert _input_digest(entries) == _input_digest(reversed_insertion)
