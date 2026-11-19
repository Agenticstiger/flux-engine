# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The canonical form a FLUX digest is taken over (KERNEL-SPEC §2.1).

Every string NFC, every mapping key a coerced NFC string, every mapping
rebuilt in sorted-key order, every :class:`~decimal.Decimal` its string
spelling at its declared scale, and no ``float`` anywhere. That structure is
what the engine serialises with RFC 8785 (JCS) and hashes.

It lives here, not in the engine, so that a reader without the engine forms
the same structure the engine hashes: forge-cli's profiler reads a
Simulation's fidelity block into exactly the value the engine parses. The
engine's ``flux_engine.core.canonical`` re-exports these names and keeps the
two steps that need more than the standard library or that make a digest:
the RFC 8785 bytes (``rfc8785``) and the sha256 (``hashlib``). This module,
like the rest of the package, imports the standard library alone.

Everything here is a pure function of its argument: no clock, no randomness,
no environment.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Mapping, Sequence

from flux_stats.errors import FluxError, register
from flux_stats.reading import nfc

__all__ = [
    "FLX_1001",
    "FLX_1003",
    "FLX_1004",
    "FLX_1005",
    "FLX_1006",
    "FLX_1007",
    "JCS_MAX_SAFE_INTEGER",
    "canonicalise",
    "coerce_key",
    "render_decimal",
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


def render_decimal(value: Decimal) -> str:
    """Spell *value* as the string KERNEL-SPEC §2.1 requires.

    The declared scale is preserved (``Decimal("42.0000")`` -> ``"42.0000"``,
    which is a different canonical string from ``Decimal("42")``): the scale
    is part of what the document declared, so it is part of what is hashed.
    Negative zero is folded onto the positive spelling at the same scale,
    because ``-0.00`` and ``0.00`` are one number and must not be two
    digests.

    The spelling is written out in full, so a caller that reads untrusted
    text bounds a number's length first (:mod:`flux_stats.exact_yaml` does,
    at the scalar).
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
        return render_decimal(key)
    raise FluxError(FLX_1004, f"mapping key class {type(key).__name__!r}")


def canonicalise(value: Any) -> Any:
    """Return *value* as JSON-ready Python, per KERNEL-SPEC §2.1.

    The result contains only ``str``, ``int``, ``bool``, ``None``,
    ``list`` and ``dict``; every string is NFC, every mapping key is a
    coerced NFC string, mappings are rebuilt in sorted-key order, and
    every ``Decimal`` has become its string spelling. The function is
    idempotent: canonicalising a canonical structure returns an equal
    structure.

    It writes the structure out in full: a value reached twice is written
    twice. :mod:`flux_stats.exact_yaml` bounds what a document read from
    untrusted text can write out before a caller canonicalises it.
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
        return render_decimal(value)
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
