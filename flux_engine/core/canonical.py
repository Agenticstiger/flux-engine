# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The one canonicaliser: NFC -> key coercion -> JCS -> sha256.

Implements KERNEL-SPEC §2.1-§2.2 and the masking rule of §2.4. This
module is the ONLY place in flux_engine allowed to import ``rfc8785``
(import-linter contract "only core.canonical may import rfc8785" in
pyproject.toml) and, by the same rule of one-place-to-get-right, the only
place that imports ``hashlib`` -- KERNEL-SPEC §2's heading names this
module for both. Every digest in the engine routes through
:func:`canonical_bytes` and :func:`sha256_hex`, so a FLUX bundle and its
engine agree on one digest, not two.

The pipeline, in order (KERNEL-SPEC §2.1):

1. recursive NFC normalisation of every ``str`` value **and every key**;
2. coercion of non-``str`` mapping keys to ``str`` (``True`` -> ``"true"``,
   ``1`` -> ``"1"``), because PyYAML yields bool/int keys;
3. rejection (``FLX-1001``) of any ``float`` anywhere in the object --
   values that carry numbers are ``int`` or ``Decimal``; ``Decimal`` is
   serialised as a **string** with its declared scale (``"42.0000"``),
   never as a JSON number;
4. RFC 8785 (JCS) serialisation via ``rfc8785.dumps``.

Steps 1 to 3 are :mod:`flux_stats.canonical`'s, which this module re-exports:
they moved there so that a reader without the engine, such as forge-cli's
profiler, forms the very structure the engine hashes. Step 4 and the digest
stay here.

Everything here is a pure function of its argument: no clock, no
randomness, no environment, and mappings are rebuilt in sorted-key order
so an intermediate structure is as order-stable as the bytes it becomes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Sequence, Tuple

import rfc8785

from flux_engine.core.errors import FluxError
from flux_stats.canonical import (
    FLX_1001,
    FLX_1003,
    FLX_1004,
    FLX_1005,
    FLX_1006,
    FLX_1007,
    JCS_MAX_SAFE_INTEGER,
    canonicalise,
    coerce_key,
)
from flux_stats.reading import nfc

__all__ = [
    "FLX_1001",
    "FLX_1003",
    "FLX_1004",
    "FLX_1005",
    "FLX_1006",
    "FLX_1007",
    "FieldPartition",
    "JCS_MAX_SAFE_INTEGER",
    "NON_DIGEST_FIELDS",
    "SELF_REFERENTIAL_FIELDS",
    "canonical_array_bytes",
    "canonical_bytes",
    "canonicalise",
    "coerce_key",
    "field_paths",
    "mask_document",
    "nfc",
    "partition_fields",
    "sha256_hex",
    "sha256_hex_chunks",
]

#: KERNEL-SPEC §2.4. Wall-clock and identity fields that are written into
#: an artifact but never hashed: two runs over identical inputs must
#: produce one digest, and these five legitimately differ between them.
#: ``flux_engine.core.artifact.ENVELOPE_NON_DIGEST_FIELDS`` is the envelope-only
#: subset of this set; a test pins the subset relation so the two cannot
#: drift apart.
NON_DIGEST_FIELDS: FrozenSet[str] = frozenset(
    {"generated_at", "generated_by", "engineVersion", "hostname", "durationMs"}
)

#: KERNEL-SPEC §2.4, second half: the fields that are themselves a digest
#: *of the document that carries them*. Hashing a document that contains
#: its own hash has no fixed point, so these are masked as well -- the
#: same rule forge-cli's ``plan_digest`` module applies to ``planDigest``.
SELF_REFERENTIAL_FIELDS: FrozenSet[str] = frozenset(
    {"planDigest", "baselineDigest", "outputDigest"}
)


def canonical_bytes(value: Any) -> bytes:
    """Return the RFC 8785 canonical bytes of *value* (KERNEL-SPEC §2.1)."""
    canonical = canonicalise(value)
    try:
        return rfc8785.dumps(canonical)
    except rfc8785.FloatDomainError as exc:  # pragma: no cover - canonicalise rejects floats
        raise FluxError(FLX_1001, str(exc)) from exc
    except rfc8785.IntegerDomainError as exc:  # pragma: no cover - guarded above
        raise FluxError(FLX_1003, str(exc)) from exc
    except rfc8785.CanonicalizationError as exc:  # pragma: no cover - guarded above
        raise FluxError(FLX_1005, str(exc)) from exc


