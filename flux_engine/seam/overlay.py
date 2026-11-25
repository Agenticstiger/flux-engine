# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The profile overlay a Simulation declares: the ``overlay`` key (ADR 0030 section 7).

A team that asks what happens if a table doubles needs a variant of the profile its Simulation
pins, with the numbers it changed named. An overlay is a named RFC 7396 merge patch over that
profile. A Simulation declares one under the top-level ``overlay`` key of its namespace, in the
FLUX bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        overlay:
          name: double-churn
          patch:
            tables:
              subscriber: {rowCount: 2400}

* ``name`` matches ``[a-z][a-z0-9-]*`` and has at most 63 characters.
* ``patch`` is a merge patch. It may touch only numbers a profile publishes: a table's
  ``rowCount``, a column's ``metrics`` and ``statistics``, a table's ``columnPairs`` and
  ``periods``, and the top-level ``relationships`` and ``invariants``. A patch replaces a list
  whole, so an overlay restates a column's ``statistics`` to change one of them, and a statistic
  it restates carries no ``noise``: the overlay's numbers are written by hand, never released
  under a privacy budget. Anything else, such as ``_schemaURL``, ``encoding``,
  ``disclosureRules``, ``privacy``, ``references``, ``queries``, a column's ``domain``, or a table
  or column the pinned profile does not hold, is refused.

**The overlaid profile** is ``MergePatch(profile, patch)`` (:func:`merge_patch`), read by
flux-stats' own profile reader at the pinned profile's format (:func:`overlaid_profile`), so every
bound and exactness rule of a profile holds for it. The privacy grade keeps reading the pinned,
unpatched profile (ADR 0020): an overlay's numbers are written by hand, as an assumption's are.

**The key is part of the run's fidelity predicate**, so it is bound into ``H`` before any row is
drawn: :func:`bound_into_h` is the member every emit's entry for ``H`` gains, the ``jcs-document``
digest of ``{name, patch}``, present only when the Simulation declares one, so a bundle that
declares none hashes exactly as before. ``G`` leaves the member out
(:func:`flux_engine.seam.emit.seam_reusable_digest`): the overlay is read by ``plan`` and
``verify`` only and changes no committed state.

``MergePatch`` is written from RFC 7396 section 2, whose function is twelve lines. Prior art
(fetched 2026-11-22): the RFC itself; ``json-merge-patch`` 0.3.0 (BSD) on PyPI, not needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.seam.coverage import printable

__all__ = [
    "OVERLAY_KEY",
    "Overlay",
    "bound_into_h",
    "merge_patch",
    "overlaid_document",
    "overlaid_profile",
    "overlay_from_plan",
    "read_overlay_block",
    "read_overlay_file",
    "require_in_contract",
]

#: The top-level key of the namespace that declares an overlay, and the member an emit's ``H`` gains.
OVERLAY_KEY = "overlay"

_NAME = re.compile(r"[a-z][a-z0-9-]*")
_LONGEST_NAME = 63
_ENTRY_KEYS = ("name", "patch")

#: What a table of the pinned profile may have patched, and what a column may.
_TABLE_MEMBERS = frozenset({"rowCount", "columns", "columnPairs", "periods"})
_COLUMN_MEMBERS = frozenset({"metrics", "statistics"})
#: The top-level members of a profile a patch may name.
_TOP_MEMBERS = frozenset({"tables", "relationships", "invariants"})


@dataclass(frozen=True)
class Overlay:
    """A declared overlay: its name, its merge patch, and the digest of both."""

    name: str
    patch: Mapping[str, Any]
    digest: DigestRef

    def to_document(self) -> Dict[str, Any]:
        """``{name, patch}``: the document the digest is taken over, and the plan records."""
        return {"name": self.name, "patch": _copy(self.patch)}


def _copy(value: Any) -> Any:
    """A deep copy of plain data: mappings, lists, strings, whole numbers, booleans and null."""
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy(item) for item in value]
    return value


def _named(text: str) -> str:
    shown = printable(text)
    return shown if len(shown) <= 40 else shown[:40] + "..."


