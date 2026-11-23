# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The copula key of an emit's fidelity block (ADR 0035 decision 4).

An emit may declare ``copula`` inside its fidelity block, beside ``columns`` and ``properties``:
which of its number or timestamp columns are drawn together, and the rank correlations between
them. The key changes the rows the emit generates, so it lives in the block the digests of the
emit already cover: ``H`` and ``G`` both take it with no rule of their own
(:func:`flux_engine.seam.emit.strip_excluded_from_fidelity_block` copies every key it does not
name), and a committed state made under one copula is not reused under another.

The declaration is read and checked by the generator (:mod:`flux_engine.generator.copula`), which
knows the emit's columns. The fidelity check's grammar is the profiler's and does not carry the key
(a block that held it would be refused as an unknown one), so the block the check reads is
:func:`fidelity_without_copula`'s.
"""

from __future__ import annotations

from typing import Any, Mapping

from flux_engine.core.canonical import nfc

__all__ = ["COPULA_KEY", "fidelity_without_copula"]

#: The key of the fidelity block that declares a copula.
COPULA_KEY = "copula"


def fidelity_without_copula(block: Any) -> Any:
    """The fidelity *block* without its ``copula`` key; any other value is returned as it is."""
    if not isinstance(block, Mapping) or not any(_is_copula(key) for key in block):
        return block
    return {key: value for key, value in block.items() if not _is_copula(key)}


def _is_copula(key: Any) -> bool:
    return isinstance(key, str) and nfc(key) == COPULA_KEY
