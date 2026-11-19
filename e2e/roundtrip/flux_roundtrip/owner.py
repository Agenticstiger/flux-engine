# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The data owner's side: the truth's rows to Parquet, then one `fluid profile` call for the product.

Everything here reads records, so on a real project it runs only where the data lives. What
leaves for the generator side is the FLUID contract and the profile document.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import yaml

_DECIMAL = re.compile(r"^DECIMAL\(\d+,\s*\d+\)$", re.IGNORECASE)


def duckdb_type(contract_type: str) -> str:
    """The DuckDB type for one FLUID column type of this harness's contract."""
    declared = contract_type.strip()
    upper = declared.upper()
    if upper in ("INT", "INTEGER"):
        return "INTEGER"
    if upper == "DATE":
        return "DATE"
    if _DECIMAL.match(declared):
        return upper.replace(" ", "")
    if upper.startswith("ENUM(") or upper in ("STRING", "VARCHAR"):
        return "VARCHAR"
    raise ValueError(f"no DuckDB type for the contract type {contract_type!r}")


def read_schema(contract: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Each expose's schema, in declared order."""
    document = yaml.safe_load(contract.read_text(encoding="utf-8"))
    return {expose["exposeId"]: expose["contract"]["schema"] for expose in document["exposes"]}


def read_emit(run_dir: Path, table: str, columns: Sequence[str]) -> List[Dict[str, Any]]:
    """One expose's rows from a run's ``emits/<expose>.ndjson``, as one dict per row.

    The engine writes a JSON array per row, in schema order.
    """
    rows: List[Dict[str, Any]] = []
    with (run_dir / "emits" / f"{table}.ndjson").open(encoding="utf-8") as handle:
        for line in handle:
            values = json.loads(line)
            if len(values) != len(columns):
                raise ValueError(f"{table}: a row holds {len(values)} values for {len(columns)}")
            rows.append(dict(zip(columns, values, strict=True)))
    return rows


def export_parquet(run_dir: Path, contract: Path, out_dir: Path, scratch: Path) -> Dict[str, int]:
    """Write each expose of a run to ``<out_dir>/<table>.parquet``; return the row counts."""
    import duckdb

    out_dir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    counts: Dict[str, int] = {}
    for table, schema in read_schema(contract).items():
        names = [column["name"] for column in schema]
        rows = read_emit(run_dir, table, names)
        objects = scratch / f"{table}.objects.ndjson"
        objects.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        types = ", ".join(f"'{c['name']}': '{duckdb_type(c['type'])}'" for c in schema)
        target = out_dir / f"{table}.parquet"
        with duckdb.connect() as con:
            con.execute(
                f"COPY (SELECT * FROM read_json('{objects}', format='newline_delimited', "
                f"columns={{{types}}})) TO '{target}' (FORMAT parquet, COMPRESSION zstd)"
            )
        counts[table] = len(rows)
    return counts


def profile_product(
    fluid: str, contract: Path, parquet_dir: Path, tables: Sequence[str], out: Path
) -> str:
    """Run ``fluid profile`` once for the whole product, with its default settings.

    Each expose gets its own ``--source <exposeId>=<location>``, so one call writes the one
    document a Simulation pins. Returns the document's digest.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [fluid, "profile", str(contract)]
    for table in tables:
        command += ["--expose", table, "--source", f"{table}={parquet_dir / f'{table}.parquet'}"]
    command += ["--out", str(out)]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    return "sha256:" + hashlib.sha256(out.read_bytes()).hexdigest()


def fluid_executable() -> str:
    """The ``fluid`` console script next to this interpreter, else the one on PATH."""
    import sys

    beside = Path(sys.executable).parent / "fluid"
    if beside.exists():
        return str(beside)
    found = shutil.which("fluid")
    if found is None:
        raise RuntimeError("fluid is not installed: run `uv sync` in e2e/roundtrip")
    return found


def table_rows(profile: Mapping[str, Any]) -> Dict[str, int]:
    return {name: table.get("rowCount") for name, table in profile["tables"].items()}
