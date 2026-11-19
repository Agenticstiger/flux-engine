# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Keys from the FLUID contract.

A FLUID contract declares the keys of its exposes, and FLUX reads them instead
of restating them. FLUID 0.7.7 is the first version that can say all of it:

* ``exposes[].semantics.relationships[]`` names one foreign key per entry:
  ``to`` (an expose of the same contract), ``fromColumns`` and ``toColumns``
  (ordered and paired by position, so a composite key is one relationship),
  ``cardinality`` (``many_to_one`` or ``one_to_one``) and ``joinType``
  (``left``, the default, or ``inner``). The declaring expose is the many side.
* ``exposes[].semantics.entities[]`` names keys by type: a ``primary`` or
  ``unique`` entity is a one-column key of its expose, ``natural`` is the key
  whose versions a validity window separates, and ``foreign`` is a join name
  (the relationships carry the foreign keys).

What the engine takes from them:

* Keys are read only from a contract that declares ``fluidVersion: "0.7.7"``.
  An older contract behaves exactly as before, entities and all.
* A relationship reaches at most one target row (RFC-data-rules section 2.2),
  so its ``toColumns``, composite or not, are a key of the target, whatever
  entities the target declares. A ``one_to_one`` relationship makes its
  ``fromColumns`` a key of the declaring expose as well. On an expose with a
  validity window a relationship's target key holds at each point in time,
  not across versions, so it is kept apart (:attr:`ExposeKeys.instant_keys`).
* A key that does not resolve is refused where the contract declares it
  (``FLX-3005``): a ``to`` that names no expose, column lists of different
  lengths, a repeated column, a column the expose does not declare, a primary,
  unique or natural entity whose ``expr`` names no column.
* FLUX does not restate a key (``FLX-3111``): see :func:`check_no_key_blocks`
  and :func:`check_unique_declarations`.
* An emit whose contract declares keys pins the contract digest
  (``provenance.contractDigest``), so a moved key stops the plan instead of
  passing unnoticed (``FLX-3112``).

Prior art. The relationship shape is Apache Ossie's (``from_columns``,
``to_columns``) plus the cardinality and join type Ossie leaves out, as FLUID
took it. SDV's multi-table metadata keeps a composite key as equal-length
column lists and requires the parent key to identify one row; dbt MetricFlow
joins a foreign entity to a primary or unique one; Snowflake semantic views
require the referenced side to be a key. No tool refuses a second key source
that disagrees with the first, so that check is this module's own.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError, register
from flux_engine.seam.contract import CompiledExpose

__all__ = [
    "FLX_3005",
    "FLX_3111",
    "FLX_3112",
    "KEYED_FLUID_VERSIONS",
    "KEY_BLOCK_NAMES",
    "ContractKeys",
    "EntityKey",
    "ExposeKeys",
    "Relationship",
    "check_no_key_blocks",
    "check_unique_declarations",
    "compile_keys",
    "declared_exposes",
    "keys_for_emit",
    "mark_key_columns",
]

FLX_3005 = register(
    "FLX-3005",
    "a key the contract declares does not resolve",
    "FLUX takes primary and foreign keys from the FLUID contract "
    "(exposes[].semantics.relationships and the primary, unique and natural "
    "entities), so a key it cannot follow is refused where the contract "
    "declares it. A relationship's `to` must name an expose of the same "
    "contract, its fromColumns and toColumns must pair up one to one, with no "
    "column twice, and every column must be one its expose's contract.schema "
    "declares; an entity that is a key must name a column in its `expr` (the "
    "entity's name when `expr` is absent). Fix the contract, not the "
    "Simulation: a composite key is one relationship with several columns.",
)

FLX_3111 = register(
    "FLX-3111",
    "the Simulation restates a key the contract declares",
    "Keys come from the FLUID contract, and FLUX does not restate them. The "
    "Simulation's extensions block carries a key declaration of its own "
    "(keys, primaryKey, primaryKeys, foreignKey, foreignKeys, uniqueKeys, "
    "relationships or entities), or its fidelity block declares `unique` "
    "against the contract: `unique: false` on a column the contract makes a "
    "key, or `unique: true` on a key the contract's validity window repeats "
    "across versions. Remove the declaration from the Simulation and declare "
    "the key in the contract's exposes[].semantics.",
)

