# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The data owner's side: make the source, write its contract, profile it.

Everything here reads records, so it runs only where the data lives. What leaves
for the generator side is the FLUID contract and the profile document, nothing
else.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import rfc8785
import yaml

from flux_e2e import rules, tpcds

#: The output check a level must pass before the contract names it, the same
#: minimum cell ``fluid profile`` applies to an order statistic (ACRO's default).
MINIMUM_CELL = 10
#: A string column with more distinct values than this is not given an enum.
MAX_ENUM_LEVELS = 64
#: The characters FLUID 0.7.7's enum grammar refuses inside a quoted level.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: The database the contract's bindings name; each run publishes to its own copy.
LOGICAL_DATABASE = "tpcds_shaped"


def generate_source(database: Path, scale_factor: float) -> Dict[str, Any]:
    """Run ``dsdgen`` into a DuckDB file and say what it made."""
    import duckdb

    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    with duckdb.connect(str(database)) as con:
        con.execute("INSTALL tpcds")
        con.execute("LOAD tpcds")
        con.execute(f"CALL dsdgen(sf={float(scale_factor)!r}, keys=true)")
        (extension,) = con.execute(
            "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'tpcds'"
        ).fetchone()
        tables = {
            name: count
            for name, count in con.execute(
                "SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY table_name"
            ).fetchall()
        }
    return {
        "duckdb": duckdb.__version__,
        "tpcdsExtension": extension,
        "scaleFactor": scale_factor,
        "tables": tables,
    }


def export_parquet(database: Path, tables: Sequence[str], out_dir: Path) -> Dict[str, int]:
    """Write each table to ``<out_dir>/<table>.parquet``; return the row counts."""
    import duckdb

    out_dir.mkdir(parents=True, exist_ok=True)
    counts: Dict[str, int] = {}
    with duckdb.connect(str(database), read_only=True) as con:
        for table in tables:
            target = out_dir / f"{table}.parquet"
            con.execute(f"COPY {table} TO '{target}' (FORMAT parquet, COMPRESSION zstd)")
            (counts[table],) = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    return counts


def _fluid_type(duckdb_type: str) -> str:
    upper = duckdb_type.upper()
    return "STRING" if upper == "VARCHAR" else upper


def _enum_type(levels: Sequence[str]) -> str:
    # Each level is a quoted string with its own quote doubled, as SQL writes a
    # string literal. FLUID 0.7.7 spells an enum level this way, and the engine
    # reads it back with flux-stats' declared_enum_values.
    quoted = ("'" + level.replace("'", "''") + "'" for level in levels)
    return "enum(" + ", ".join(quoted) + ")"


def _enum_levels(con: Any, table: str, column: str) -> Tuple[str, ...]:
    """The column's levels when every one passes the output check, else ()."""
    rows = con.execute(
        f'SELECT "{column}", count(*) FROM {table} WHERE "{column}" IS NOT NULL '
        f"GROUP BY 1 ORDER BY 1 LIMIT {MAX_ENUM_LEVELS + 1}"
    ).fetchall()
    if not rows or len(rows) > MAX_ENUM_LEVELS:
        return ()
    levels = tuple(str(value) for value, _ in rows)
    if any(count < MINIMUM_CELL for _, count in rows):
        return ()
    # A quoted FLUID 0.7.7 level holds any character but a control character, and
    # the engine reads it in NFC. A level it cannot spell, or would read as other
    # text, keeps the column a plain string, and so does an empty one.
    if any(
        not level or _CONTROL.search(level) or unicodedata.normalize("NFC", level) != level
        for level in levels
    ):
        return ()
    return levels


