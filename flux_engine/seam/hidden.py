# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The hidden columns and the hidden table an emit declares (ADR 0024 sections 2 and 3).

``hidden: true`` in an emit's block makes the emit a hidden table: it is planned and generated as
any emit and never written, and a path through a link reads its rows. The key takes only ``true``
(:func:`read_hidden_value`). Like ``hiddenColumns`` it is part of the emit's block, so it is in
``H`` when declared, and ``G`` leaves it out.

The hidden columns of an emit are declared with ``hiddenColumns``.

An emit declares them in the FLUX bundle, in its block under the namespace, and never in the
FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        emits:
          customer:
            hiddenColumns:
              - { name: churn_date, type: DATE }

An entry is a ``{name, type}`` mapping: a column the expose's contract does not declare, of a
FLUID logical type written as the contract writes one. Under kernel 0.2.0 the column is planned as
a contract column is, after the contract's columns and in the order listed. It has statistics in
the emit's ``fidelity`` block, a rule may set it, a path may read it, and it draws on the cell path
a contract column of that name would. It is never written: no stream, no header, no
``outputDigest`` and no sink holds it.

**The block changes the rows the emit generates**, and the emit's block is hashed whole into ``H``
(:func:`flux_engine.seam.emit.seam_digest` quotes it as the emit's ``extension``), so the key is in
``H`` when it is declared and a bundle that declares none hashes exactly as before. ``G`` leaves the
member out, as it leaves out ``links``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.seam.contract import CompiledColumn, CompiledExpose, parse_logical_type
from flux_engine.seam.copula import fidelity_without_copula

__all__ = [
    "HIDDEN_COLUMNS_KEY",
    "HIDDEN_KEY",
    "HiddenColumn",
    "fidelity_without_hidden",
    "hidden_columns_of",
    "read_hidden_columns",
    "read_hidden_value",
    "with_hidden_columns",
    "with_hidden_in_schema",
]

#: The key of an emit's block that declares hidden columns, and the member an emit's ``G`` leaves out.
HIDDEN_COLUMNS_KEY = "hiddenColumns"

#: The key of an emit's block that makes the emit a hidden table, and the member ``G`` leaves out.
HIDDEN_KEY = "hidden"

_ENTRY_KEYS = ("name", "type")
_MOST_HIDDEN_COLUMNS = 1024


@dataclass(frozen=True)
class HiddenColumn:
    """One hidden column: its name in NFC and the type text as declared."""

    name: str
    type: str


def _entry(item: Any, at: str, taken: Set[str], code: ErrorCode) -> HiddenColumn:
    """One ``{name, type}`` entry, or a refusal with *code*."""
    if not isinstance(item, Mapping):
        raise FluxError(code, f"{at} is a mapping with name and type")
    unknown = sorted(str(key) for key in item if str(key) not in _ENTRY_KEYS)
    if unknown:
        raise FluxError(
            code, f"{at} declares {unknown}; a hidden column carries {list(_ENTRY_KEYS)}"
        )
    missing = [key for key in _ENTRY_KEYS if key not in item]
    if missing:
        raise FluxError(code, f"{at} leaves out {missing}; a hidden column has a name and a type")
    raw = item["name"]
    if not isinstance(raw, str) or not raw.strip():
        raise FluxError(code, f"{at}.name is a non-empty string")
    name = nfc(raw)
    if name in taken:
        raise FluxError(code, f"{at}.name {name!r} is a column the expose or an earlier entry has")
    try:
        parse_logical_type(item["type"])
    except FluxError as error:
        raise FluxError(
            code, f"{at}.type is not a FLUID logical type this engine reads: {error.detail}"
        ) from error
    return HiddenColumn(name=name, type=str(item["type"]))


def read_hidden_columns(
    block: Any, *, contract_columns: Sequence[str], where: str, code: ErrorCode
) -> Tuple[HiddenColumn, ...]:
    """Read a ``hiddenColumns`` block, or refuse it with *code*, naming where.

    *contract_columns* is the names of the columns the emit's contract declares. Refused: a block
    that is not a list, an empty list, more than 1 024 entries, an entry that is not a mapping or
    carries a key other than ``name`` and ``type``, an entry that leaves one out, an empty or
    repeated name, a name that is a contract column's, and a ``type`` that is not a FLUID logical
    type this engine reads.
    """
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{name, type}} entries")
    if not block:
        raise FluxError(code, f"{where} is empty: declare a hidden column or leave the key out")
    if len(block) > _MOST_HIDDEN_COLUMNS:
        raise FluxError(code, f"{where} holds more than {_MOST_HIDDEN_COLUMNS} entries")
    taken: Set[str] = {nfc(name) for name in contract_columns}
    found: List[HiddenColumn] = []
    for index, item in enumerate(block):
        column = _entry(item, f"{where}[{index}]", taken, code)
        taken.add(column.name)
        found.append(column)
    return tuple(found)