FLX_3112 = register(
    "FLX-3112",
    "an emit whose contract declares keys pins no contract digest",
    "Every key the engine generates is checked against the contract that "
    "declares it, so an emit of a contract that declares keys pins that "
    "contract's digest in provenance.contractDigest. A moved key then stops "
    "the plan with FLX-3101 instead of passing unnoticed. The message names "
    "the digest of the contract in the bundle: pin it once the keys are the "
    "ones you mean.",
)

#: FLUID versions whose keys the engine reads. 0.7.7 is the first with
#: relationships; it is a preview that flux-spec 0.5.2 vendors from a pinned
#: forge-cli commit, selected only by an exact pin.
KEYED_FLUID_VERSIONS: FrozenSet[str] = frozenset({"0.7.7"})

#: Names that would make an extensions block a second source of keys. A block
#: is closed anyway; these get FLX-3111 instead of the generic FLX-3105, with
#: a message that says where keys are declared.
KEY_BLOCK_NAMES: FrozenSet[str] = frozenset(
    {
        "keys",
        "primaryKey",
        "primaryKeys",
        "foreignKey",
        "foreignKeys",
        "uniqueKeys",
        "relationships",
        "entities",
    }
)

_CARDINALITIES: FrozenSet[str] = frozenset({"many_to_one", "one_to_one"})
_JOIN_TYPES: FrozenSet[str] = frozenset({"left", "inner"})
#: The same sets as sorted lists: membership in a list compares, so an
#: unhashable value from a malformed document is refused instead of raising.
_CARDINALITIES_LIST: List[str] = sorted(_CARDINALITIES)
_JOIN_TYPES_LIST: List[str] = sorted(_JOIN_TYPES)
_ENTITY_TYPES: FrozenSet[str] = frozenset({"primary", "foreign", "unique", "natural"})
#: Entity types that are keys of their own expose.
_KEY_ENTITY_TYPES: FrozenSet[str] = frozenset({"primary", "unique", "natural"})

Key = Tuple[str, ...]


@dataclass(frozen=True)
class Relationship:
    """One foreign key: ``from_columns`` of *source* equal ``to_columns`` of *target*."""

    name: str
    source: str
    target: str
    from_columns: Key
    to_columns: Key
    cardinality: str
    join_type: str


@dataclass(frozen=True)
class EntityKey:
    """A ``primary``, ``unique`` or ``natural`` entity and the column it names."""

    name: str
    kind: str
    column: str


@dataclass(frozen=True)
class ExposeKeys:
    """The keys the contract declares for one expose.

    Attributes
    ----------
    expose_id
        The expose, in NFC.
    entities
        Its ``primary``, ``unique`` and ``natural`` entities, in declared order.
    relationships
        The relationships it declares (it is their many side), in declared order.
    keys
        Column tuples the expose keeps unique across all its rows, sorted: each
        primary or unique entity's column, the ``toColumns`` of every
        relationship that targets it, and the ``fromColumns`` of each
        ``one_to_one`` relationship it declares, the last two unless the
        expose is versioned.
    instant_keys
        On a versioned expose, the keys that come from relationships: unique
        at each point in time, repeated across versions.
    versioned
        Whether the expose declares a validity window (a dimension carrying
        ``typeParams.validityParams``).
    """

    expose_id: str
    entities: Tuple[EntityKey, ...] = ()
    relationships: Tuple[Relationship, ...] = ()
    keys: Tuple[Key, ...] = ()
    instant_keys: Tuple[Key, ...] = ()
    versioned: bool = False

    @property
    def one_column_keys(self) -> FrozenSet[str]:
        """The columns that are a whole key on their own: unique on every row."""
        return frozenset(key[0] for key in self.keys if len(key) == 1)

    @property
    def repeated_keys(self) -> FrozenSet[str]:
        """Columns whose values the validity window repeats across versions:
        the natural entity and one-column instant keys of a versioned expose."""
        if not self.versioned:
            return frozenset()
        natural = {entity.column for entity in self.entities if entity.kind == "natural"}
        return frozenset(natural | {key[0] for key in self.instant_keys if len(key) == 1})

    @property
    def declares_keys(self) -> bool:
        return bool(self.entities or self.relationships or self.keys or self.instant_keys)