def _plain(value: Any, where: str, code: ErrorCode) -> None:
    """Refuse (*code*) a patch value no profile holds: a float, a decimal or a non-string key."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise FluxError(code, f"{where} has a key that is not a string")
            _plain(item, f"{where}.{_named(key)}", code)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _plain(item, f"{where}[{index}]", code)
    elif isinstance(value, (float, Decimal)):
        raise FluxError(
            code, f"{where} is a number written as a decimal; a profile writes it as a string"
        )
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise FluxError(code, f"{where} is {type(value).__name__}, which a profile does not hold")


def _no_noise(value: Any, where: str, code: ErrorCode) -> None:
    """Refuse (*code*) a restated statistic that carries a ``noise`` block, however deep."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "noise":
                raise FluxError(
                    code,
                    f"{where} restates a statistic with a 'noise' block; an overlay's numbers "
                    "are written by hand and carry none",
                )
            _no_noise(item, f"{where}.{_named(str(key))}", code)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _no_noise(item, f"{where}[{index}]", code)


def _object(value: Any, where: str, code: ErrorCode, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FluxError(code, f"{where} patches {what}, so it is a mapping and never null")
    return value


def _refuse_member(where: str, key: str, allowed: Sequence[str], code: ErrorCode) -> FluxError:
    return FluxError(
        code,
        f"{where} touches {_named(key)!r}, which an overlay may not; it may touch only "
        f"{', '.join(allowed)}",
    )


def _check_scope(patch: Mapping[str, Any], where: str, code: ErrorCode) -> None:
    """Refuse (*code*) a patch that touches anything but the numbers a profile publishes."""
    for key, value in patch.items():
        if key not in _TOP_MEMBERS:
            raise _refuse_member(
                where,
                key,
                [
                    "a table's rowCount, columns' metrics and statistics, columnPairs and periods",
                    "relationships",
                    "invariants",
                ],
                code,
            )
        if key == "tables":
            _check_tables(_object(value, f"{where}.tables", code, "the tables"), where, code)
        else:
            _no_noise(value, f"{where}.{key}", code)


def _check_tables(tables: Mapping[str, Any], where: str, code: ErrorCode) -> None:
    for table_name, table in tables.items():
        here = f"{where}.tables.{_named(table_name)}"
        for key, value in _object(table, here, code, "a table").items():
            if key not in _TABLE_MEMBERS:
                raise _refuse_member(
                    here, key, ["rowCount", "columns", "columnPairs", "periods"], code
                )
            if key == "columns":
                _check_columns(_object(value, f"{here}.columns", code, "the columns"), here, code)
            elif key != "rowCount":
                _no_noise(value, f"{here}.{key}", code)


def _check_columns(columns: Mapping[str, Any], where: str, code: ErrorCode) -> None:
    for column_name, column in columns.items():
        here = f"{where}.columns.{_named(column_name)}"
        for key, value in _object(column, here, code, "a column").items():
            if key not in _COLUMN_MEMBERS:
                raise _refuse_member(here, key, ["metrics", "statistics"], code)
            _no_noise(value, f"{here}.{key}", code)


def read_overlay_block(value: Any, *, where: str, code: ErrorCode) -> Overlay:
    """Read ``extensions["com.agentics.flux/1"].overlay``, or refuse it with *code*.

    The block is a mapping holding exactly ``name`` and ``patch``. ``name`` matches
    ``[a-z][a-z0-9-]*`` and has at most 63 characters; ``patch`` is a mapping (a patch that is
    not an object would replace the whole profile) of plain data, no number written as a decimal,
    that touches only what an overlay may touch and restates no ``noise``. Whether the patch
    names a table and a column the pinned profile holds, and whether the result is a profile, is
    :func:`overlaid_document`'s and :func:`overlaid_profile`'s question, which need the profile.
    """
    if not isinstance(value, Mapping):
        raise FluxError(code, f"{where} is a mapping holding exactly 'name' and 'patch'")
    unknown = sorted(str(key) for key in value if key not in _ENTRY_KEYS)
    if unknown:
        raise FluxError(code, f"{where} declares {unknown}; it carries {list(_ENTRY_KEYS)}")
    for required in _ENTRY_KEYS:
        if required not in value:
            raise FluxError(code, f"{where} names its '{required}'")
    name = value["name"]
    if not isinstance(name, str) or len(name) > _LONGEST_NAME or not _NAME.fullmatch(name):
        raise FluxError(
            code,
            f"{where}.name matches [a-z][a-z0-9-]* and has at most {_LONGEST_NAME} characters",
        )
    patch = value["patch"]
    if not isinstance(patch, Mapping):
        raise FluxError(
            code, f"{where}.patch is a mapping: a patch that is not one replaces the whole profile"
        )
    _plain(patch, f"{where}.patch", code)
    _check_scope(patch, f"{where}.patch", code)
    document = {"name": name, "patch": _copy(patch)}
    return Overlay(
        name=name,
        patch=document["patch"],
        digest=digest_of_document(document, subject=f"overlay:{name}"),
    )


def read_overlay_file(path: Path, *, code: ErrorCode) -> Overlay:
    """Read an overlay file for ``fluxsim init --overlay``: one YAML document of ``name`` and ``patch``.

    The text is read as a bundle's documents are (:func:`flux_stats.exact_yaml.load_documents`:
    UTF-8, bounded, no float and no timestamp), and its one document by
    :func:`read_overlay_block`, the reader the namespace key uses, so a file and a Simulation hold
    the same overlay by the same rules. A file that is not a regular file, is past the size a
    bundle file may have, is not UTF-8, holds no document or several, or holds an overlay the
    reader refuses is refused with *code*, naming the file.
    """
    import stat

    from flux_engine.seam.resolve import refuse_oversized_file
    from flux_stats.exact_yaml import load_documents

    name = Path(path).name
    try:
        status = Path(path).stat()
        if not stat.S_ISREG(status.st_mode):
            raise FluxError(code, f"{printable(name)}: not a regular file, so it is not read")
        refuse_oversized_file(printable(name), status.st_size)
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise FluxError(
            code, f"cannot read {printable(name)}: {error.strerror or error}"
        ) from error
    except UnicodeDecodeError:
        raise FluxError(code, f"{printable(name)}: it is not UTF-8") from None
    documents = load_documents(text, name=printable(name))
    if len(documents) != 1:
        raise FluxError(
            code,
            f"{printable(name)}: an overlay file holds one document, "
            f"and this holds {len(documents)}",
        )
    return read_overlay_block(documents[0], where=printable(name), code=code)


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396 ``MergePatch(Target, Patch)``, section 2, on plain data.

    A patch that is an object patches *target* member by member (a *target* that is not an object
    is taken as an empty one), a member whose value is ``null`` is removed, and any other patch,
    a list included, replaces *target* whole. Neither argument is changed: the result shares
    nothing with them.
    """
    if not isinstance(patch, Mapping):
        return _copy(patch)
    result: Dict[str, Any] = (
        {key: _copy(item) for key, item in target.items()} if isinstance(target, Mapping) else {}
    )
    for name, value in patch.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = merge_patch(result.get(name), value)
    return result


def _require_present(profile: Mapping[str, Any], patch: Mapping[str, Any], code: ErrorCode) -> None:
    """Refuse (*code*) a patch that names a table or a column the pinned profile does not hold."""
    tables = profile.get("tables")
    held = tables if isinstance(tables, Mapping) else {}
    for table_name, table in (patch.get("tables") or {}).items():
        if table_name not in held:
            raise FluxError(
                code,
                f"the overlay patches table {_named(table_name)!r}, which the pinned profile "
                "does not hold",
            )
        _require_columns(held[table_name], table, table_name, code)
        _require_history_rows(held[table_name], table, table_name, code)


def require_in_contract(
    contract: Mapping[str, Sequence[str]], overlay: Overlay, *, code: ErrorCode
) -> None:
    """Refuse (*code*) an overlay that names a table or a column the pinned contract does not have.

    *contract* maps each table (expose) of the pinned FLUID contract to its column names. A
    profile describes the contract's tables, so a name the contract lacks is one no profile
    can hold, and it is refused without a profile; a name the contract has and the profile
    lacks is :func:`overlaid_document`'s, which needs the profile.
    """
    at = f"overlay {_named(overlay.name)!r}: patch.tables"
    for table_name, table in (overlay.patch.get("tables") or {}).items():
        if table_name not in contract:
            raise FluxError(
                code,
                f"{at}[{_named(table_name)!r}]: the pinned contract declares no such table",
            )
        patched = table.get("columns") if isinstance(table, Mapping) else None
        for column_name in patched or ():
            if column_name not in contract[table_name]:
                raise FluxError(
                    code,
                    f"{at}[{_named(table_name)!r}].columns[{_named(column_name)!r}]: "
                    f"the pinned contract declares no such column in table {_named(table_name)!r}",
                )


def _require_history_rows(held: Any, table: Any, table_name: str, code: ErrorCode) -> None:
    """Refuse (*code*) a new ``rowCount`` for a table whose pinned profile publishes a history.

    The rows of such a table are the versions of the history's entities, and the entities are
    read from the table's ``history``, which an overlay cannot patch. A different ``rowCount``
    would therefore make each chain longer or shorter and add no entity, and nothing downstream
    says so: the run only fails its tier check.
    """
    if not isinstance(held, Mapping) or not isinstance(table, Mapping) or "rowCount" not in table:
        return
    if "history" in held and table["rowCount"] != held.get("rowCount"):
        raise FluxError(
            code,
            f"the overlay patches the rowCount of table {_named(table_name)!r}, which publishes a "
            "history: its rows are the versions of the history's entities (history.entities), "
            "and an overlay cannot patch the history, so another rowCount would lengthen or "
            "shorten each chain and add no entity. Leave the rowCount of that table as it is",
        )


def _require_columns(held: Any, table: Any, table_name: str, code: ErrorCode) -> None:
    """Refuse (*code*) a patched column the pinned *held* table does not have."""
    columns = held.get("columns") if isinstance(held, Mapping) else None
    patched = table.get("columns") if isinstance(table, Mapping) else None
    for column_name in patched or ():
        if not isinstance(columns, Mapping) or column_name not in columns:
            raise FluxError(
                code,
                f"the overlay patches column {_named(column_name)!r} of table "
                f"{_named(table_name)!r}, which the pinned profile does not hold",
            )


def overlaid_document(
    profile: Mapping[str, Any], overlay: Overlay, *, code: ErrorCode
) -> Dict[str, Any]:
    """``MergePatch(profile, patch)``: the pinned profile's document with the overlay applied.

    Refuses (*code*) a patch that names a table or a column the profile does not hold. The
    document read is the one the pinned digest was taken over, and it is not changed.
    """
    _require_present(profile, overlay.patch, code)
    merged = merge_patch(profile, overlay.patch)
    assert isinstance(merged, dict)
    return merged


def overlaid_profile(
    profile: Mapping[str, Any], overlay: Overlay, *, code: ErrorCode
) -> Tuple[Any, Dict[str, Any]]:
    """The overlaid profile, read by flux-stats' profile reader, and its document.

    The reader reads the document at the format it names, the pinned profile's, so every bound
    and exactness rule of a profile holds for the overlaid one. A document it refuses is refused
    with *code*, with the reader's own words.
    """
    from flux_stats.profile import parse_profile

    document = overlaid_document(profile, overlay, code=code)
    try:
        return parse_profile(document, allow_empty_calendar_entries=True), document
    except FluxError as error:
        # The reader's words quote a key of the patch as it was written, and a key may carry a
        # newline or a terminal escape: the message holds it quoted.
        raise FluxError(
            code,
            f"the overlay {overlay.name!r} makes a profile that is refused: "
            f"{printable(error.detail)}",
        ) from error


def bound_into_h(overlay: Optional[Overlay]) -> Optional[str]:
    """The member of an emit's entry for ``H`` that an overlay makes, or ``None``.

    The ``jcs-document`` digest of ``{name, patch}``; ``None`` when the Simulation declares no
    overlay, so the entry gains no key.
    """
    return overlay.digest.value if overlay is not None else None


def overlay_from_plan(recorded: Any, *, code: ErrorCode) -> Optional[Overlay]:
    """The overlay a run's plan records (``{name, patch}``), or ``None`` when it records none.

    ``verify`` and ``ship`` read the overlay through the plan's digest, never from a bundle
    directory that nothing binds to the run. The record is read as the namespace reads it.
    """
    if recorded is None:
        return None
    return read_overlay_block(recorded, where="the plan's overlay", code=code)