def _schema(con: Any, table: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The expose's columns, and which of them the contract gives an enum."""
    schema, enums = [], []
    for name, data_type, nullable in con.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall():
        declared = _fluid_type(data_type)
        levels = _enum_levels(con, table, name) if declared == "STRING" else ()
        if levels:
            declared = _enum_type(levels)
            enums.append(name)
        schema.append({"name": name, "type": declared, "required": nullable == "NO"})
    return schema, enums


def _semantics(table: str, key: Sequence[str], tables: Sequence[str]) -> Dict[str, Any]:
    semantics: Dict[str, Any] = {}
    if len(key) == 1:
        semantics["entities"] = [{"name": table, "type": "primary", "expr": key[0]}]
    relationships = [fk for fk in tpcds.foreign_keys(tables) if fk.table == table]
    if relationships:
        semantics["relationships"] = [
            {
                "name": fk.name,
                "to": fk.target,
                "fromColumns": list(fk.columns),
                "toColumns": list(fk.target_columns),
                # A foreign key that is the table's whole primary key (a return's
                # sale line) references each parent row at most once.
                "cardinality": "one_to_one" if sorted(fk.columns) == sorted(key) else "many_to_one",
            }
            for fk in relationships
        ]
    return semantics


def _expose(
    table: str,
    schema: List[Dict[str, Any]],
    invariants: List[Dict[str, Any]],
    key: Sequence[str],
    tables: Sequence[str],
    region: str,
) -> Dict[str, Any]:
    """One table's expose: its schema, its rules when it has any, and its keys."""
    block: Dict[str, Any] = {"schema": schema}
    if invariants:
        block["invariants"] = invariants
    expose: Dict[str, Any] = {
        "exposeId": table,
        "kind": "table",
        "binding": {
            "platform": "aws",
            "format": "glue_table",
            "location": {"database": LOGICAL_DATABASE, "table": table, "region": region},
        },
        "contract": block,
    }
    semantics = _semantics(table, key, tables)
    if semantics:
        expose["semantics"] = semantics
    return expose


def write_contract(
    database: Path, tables: Sequence[str], out: Path, *, region: str
) -> Dict[str, Any]:
    """Write the FLUID 0.7.7 contract: one expose per table, with its keys and rules.

    The calendar and the clock carry the rules of :mod:`flux_e2e.rules`, each with the
    band this source needs.

    The binding names a logical Glue database, the same for every run: the
    contract is part of what FLUX hashes, so a run-specific name would change
    every draw from one run to the next.
    """
    import duckdb

    exposes: List[Dict[str, Any]] = []
    enums: Dict[str, List[str]] = {}
    invariants: Dict[str, List[Dict[str, Any]]] = {}
    with duckdb.connect(str(database), read_only=True) as con:
        primary = {
            table: list(columns)
            for table, columns in con.execute(
                "SELECT table_name, constraint_column_names FROM duckdb_constraints() "
                "WHERE constraint_type = 'PRIMARY KEY' AND schema_name = 'main'"
            ).fetchall()
        }
        for table in tables:
            schema, enums[table] = _schema(con, table)
            invariants[table] = rules.contract_invariants(
                con, table, schema, minimum_cell=MINIMUM_CELL
            )
            exposes.append(
                _expose(table, schema, invariants[table], primary.get(table, []), tables, region)
            )
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "tpcds_shaped.store_channel",
        "name": "TPC-DS-shaped store channel",
        "description": "A TPC-DS-shaped source generated with DuckDB's tpcds extension.",
        "metadata": {"owner": {"team": "flux-e2e"}},
        "exposes": exposes,
    }
    refuse_floats(contract, "the contract")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return {
        "primaryKeys": {table: primary.get(table, []) for table in tables},
        "relationships": len(tpcds.foreign_keys(tables)),
        "enumColumns": {table: names for table, names in enums.items() if names},
        "rules": {
            table: [entry["id"] for entry in entries]
            for table, entries in invariants.items()
            if entries
        },
    }


def refuse_floats(value: Any, where: str) -> None:
    """Refuse a float anywhere in *value*, before a run spends its profile on it.

    The contract declares keys, so ``bundle.py`` pins its digest, and the reference validator
    and the engine canonicalise a YAML float differently (KERNEL-SPEC, keys): a float there
    makes ``fluxsim validate`` refuse the pin (``FLX-3101``). Write an integer or a string.
    """
    if isinstance(value, float):
        raise ValueError(f"{where} holds the float {value!r}: write an integer or a string")
    if isinstance(value, Mapping):
        for key, item in value.items():
            refuse_floats(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            refuse_floats(item, f"{where}[{index}]")


def profile_tables(
    fluid: Path, contract: Path, parquet_dir: Path, tables: Sequence[str], out_dir: Path
) -> List[Path]:
    """Run ``fluid profile`` once per table, each on its own Parquet copy, in parallel.

    Each call reads every value of its table exactly, on one core, so the tables
    run side by side and the largest one sets the time.
    """
    from concurrent.futures import ThreadPoolExecutor

    out_dir.mkdir(parents=True, exist_ok=True)

    def one(table: str) -> Path:
        target = out_dir / f"{table}.profile.json"
        subprocess.run(
            [
                str(fluid),
                "profile",
                str(contract),
                "--expose",
                table,
                "--source",
                str(parquet_dir / f"{table}.parquet"),
                "--out",
                str(target),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return target

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, tables))


def merge_profiles(documents: Sequence[Path], out: Path) -> str:
    """One profile with every table: a Simulation pins exactly one.

    ``fluid profile`` writes one expose per document. The documents share their
    producer, grid and disclosure rules, so their tables are joined as they are,
    and a document that differs in any of those is refused rather than mixed in.
    """
    merged: Dict[str, Any] = {}
    for path in documents:
        document = json.loads(path.read_text(encoding="utf-8"))
        head = {key: value for key, value in document.items() if key != "tables"}
        if not merged:
            merged = {**head, "tables": {}}
        elif head != {key: value for key, value in merged.items() if key != "tables"}:
            raise ValueError(f"{path.name}: producer, grid or disclosure rules differ")
        for table, entry in document["tables"].items():
            if table in merged["tables"]:
                raise ValueError(f"{path.name}: table {table!r} is profiled twice")
            merged["tables"][table] = entry
    canonical = rfc8785.dumps(merged)
    out.write_bytes(canonical)
    out.chmod(0o600)
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def read_profile_metrics(document: Mapping[str, Any]) -> Dict[str, Any]:
    """How much of the profile was published: for the report, never a value."""
    published = withheld = 0
    for table in document["tables"].values():
        for column in table.get("columns", {}).values():
            for metric in column.get("metrics", {}).values():
                if metric.get("status") == "F":
                    published += 1
                else:
                    withheld += 1
    return {"metricsPublished": published, "metricsWithheld": withheld}
