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

Everything here is a pure function of its argument: no clock, no
randomness, no environment, and mappings are rebuilt in sorted-key order
so an intermediate structure is as order-stable as the bytes it becomes.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Sequence, Tuple

import rfc8785

from flux_engine.core.errors import FluxError, register

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

FLX_1001 = register(
    "FLX-1001",
    "float value in a document being canonicalised",
    "A binary floating-point number reached the canonicaliser. Floats are not "
    "reproducible across platforms and libm versions, so no hashed value may "
    "contain one (KERNEL-SPEC §2.1). Carry the number as an int or as a "
    "decimal.Decimal, which is serialised as a string at its declared scale.",
)

FLX_1003 = register(
    "FLX-1003",
    "integer outside the JSON safe-integer domain",
    "RFC 8785 serialises JSON numbers through the IEEE 754 double domain, so "
    "an integer with magnitude above 2^53-1 has no canonical spelling. Carry "
    "a value this large as a decimal.Decimal (serialised as a string) or as "
    "its hex or base64 encoding.",
)

FLX_1004 = register(
    "FLX-1004",
    "mapping key cannot be coerced to a string",
    "KERNEL-SPEC §2.1 coerces the non-string mapping keys PyYAML can produce "
    "-- bool, int, null, Decimal -- to their JSON spelling. A key of any "
    "other class has no defined spelling and is refused rather than guessed.",
)

FLX_1005 = register(
    "FLX-1005",
    "value class has no canonical JSON form",
    "Only str, int, bool, None, Decimal, mappings and sequences reach the "
    "canonicaliser. Anything else (bytes, sets, datetimes, engine objects) "
    "must be rendered to one of those first -- see KERNEL-SPEC §9 for the "
    "rendering of each logical type.",
)

FLX_1006 = register(
    "FLX-1006",
    "non-finite Decimal in a document being canonicalised",
    "A NaN, sNaN or Infinity Decimal has no JSON spelling and no ordering, "
    "so it can carry no digest. Refuse it where it is produced rather than "
    "hashing a placeholder.",
)

FLX_1007 = register(
    "FLX-1007",
    "two mapping keys collide after canonicalisation",
    "NFC normalisation and key coercion (KERNEL-SPEC §2.1) can map two "
    "distinct source keys onto one canonical key -- for example True and "
    "'true', or the composed and decomposed spellings of the same word. "
    "Merging them would silently drop a value, so the document is refused.",
)

#: The largest magnitude an integer may have and still have a canonical
#: JCS spelling (RFC 8785 defers number serialisation to the IEEE 754
#: double domain).
JCS_MAX_SAFE_INTEGER = 2**53 - 1

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


def nfc(text: str) -> str:
    """Return *text* in Unicode Normalization Form C (KERNEL-SPEC §2.1).

    Applied to every string **and every key**: the composed and decomposed
    spellings of the same word are the same word, and a digest that
    disagreed about that would be a digest of the input's encoding rather
    than of its content.
    """
    return unicodedata.normalize("NFC", text)


def _render_decimal(value: Decimal) -> str:
    """Spell *value* as the string KERNEL-SPEC §2.1 requires.

    The declared scale is preserved (``Decimal("42.0000")`` -> ``"42.0000"``,
    which is a different canonical string from ``Decimal("42")``): the scale
    is part of what the document declared, so it is part of what is hashed.
    Negative zero is folded onto the positive spelling at the same scale,
    because ``-0.00`` and ``0.00`` are one number and must not be two
    digests.
    """
    if not value.is_finite():
        raise FluxError(FLX_1006, f"Decimal is {value.to_eng_string()}")
    rendered = format(value, "f")
    if rendered.startswith("-") and value == 0:
        rendered = rendered[1:]
    return rendered


def coerce_key(key: Any) -> str:
    """Return the canonical string spelling of a mapping *key*.

    KERNEL-SPEC §2.1: PyYAML yields ``bool``/``int`` keys for the YAML
    magic words and for bare numerics, so those are coerced to their JSON
    spelling (``True`` -> ``"true"``, ``1`` -> ``"1"``) rather than
    rejected. A ``float`` key is a float (``FLX-1001``); any other class
    has no defined spelling (``FLX-1004``).
    """
    if isinstance(key, str):
        return nfc(key)
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, float):
        raise FluxError(FLX_1001, "mapping key is a float")
    if isinstance(key, int):
        return str(key)
    if key is None:
        return "null"
    if isinstance(key, Decimal):
        return _render_decimal(key)
    raise FluxError(FLX_1004, f"mapping key class {type(key).__name__!r}")


def canonicalise(value: Any) -> Any:
    """Return *value* as JSON-ready Python, per KERNEL-SPEC §2.1.

    The result contains only ``str``, ``int``, ``bool``, ``None``,
    ``list`` and ``dict``; every string is NFC, every mapping key is a
    coerced NFC string, mappings are rebuilt in sorted-key order, and
    every ``Decimal`` has become its string spelling. The function is
    idempotent: canonicalising a canonical structure returns an equal
    structure.
    """
    if isinstance(value, bool):
        # Before the int branch: bool is a subclass of int, and JSON
        # spells the two differently.
        return value
    if isinstance(value, float):
        raise FluxError(FLX_1001, "float value")
    if isinstance(value, int):
        if abs(value) > JCS_MAX_SAFE_INTEGER:
            raise FluxError(
                FLX_1003, f"integer of {value.bit_length()} bits exceeds the JCS domain"
            )
        return value
    if isinstance(value, Decimal):
        return _render_decimal(value)
    if isinstance(value, str):
        return nfc(value)
    if value is None:
        return None
    if isinstance(value, Mapping):
        return _canonicalise_mapping(value)
    if isinstance(value, (bytes, bytearray)):
        # Explicitly ahead of the Sequence branch: bytes are a sequence of
        # ints, and silently hashing them as one would be a rendering
        # decision this module has no business making (KERNEL-SPEC §9
        # renders BYTES as base64 at the seam).
        raise FluxError(FLX_1005, f"value class {type(value).__name__!r}")
    if isinstance(value, Sequence):
        return [canonicalise(item) for item in value]
    raise FluxError(FLX_1005, f"value class {type(value).__name__!r}")


def _canonicalise_mapping(mapping: Mapping[Any, Any]) -> Dict[str, Any]:
    canonical: Dict[str, Any] = {}
    for raw_key, raw_value in mapping.items():
        key = coerce_key(raw_key)
        if key in canonical:
            raise FluxError(FLX_1007, f"canonical key {key!r} is produced by two source keys")
        canonical[key] = canonicalise(raw_value)
    # Sorted-key iteration order (KERNEL-SPEC §7's "iteration over any
    # mapping is by sorted key"). JCS re-sorts by UTF-16 code unit when it
    # serialises, so this does not decide the bytes -- it decides what a
    # caller that walks the intermediate structure sees.
    return {key: canonical[key] for key in sorted(canonical)}


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
