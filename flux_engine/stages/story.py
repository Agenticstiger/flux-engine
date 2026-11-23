# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim story``: one entity's history, read from a committed run (ADR 0031 section 3).

The story is told from the run directory alone and writes nothing. Each stream it reads is
read once, without following a link, and digested as ``verify`` digests it
(:func:`flux_engine.stages.verify._emit_component_digest`) against the receipt's
``outputDigest`` before a row of it is used, so the story holds only bytes the receipt attests.

The entity is the rows of one expose whose key equals the value asked for, compared as the
canonical text of the key's value. Its history is the rows that link to it and the rows it
links to, through the relationships the run's contract declares (the run directory keeps the
contract in the ``fidelity-input-<expose>.json`` of each expose that declares fidelity), up to
a number of hops, and at most a number of rows. The entity is every row
of that expose that matches, and the story starts from all of them. Rows are ordered by their first
date or timestamp column, then expose, then row index, except that the entity's own rows come first.
Every row says how many relationships away from the entity it was reached (``hop``, 0 for the
entity's own rows) and the relationship it was reached by.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from flux_engine.api.stages import StageResult
from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.safeio import is_regular_file, json_lines, read_bytes_nofollow
from flux_engine.seam.keys import ContractKeys, ExposeKeys, Relationship, compile_keys
from flux_engine.stages.errors import FLX_2111, FLX_2121, RunArtifactsMoved
from flux_engine.stages.layout import RunLayout

__all__ = ["StoryRow", "escape_control", "story"]

#: The bidirectional controls and the line and paragraph separators, printed escaped as well as
#: the C0 and C1 controls and DEL, so a value can neither drive the terminal nor reorder or
#: split the lines a reader sees.
_ESCAPED_BEYOND_CONTROLS = frozenset(
    [0x061C, 0x200E, 0x200F, 0x2028, 0x2029]
    + list(range(0x202A, 0x202F))
    + list(range(0x2066, 0x206A))
)

_DATED_TYPES = ("DATE", "TIMESTAMP")


def escape_control(text: str) -> str:
    """*text* with every control and bidirectional control character written out.

    C0, C1 and DEL come out as ``\\xNN``; U+061C, U+200E, U+200F, U+202A to U+202E, U+2066 to
    U+2069, U+2028 and U+2029 as ``\\uNNNN``. Nothing else changes, a backslash included.
    """
    out: List[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif code in _ESCAPED_BEYOND_CONTROLS:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def cell_text(value: Any) -> Optional[str]:
    """The canonical text of one decoded cell, ``None`` for a null."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class StoryRow:
    """One row of the story: where it is, its key, its first date, its cells and how it was reached."""

    expose_id: str
    index: int
    date: Optional[str]
    key: Tuple[Tuple[str, Optional[str]], ...]
    cells: Tuple[Tuple[str, Any], ...]
    hop: int = 0
    relationship: Optional[str] = None

    def sort_key(self) -> Tuple[bool, bool, str, str, int]:
        return (self.hop != 0, self.date is None, self.date or "", self.expose_id, self.index)

    def human_line(self) -> str:
        """The row as one line: date, expose, key, hop, relationship, then ``column=value`` pairs.

        The relationship is ``-`` at hop 0. Control characters are escaped.
        """
        key = ",".join("null" if text is None else text for _, text in self.key) or "-"
        pairs = [f"{name}={_shown(value)}" for name, value in self.cells]
        fields = [
            self.date or "-",
            self.expose_id,
            key,
            str(self.hop),
            self.relationship or "-",
            *pairs,
        ]
        return "\t".join(escape_control(field) for field in fields)

    def json_document(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "expose": self.expose_id,
            "row": self.index,
            "key": {name: text for name, text in self.key},
            "hop": self.hop,
            "relationship": self.relationship,
            "columns": {name: value for name, value in self.cells},
        }


def _shown(value: Any) -> str:
    text = cell_text(value)
    return "null" if text is None else text


class _Refused(Exception):
    """A refusal: the story could not be told, and says why; it carries a code when the catalogue has one."""

    def __init__(self, message: str, code: Optional[ErrorCode] = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Stream:
    """One verified stream: its decoded rows, and the indexes built over it."""

    columns: Tuple[str, ...]
    types: Tuple[str, ...]
    rows: List[List[Any]]
    indexes: Dict[Tuple[str, ...], Dict[Tuple[str, ...], List[int]]]


class _Run:
    """The streams of one committed run, each read and checked once, when first asked for."""

    def __init__(self, layout: RunLayout, receipt: Mapping[str, Any]) -> None:
        self.layout = layout
        self.receipt = receipt
        self._streams: Dict[str, _Stream] = {}

    def stream(self, expose_id: str) -> _Stream:
        if expose_id not in self._streams:
            self._streams[expose_id] = self._read(expose_id)
        return self._streams[expose_id]

    def _read(self, expose_id: str) -> _Stream:
        from flux_engine.stages.verify import _emit_component_digest, _read_json

        layout = self.layout
        path = layout.emit_path(expose_id)
        claimed = self.receipt["emits"][expose_id].get("outputDigest")
        moved = RunArtifactsMoved(components=[f"emits.{expose_id}"])
        if not is_regular_file(path) or not is_regular_file(
            layout.seam_attestation_path(expose_id)
        ):
            raise moved
        data = read_bytes_nofollow(path)
        # The digest is taken over the bytes this read returned, so the rows below are the
        # ones the receipt attests and not a second read of a file that may have moved.
        if claimed is None or _emit_component_digest(layout, expose_id, data) != claimed:
            raise moved
        try:
            attestation = _read_json(layout.seam_attestation_path(expose_id))
            columns = tuple(str(name) for name in attestation["columns"])
            types = tuple(str(name) for name in attestation["types"])
            rows = [json.loads(line) for line in json_lines(data.decode("utf-8")) if line]
        except (ValueError, KeyError, TypeError) as error:
            raise moved from error
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise moved
        return _Stream(columns, types, rows, {})

    def index(self, expose_id: str, columns: Tuple[str, ...]) -> Dict[Tuple[str, ...], List[int]]:
        """The rows of an expose by the canonical text of *columns*, nulls left out."""
        stream = self.stream(expose_id)
        if columns not in stream.indexes:
            positions = [stream.columns.index(name) for name in columns]
            found: Dict[Tuple[str, ...], List[int]] = {}
            for number, row in enumerate(stream.rows):
                texts = [cell_text(row[position]) for position in positions]
                if None not in texts:
                    found.setdefault(tuple(t for t in texts if t is not None), []).append(number)
            stream.indexes[columns] = found
        return stream.indexes[columns]


def _contract_keys(layout: RunLayout, receipt: Mapping[str, Any]) -> Optional[ContractKeys]:
    """The keys and relationships the run's contract declares, from the run directory alone."""
    from flux_engine.stages.verify import _read_json

    for expose_id in sorted(receipt.get("emits", {})):
        path = layout.fidelity_input_path(expose_id)
        if not is_regular_file(path):
            continue
        try:
            contract = _read_json(path)["contract"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(contract, dict):
            return compile_keys(contract)
    return None


def _key_columns(keys: Optional[ExposeKeys]) -> Tuple[str, ...]:
    """The columns that name a row of an expose: its primary entity, else its first declared key."""
    if keys is None:
        return ()
    for entity in keys.entities:
        if entity.kind == "primary":
            return (entity.column,)
    return tuple(keys.keys[0]) if keys.keys else ()


def _entity_columns(
    values: Sequence[str], stream: _Stream, expose_id: str, declared: Tuple[str, ...]
) -> Dict[str, str]:
    """The key columns and their text from ``--key`` values: ``COLUMN=VALUE`` each, or one bare value."""
    named: Dict[str, str] = {}
    for value in values:
        column, separator, text = value.partition("=")
        if separator and column in stream.columns:
            if column in named:
                raise _Refused(f"--key names column {escape_control(column)} twice", FLX_2121)
            named[column] = text
    if named and len(named) == len(values):
        return named
    if len(values) != 1:
        raise _Refused(
            "a composite key is given as --key COLUMN=VALUE, once per column, "
            f"and every COLUMN must be a column of {escape_control(expose_id)}",
            FLX_2121,
        )
    if len(declared) != 1:
        columns = escape_control(", ".join(stream.columns))
        unknown = values[0].partition("=")
        if unknown[1] and unknown[0] not in stream.columns:
            columns += f"; {escape_control(unknown[0])} is not one of them"
        raise _Refused(
            f"{escape_control(expose_id)} declares no one-column key, so --key needs "
            f"COLUMN=VALUE (columns: {columns})",
            FLX_2121,
        )
    return {declared[0]: values[0]}


def _unknown_column_hint(
    keys: Sequence[str], stream: _Stream, contract: Optional[ContractKeys], expose_id: str
) -> str:
    """A note for a lone ``--key X=Y`` whose X is no column of the expose: a mistyped column name."""
    if len(keys) != 1:
        return ""
    column, separator, _ = keys[0].partition("=")
    if not separator or column in stream.columns:
        return ""
    declared = _key_columns(contract.exposes.get(expose_id) if contract else None)
    shown = escape_control(", ".join(declared or stream.columns))
    label = "key columns" if declared else "columns"
    return (
        f"; {escape_control(column)} is not a column of {escape_control(expose_id)} "
        f"(its {label}: {shown}), so the whole value was compared as the key"
    )


def story(
    *,
    state_dir: Union[str, Path, None],
    run_id: str,
    expose_id: str,
    keys: Sequence[str],
    depth: int = 2,
    limit: int = 200,
) -> StageResult:
    """Tell the history of the row of *expose_id* named by *keys* in the run at *run_id*."""
    from flux_engine.stages.verify import _load_run

    state = Path(state_dir) if state_dir is not None else Path(".flux")
    layout = RunLayout.for_state_dir(state, run_id).require_real_dirs()
    try:
        loaded = _load_run(layout)
        if isinstance(loaded, StageResult):
            return replace(loaded, stage="story")
        return _tell(loaded.layout, loaded.receipt, expose_id, keys, depth, limit)
    except _Refused as refusal:
        return StageResult(
            stage="story",
            exit_code=1,
            _artifacts={"code": refusal.code.code} if refusal.code else {},
            stderr=str(refusal),
            error_code=refusal.code.code if refusal.code else None,
        )
    except FluxError as error:
        return StageResult(
            stage="story",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )


def _tell(
    layout: RunLayout,
    receipt: Mapping[str, Any],
    expose_id: str,
    keys: Sequence[str],
    depth: int,
    limit: int,
) -> StageResult:
    emits = receipt.get("emits", {})
    if expose_id not in emits:
        known = escape_control(", ".join(sorted(emits))) or "none"
        raise _Refused(
            f"this run does not emit '{escape_control(expose_id)}'; it emits {known}", FLX_2111
        )
    contract = _contract_keys(layout, receipt)
    run = _Run(layout, receipt)
    stream = run.stream(expose_id)
    columns = _entity_columns(
        keys,
        stream,
        expose_id,
        _key_columns(contract.exposes.get(expose_id) if contract else None),
    )
    names = tuple(sorted(columns))
    found = run.index(expose_id, names).get(tuple(columns[name] for name in names))
    if not found:
        wanted = escape_control(", ".join(f"{name}={columns[name]}" for name in names))
        raise _Refused(
            f"{escape_control(expose_id)} has no row whose key is {wanted}"
            + _unknown_column_hint(keys, stream, contract, expose_id),
            FLX_2121,
        )

    relationships: List[Relationship] = []
    if contract is not None:
        for each in sorted(contract.exposes):
            relationships.extend(
                r
                for r in contract.exposes[each].relationships
                if r.source in emits and r.target in emits
            )

    # Every row that matches the key is the entity asked for.
    chosen: List[Tuple[str, int]] = [(expose_id, number) for number in found[:limit]]
    truncated = len(found) > limit
    seen: Set[Tuple[str, int]] = set(chosen)
    reached_by: Dict[Tuple[str, int], Tuple[int, Optional[str]]] = {
        place: (0, None) for place in chosen
    }
    frontier = list(chosen)
    for hop in range(1, depth + 1):
        reached: List[Tuple[Tuple[str, int], str]] = []
        for relationship in relationships:
            reached.extend(
                (place, relationship.name) for place in _across(run, relationship, frontier)
            )
        frontier = []
        for place, name in reached:
            if place in seen:
                continue
            if len(chosen) >= limit:
                truncated = True
                continue
            seen.add(place)
            chosen.append(place)
            reached_by[place] = (hop, name)
            frontier.append(place)

    rows = sorted(
        (_row_of(run, *place, contract, *reached_by[place]) for place in chosen),
        key=StoryRow.sort_key,
    )
    matched = len(found)
    wanted = escape_control(", ".join(f"{name}={columns[name]}" for name in names))
    header = (
        f"{matched} {'row' if matched == 1 else 'rows'} of {escape_control(expose_id)} "
        f"matched {wanted}"
    )
    return StageResult(
        stage="story",
        exit_code=0,
        _artifacts={
            "exposeId": expose_id,
            "matched": matched,
            "rows": [row.json_document() for row in rows],
            "truncated": truncated,
            "limit": limit,
            "depth": depth,
        },
        stdout="\n".join([header, *(row.human_line() for row in rows)]),
        stderr=(
            f"the story holds the first {limit} rows it reached; raise --limit to read the rest"
            if truncated
            else ""
        ),
    )


def _across(
    run: _Run, relationship: Relationship, frontier: Sequence[Tuple[str, int]]
) -> List[Tuple[str, int]]:
    """The rows one hop from *frontier* over *relationship*, the rows it links to and the rows that link to it."""
    reached: List[Tuple[str, int]] = []
    for here, there, here_columns, there_columns in (
        (
            relationship.source,
            relationship.target,
            relationship.from_columns,
            relationship.to_columns,
        ),
        (
            relationship.target,
            relationship.source,
            relationship.to_columns,
            relationship.from_columns,
        ),
    ):
        mine = [number for expose, number in frontier if expose == here]
        if not mine:
            continue
        stream = run.stream(here)
        positions = [stream.columns.index(name) for name in here_columns]
        index = run.index(there, tuple(there_columns))
        for number in mine:
            texts = [cell_text(stream.rows[number][position]) for position in positions]
            if None in texts:
                continue
            reached.extend(
                (there, other) for other in index.get(tuple(t for t in texts if t is not None), [])
            )
    return reached


def _row_of(
    run: _Run,
    expose_id: str,
    number: int,
    contract: Optional[ContractKeys],
    hop: int,
    relationship: Optional[str],
) -> StoryRow:
    stream = run.stream(expose_id)
    row = stream.rows[number]
    date: Optional[str] = None
    for value, declared in zip(row, stream.types, strict=True):
        if declared.upper().startswith(_DATED_TYPES) and value is not None:
            date = cell_text(value)
            break
    key_names = _key_columns(contract.exposes.get(expose_id) if contract else None)
    key = tuple((name, cell_text(row[stream.columns.index(name)])) for name in key_names)
    cells = tuple(zip(stream.columns, row, strict=True))
    return StoryRow(expose_id, number, date, key, cells, hop, relationship)