@dataclass(frozen=True)
class ContractKeys:
    """Every expose's keys, by exposeId (NFC)."""

    fluid_version: str
    exposes: Mapping[str, ExposeKeys]

    @property
    def declares_keys(self) -> bool:
        return any(keys.declares_keys for keys in self.exposes.values())

    def expose(self, expose_id: str) -> ExposeKeys:
        wanted = nfc(expose_id)
        return self.exposes.get(wanted, ExposeKeys(expose_id=wanted))


def compile_keys(contract: Mapping[str, Any]) -> Optional[ContractKeys]:
    """The keys *contract* declares, or ``None`` when its FLUID version carries none.

    Raises ``FLX-3005`` on the first key that does not resolve, naming the
    expose and the key.
    """
    version = contract.get("fluidVersion")
    if not isinstance(version, str) or version not in KEYED_FLUID_VERSIONS:
        return None
    declared = declared_exposes(contract)
    columns = {expose_id: _column_names(entry) for expose_id, entry in declared.items()}
    entities: Dict[str, Tuple[EntityKey, ...]] = {}
    relationships: Dict[str, Tuple[Relationship, ...]] = {}
    versioned: Dict[str, bool] = {}
    for expose_id, entry in declared.items():
        semantics = entry.get("semantics")
        semantics = semantics if isinstance(semantics, Mapping) else {}
        entities[expose_id] = _entities(expose_id, semantics, columns[expose_id])
        relationships[expose_id] = _relationships(expose_id, semantics, columns)
        versioned[expose_id] = _has_validity_window(semantics)
    keys, instant = _key_sets(entities, relationships, versioned)
    compiled = {
        expose_id: ExposeKeys(
            expose_id=expose_id,
            entities=entities[expose_id],
            relationships=relationships[expose_id],
            keys=tuple(sorted(keys[expose_id])),
            instant_keys=tuple(sorted(instant[expose_id])),
            versioned=versioned[expose_id],
        )
        for expose_id in declared
    }
    return ContractKeys(fluid_version=version, exposes=compiled)


def keys_for_emit(
    contract: Mapping[str, Any],
    *,
    expose_id: str,
    pinned: bool,
    contract_digest: str,
    contract_name: str,
    fidelity: Optional[Mapping[str, Any]],
    where: str,
    pin_note: Callable[[], str] = lambda: "",
) -> Optional[ExposeKeys]:
    """The emitted expose's keys, after the seam's key checks.

    ``None`` for a contract whose FLUID version carries no keys the engine
    reads. Raises ``FLX-3005`` when a key does not resolve, ``FLX-3112`` when
    the contract declares keys and the emit pins no contract digest (the
    message names *contract_digest*, the one to pin, and ends with
    *pin_note*), and ``FLX-3111`` when the emit's fidelity block declares
    ``unique`` against a key.
    """
    contract_keys = compile_keys(contract)
    if contract_keys is None:
        return None
    if contract_keys.declares_keys and not pinned:
        raise FluxError(
            FLX_3112,
            f"{where}: {contract_name} declares keys; pin provenance.contractDigest: "
            f"{contract_digest!r}{pin_note()}",
        )
    keys = contract_keys.expose(expose_id)
    check_unique_declarations(keys, fidelity, where=where)
    return keys


def mark_key_columns(compiled: CompiledExpose, keys: Optional[ExposeKeys]) -> CompiledExpose:
    """*compiled* with ``key=True`` on each column the contract makes a one-column key."""
    key_columns = keys.one_column_keys if keys is not None else frozenset()
    if not key_columns:
        return compiled
    return replace(
        compiled,
        columns=tuple(
            replace(column, key=column.name in key_columns) for column in compiled.columns
        ),
    )


def check_no_key_blocks(names: Sequence[Any], *, where: str) -> None:
    """Refuse key declarations among the keys of an extensions block (``FLX-3111``)."""
    restated = sorted(str(name) for name in names if nfc(str(name)) in KEY_BLOCK_NAMES)
    if restated:
        raise FluxError(
            FLX_3111,
            f"{where} declares {restated}: keys come from the FLUID contract, and FLUX does "
            "not restate them (FLUID 0.7.7 declares them in exposes[].semantics: relationships "
            "and primary, unique or natural entities)",
        )