def canonical_array_bytes(values: Sequence[Any]) -> bytes:
    """``canonical_bytes(list(values))``, quicker for a flat array of JSON scalars.

    A row the seam renders (KERNEL-SPEC §9) is a flat array of strings,
    integers, booleans and nulls. For such an array JCS writes exactly what
    the standard library's JSON encoder writes with ``ensure_ascii=False``
    and no whitespace: ``null``, ``true``/``false``, an integer's decimal
    digits, and a string escaped by one rule -- the quote, the backslash,
    ``\\b \\f \\n \\r \\t``, and any other control character as
    ``\\u00XX``. ``rfc8785``'s escape table is the encoder's own table, and
    the two agree on every code point outside the surrogates. Each string is
    NFC'd first, as :func:`canonicalise` does.

    Anything else -- a nested value, a ``Decimal``, an integer outside the
    JCS domain, a subclass of ``str`` or ``int``, a string that is not valid
    UTF-8 -- goes through :func:`canonical_bytes`, so those bytes and every
    refusal are that function's own.
    """
    if not isinstance(values, (list, tuple)):
        # Read once, so an iterator is not half spent when the fallback reads it.
        values = list(values)
    scalars: List[Any] = []
    for value in values:
        kind = type(value)
        if kind is str:
            scalars.append(nfc(value))
        elif kind is int:
            if abs(value) > JCS_MAX_SAFE_INTEGER:
                return canonical_bytes(list(values))
            scalars.append(value)
        elif kind is bool or value is None:
            scalars.append(value)
        else:
            return canonical_bytes(list(values))
    try:
        return json.dumps(scalars, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate: JCS refuses it, and the refusal is canonical_bytes's to make.
        return canonical_bytes(list(values))


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex sha256 of *data*.

    The one hashing primitive in the engine; :mod:`flux_engine.core.digest`
    wraps it into the labelled :class:`~flux_engine.core.digest.DigestRef`
    domains of KERNEL-SPEC §2.3.
    """
    return hashlib.sha256(data).hexdigest()


def sha256_hex_chunks(chunks: Iterable[bytes]) -> str:
    """Return the lowercase hex sha256 of the concatenation of *chunks*.

    :func:`sha256_hex` for data too large to hold at once, such as a file
    an output sink wrote, read back in fixed-size pieces. Equal to
    ``sha256_hex(b"".join(chunks))`` without building the joined bytes.
    """
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def mask_document(
    document: Mapping[str, Any],
    *,
    self_referential: Iterable[str] = SELF_REFERENTIAL_FIELDS,
) -> Dict[str, Any]:
    """Return *document* with every non-digested field removed.

    KERNEL-SPEC §2.4: the mask is :data:`NON_DIGEST_FIELDS` plus the
    self-referential digest fields of the document being hashed. Masking
    is recursive -- a nested block's ``generated_at`` is as volatile as a
    top-level one -- and the result is canonicalised, so the return value
    is ready to hand to :func:`canonical_bytes`.
    """
    masked = NON_DIGEST_FIELDS | frozenset(self_referential)
    result = _mask(canonicalise(document), masked)
    if not isinstance(result, dict):  # pragma: no cover - canonicalise keeps mappings mappings
        raise FluxError(FLX_1005, f"masked document class {type(result).__name__!r}")
    return result


def _mask(value: Any, masked: FrozenSet[str]) -> Any:
    if isinstance(value, dict):
        return {key: _mask(item, masked) for key, item in value.items() if key not in masked}
    if isinstance(value, list):
        return [_mask(item, masked) for item in value]
    return value


def field_paths(value: Any, *, prefix: str = "") -> Tuple[str, ...]:
    """Return every leaf field path in *value*, sorted.

    Paths are dotted; a list contributes one path per distinct leaf below
    it with ``[]`` standing for the index, so ``properties[].tolerance``
    is one path however many properties there are. An empty mapping or
    list is itself a leaf -- it is a field with a value, and a totality
    check that skipped it would under-report.
    """
    canonical = canonicalise(value)
    return tuple(sorted(set(_leaf_paths(canonical, prefix))))


def _leaf_paths(value: Any, prefix: str) -> List[str]:
    if isinstance(value, dict) and value:
        paths: List[str] = []
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else key
            paths.extend(_leaf_paths(item, child))
        return paths
    if isinstance(value, list) and value:
        paths = []
        for item in value:
            paths.extend(_leaf_paths(item, f"{prefix}[]"))
        return paths
    return [prefix] if prefix else []


@dataclass(frozen=True)
class FieldPartition:
    """Every field of a document, split into what is hashed and what is not.

    KERNEL-SPEC §2.4 requires a totality test: "enumerates every receipt
    field and asserts it is either digested or listed". This is the shape
    that test asserts on -- ``digested`` and ``masked`` are disjoint and
    their union is every leaf path in the document, by construction, so a
    field added to a receipt tomorrow lands in exactly one of them and a
    reviewer can see which.
    """

    digested: Tuple[str, ...]
    masked: Tuple[str, ...]

    @property
    def all_fields(self) -> Tuple[str, ...]:
        """Every leaf path in the document, sorted."""
        return tuple(sorted(self.digested + self.masked))


def partition_fields(
    document: Mapping[str, Any],
    *,
    self_referential: Iterable[str] = SELF_REFERENTIAL_FIELDS,
) -> FieldPartition:
    """Split *document*'s leaf fields into digested and masked (KERNEL-SPEC §2.4).

    A path is masked when any segment of it names a field in
    :data:`NON_DIGEST_FIELDS` or in *self_referential*; masking a block
    masks everything under it. Every other leaf path is digested.
    """
    masked_names = NON_DIGEST_FIELDS | frozenset(self_referential)
    digested: List[str] = []
    masked: List[str] = []
    for path in field_paths(document):
        segments = [segment.removesuffix("[]") for segment in path.split(".")]
        if any(segment in masked_names for segment in segments):
            masked.append(path)
        else:
            digested.append(path)
    return FieldPartition(digested=tuple(sorted(digested)), masked=tuple(sorted(masked)))