def read_hidden_value(value: Any, *, where: str, code: ErrorCode) -> bool:
    """Whether an emit's ``hidden`` value makes it a hidden table; anything but ``true`` is refused with *code*."""
    if value is not True:
        raise FluxError(code, f"{where} is true: it is the only value the key takes")
    return True


def hidden_columns_of(extension: Optional[Mapping[str, Any]]) -> Tuple[HiddenColumn, ...]:
    """The hidden columns an emit's block holds (read and checked by :func:`read_hidden_columns`)."""
    raw = extension.get(HIDDEN_COLUMNS_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(
        HiddenColumn(name=nfc(str(item["name"])), type=str(item["type"]))
        for item in raw
        if isinstance(item, Mapping) and "name" in item and "type" in item
    )


def fidelity_without_hidden(block: Any, extension: Optional[Mapping[str, Any]]) -> Any:
    """The fidelity *block* without the ``columns`` entries of the hidden columns *extension* declares.

    What a check of the run's rows reads: the rows hold no hidden column, so there is nothing to
    compare its statistics to. The ``copula`` key is left off as well, which the generator alone
    reads (:mod:`flux_engine.seam.copula`). A block of an emit that declares none of either is
    returned as it is.
    """
    block = fidelity_without_copula(block)
    hidden = {column.name for column in hidden_columns_of(extension)}
    columns = block.get("columns") if isinstance(block, Mapping) else None
    if not hidden or not isinstance(columns, Mapping):
        return block
    return {
        **block,
        "columns": {name: item for name, item in columns.items() if nfc(str(name)) not in hidden},
    }


def with_hidden_columns(compiled: CompiledExpose, hidden: Sequence[HiddenColumn]) -> CompiledExpose:
    """*compiled* with *hidden* appended, in the order given, each marked ``hidden``.

    The result is the expose a plan is built from. The written expose is *compiled* itself.
    """
    if not hidden:
        return compiled
    appended = tuple(
        CompiledColumn(
            name=column.name,
            logical_type=parse_logical_type(column.type),
            required=False,
            declared_type=column.type,
            hidden=True,
        )
        for column in hidden
    )
    return CompiledExpose(expose_id=compiled.expose_id, columns=compiled.columns + appended)


def _expose_with(expose: Any, hidden: Mapping[str, Sequence[HiddenColumn]]) -> Any:
    """One ``exposes[]`` entry with its hidden columns appended to its schema, or the entry itself."""
    if not isinstance(expose, Mapping):
        return expose
    columns = hidden.get(nfc(str(expose.get("exposeId", ""))))
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    if not columns or not isinstance(contract, Mapping):
        return expose
    if not isinstance(schema, Sequence) or isinstance(schema, (str, bytes)):
        return expose
    appended = [{"name": column.name, "type": column.type} for column in columns]
    return {**expose, "contract": {**contract, "schema": [*schema, *appended]}}


def with_hidden_in_schema(
    contract_document: Mapping[str, Any], hidden: Mapping[str, Sequence[HiddenColumn]]
) -> Dict[str, Any]:
    """*contract_document* with each expose's hidden columns appended to its schema.

    *hidden* is the hidden columns by ``exposeId``. The document is the one the rules of a run
    are read against, so a rule reads and sets a hidden column, and a path reads a linked row's,
    as it does a contract column. It is never hashed and never shipped.
    """
    document: Dict[str, Any] = dict(contract_document)
    exposes = contract_document.get("exposes")
    if not isinstance(exposes, Sequence) or isinstance(exposes, (str, bytes)):
        return document
    rebuilt: List[Any] = []
    for expose in exposes:
        rebuilt.append(_expose_with(expose, hidden))
    document["exposes"] = rebuilt
    return document