def check_unique_declarations(
    keys: ExposeKeys, fidelity: Optional[Mapping[str, Any]], *, where: str
) -> None:
    """Refuse a fidelity ``unique`` that contradicts the contract's keys (``FLX-3111``).

    ``unique: false`` on a column the contract makes a one-column key, and
    ``unique: true`` on a key the contract's validity window repeats. An
    agreeing ``unique: true`` passes: it restates nothing the contract denies.
    """
    columns = fidelity.get("columns") if isinstance(fidelity, Mapping) else None
    if not isinstance(columns, Mapping):
        return
    one_column, repeated = keys.one_column_keys, keys.repeated_keys
    for raw_name, block in columns.items():
        if not isinstance(block, Mapping) or "unique" not in block:
            continue
        # Read as the generator reads it (generator/statistics.py), so a value
        # that declares uniqueness there is judged here as that declaration.
        name, unique = nfc(str(raw_name)), bool(block["unique"])
        if not unique and name in one_column:
            raise FluxError(
                FLX_3111,
                f"{where}: fidelity.columns[{name!r}] declares unique: false, but the contract "
                f"makes {name!r} a key of expose {keys.expose_id!r}; drop the declaration",
            )
        if unique and name in repeated:
            raise FluxError(
                FLX_3111,
                f"{where}: fidelity.columns[{name!r}] declares unique: true, but expose "
                f"{keys.expose_id!r} declares a validity window that repeats {name!r} across "
                "versions; drop the declaration",
            )


def declared_exposes(contract: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    """Every expose with a string exposeId, by NFC id; the first wins, as in find_expose."""
    exposes = contract.get("exposes")
    declared: Dict[str, Mapping[str, Any]] = {}
    if isinstance(exposes, Sequence) and not isinstance(exposes, (str, bytes)):
        for entry in exposes:
            if isinstance(entry, Mapping) and isinstance(entry.get("exposeId"), str):
                declared.setdefault(nfc(entry["exposeId"]), entry)
    return declared


def _key_sets(
    entities: Mapping[str, Tuple[EntityKey, ...]],
    relationships: Mapping[str, Tuple[Relationship, ...]],
    versioned: Mapping[str, bool],
) -> Tuple[Dict[str, Set[Key]], Dict[str, Set[Key]]]:
    """Per expose, the keys unique on every row and, on a versioned expose, at each instant."""
    keys: Dict[str, Set[Key]] = {expose_id: set() for expose_id in entities}
    instant: Dict[str, Set[Key]] = {expose_id: set() for expose_id in entities}
    for expose_id, declared in entities.items():
        keys[expose_id].update(
            (entity.column,) for entity in declared if entity.kind in ("primary", "unique")
        )
    for expose_id, declared_relationships in relationships.items():
        for relationship in declared_relationships:
            target = relationship.target
            (instant if versioned[target] else keys)[target].add(relationship.to_columns)
            if relationship.cardinality == "one_to_one":
                (instant if versioned[expose_id] else keys)[expose_id].add(
                    relationship.from_columns
                )
    return keys, instant


def _column_names(expose: Mapping[str, Any]) -> FrozenSet[str]:
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    if not isinstance(schema, Sequence) or isinstance(schema, (str, bytes)):
        return frozenset()
    return frozenset(
        nfc(entry["name"])
        for entry in schema
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    )


def _entities(
    expose_id: str, semantics: Mapping[str, Any], columns: FrozenSet[str]
) -> Tuple[EntityKey, ...]:
    raw = semantics.get("entities")
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise FluxError(FLX_3005, f"expose {expose_id!r}: semantics.entities is not a list")
    found: List[EntityKey] = []
    for index, entity in enumerate(raw):
        where = f"expose {expose_id!r} entities[{index}]"
        if not isinstance(entity, Mapping):
            raise FluxError(FLX_3005, f"{where} is not a mapping")
        name, kind = entity.get("name"), entity.get("type")
        if not isinstance(name, str) or not isinstance(kind, str) or kind not in _ENTITY_TYPES:
            raise FluxError(
                FLX_3005, f"{where} has no string name and entity type {sorted(_ENTITY_TYPES)}"
            )
        if kind not in _KEY_ENTITY_TYPES:
            continue
        expr = entity.get("expr", name)
        column = nfc(expr) if isinstance(expr, str) else None
        if column not in columns:
            raise FluxError(
                FLX_3005,
                f"{where} ({kind} entity {name!r}) keys expr {expr!r}, which is not a column of the "
                f"expose; set expr to the key column (columns: {sorted(columns)})",
            )
        found.append(EntityKey(name=nfc(name), kind=str(kind), column=column))
    return tuple(found)


def _relationships(
    expose_id: str, semantics: Mapping[str, Any], columns: Mapping[str, FrozenSet[str]]
) -> Tuple[Relationship, ...]:
    raw = semantics.get("relationships")
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise FluxError(FLX_3005, f"expose {expose_id!r}: semantics.relationships is not a list")
    found: List[Relationship] = []
    for index, entry in enumerate(raw):
        relationship = _relationship(expose_id, index, entry, columns)
        if any(seen.name == relationship.name for seen in found):
            raise FluxError(
                FLX_3005,
                f"expose {expose_id!r} relationship {relationship.name!r} is declared twice; "
                "names are unique within an expose",
            )
        found.append(relationship)
    return tuple(found)


def _relationship(
    expose_id: str, index: int, entry: Any, columns: Mapping[str, FrozenSet[str]]
) -> Relationship:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
        raise FluxError(
            FLX_3005,
            f"expose {expose_id!r} relationships[{index}] is not a mapping with a string name",
        )
    name = nfc(entry["name"])
    where = f"expose {expose_id!r} relationship {name!r}"
    raw_target = entry.get("to")
    if not isinstance(raw_target, str) or nfc(raw_target) not in columns:
        raise FluxError(
            FLX_3005,
            f"{where}: to {raw_target!r} names no expose of this contract "
            f"(exposes: {sorted(columns)}); a relationship names exactly one target expose",
        )
    target = nfc(raw_target)
    from_columns = _key_columns(entry.get("fromColumns"), f"{where} fromColumns")
    to_columns = _key_columns(entry.get("toColumns"), f"{where} toColumns")
    if len(from_columns) != len(to_columns):
        raise FluxError(
            FLX_3005,
            f"{where} pairs {len(from_columns)} fromColumns with {len(to_columns)} toColumns; "
            "they pair up by position",
        )
    _require_columns(from_columns, columns[expose_id], f"{where} fromColumns", expose_id)
    _require_columns(to_columns, columns[target], f"{where} toColumns", target)
    cardinality, join_type = entry.get("cardinality"), entry.get("joinType", "left")
    if cardinality not in _CARDINALITIES_LIST or join_type not in _JOIN_TYPES_LIST:
        raise FluxError(
            FLX_3005,
            f"{where}: cardinality {cardinality!r} and joinType {join_type!r} must be one of "
            f"{sorted(_CARDINALITIES)} and {sorted(_JOIN_TYPES)}",
        )
    return Relationship(
        name=name,
        source=expose_id,
        target=target,
        from_columns=from_columns,
        to_columns=to_columns,
        cardinality=str(cardinality),
        join_type=str(join_type),
    )


def _key_columns(value: Any, where: str) -> Key:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise FluxError(FLX_3005, f"{where} is not a non-empty list of column names")
    key = tuple(nfc(item) for item in value)
    if len(set(key)) != len(key):
        raise FluxError(FLX_3005, f"{where} names a column twice: {list(key)}")
    return key


def _require_columns(key: Key, columns: FrozenSet[str], where: str, expose_id: str) -> None:
    missing = [column for column in key if column not in columns]
    if missing:
        raise FluxError(
            FLX_3005,
            f"{where} names {missing}, which expose {expose_id!r} does not declare "
            f"(columns: {sorted(columns)})",
        )


def _has_validity_window(semantics: Mapping[str, Any]) -> bool:
    dimensions = semantics.get("dimensions")
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)):
        return False
    for dimension in dimensions:
        params = dimension.get("typeParams") if isinstance(dimension, Mapping) else None
        validity = params.get("validityParams") if isinstance(params, Mapping) else None
        if isinstance(validity, Mapping) and (validity.get("isStart") or validity.get("isEnd")):
            return True
    return False
