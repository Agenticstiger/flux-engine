# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A prototype for ADR 0011: synthetic rows that follow their key, their ticket and their parent.

Since step 1, FLUX generates the calendar's and the clock's rules itself, the FLUX-side
flux-expr/3 ones included (see :mod:`flux_e2e.rules`), so it generates everything the
``reference`` layer did and that layer is superseded. For the rest, this module measures what the rules would buy on a finished run,
before the engine and ``fluid profile`` take them over. It rewrites a copy of FLUX's output in
three layers, each on top of the one before:

* ``reference``: a dimension whose attributes are a function of its key. ``date_dim`` is a
  calendar keyed by the Julian day and ``time_dim`` a clock keyed by the second of the day, so
  each attribute is recomputed from the key, as dsdgen computes it (except the day of the week in
  January and February 1900 and on 1 January 2100, which no fact names). This is reference
  data: no statistic is read.
* ``ticket``: a functional dependency inside a fact. The lines of one store ticket share its
  date, time, customer, demographics, address and store, so every line of a synthetic ticket
  takes the values of the ticket's first line.
* ``parent``: a child row follows the parent row its key names. A store return keeps its sale's
  customer on the published share of rows, is dated a published lag after the sale, returns a
  published share of the quantity sold, and is priced at the sale's price.

A null a column drew on its own stays null in every layer, so the declared null fractions hold.

The owner side computes the statistics the ``parent`` layer reads, over the join of each return
to its sale. Each is a frequency table published only when every cell holds at least
``MINIMUM_CELL`` rows and as many distinct customers, and its counts are rounded to a multiple
of 5: ACRO's defaults for a frequency table, with the person counted as well as the row. The
rows a table leaves out (a null in what it reads, or a return with no sale) are checked as
cells too, pattern by pattern across the three tables, and never written. Any failing cell
withholds the tables, and the layer then leaves those columns as FLUX drew them. The generator
side reads those tables and FLUX's own output, never a record.

``python -m flux_e2e.follow --from-run <id> --run-id <new> --layers reference,ticket,parent``
clones a finished run, rewrites the clone's synthetic Parquet files, recomputes their digests
and clears the steps that read them (privacy, publish, the judges and SDMetrics), so
``python -m flux_e2e run --run-id <new>`` with the run's own settings publishes and judges the
rewritten copy. FLUX's own verify step is not run again: its result in the clone's report is
the original run's. A clone that fails half way is removed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_e2e import aws, generator
from flux_e2e.cli import _default_work_root
from flux_e2e.owner import MINIMUM_CELL

LAYERS = ("reference", "ticket", "parent")
#: ACRO's rounding base for a published count (``safe_round_base``).
ROUND_BASE = 5
#: The lag of a return after its sale, in days: declared bins [0, 10) to [180, 190).
LAG_EDGES: Tuple[int, ...] = tuple(range(0, 200, 10))
#: The share of the sold quantity a return takes back: declared bins (0, 1/10] to (9/10, 1].
QUANTITY_EDGES: Tuple[Fraction, ...] = tuple(Fraction(i, 10) for i in range(11))
#: The store-sales columns a ticket number determines (each ticket's lines share them).
TICKET_COLUMNS = (
    "ss_sold_date_sk",
    "ss_sold_time_sk",
    "ss_customer_sk",
    "ss_cdemo_sk",
    "ss_hdemo_sk",
    "ss_addr_sk",
    "ss_store_sk",
)
#: Steps a rewritten clone must run again.
_CLEARED_STEPS = ("privacy", "publish", "judge", "sdmetrics")
#: A generator output name the SQL below may quote.
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+\.parquet")
_TWO_64 = "18446744073709551616.0"

# ---- owner side ------------------------------------------------------------------------

_JOINED = """
    FROM store_returns r
    JOIN store_sales s ON r.sr_item_sk = s.ss_item_sk AND r.sr_ticket_number = s.ss_ticket_number
"""
#: The joined rows each published table covers: those on which what it reads is known.
COVERS = {
    "customerSameAsSale": "r.sr_customer_sk IS NOT NULL AND s.ss_customer_sk IS NOT NULL",
    "lagDays": "r.sr_returned_date_sk IS NOT NULL AND s.ss_sold_date_sk IS NOT NULL",
    "quantityShare": (
        "r.sr_return_quantity IS NOT NULL AND s.ss_quantity IS NOT NULL AND s.ss_quantity > 0"
    ),
}


def round_count(count: int) -> int:
    """``count`` to the nearest multiple of ``ROUND_BASE``, halves up."""
    return (count + ROUND_BASE // 2) // ROUND_BASE * ROUND_BASE


def checked_cells(cells: Sequence[Tuple[int, int]]) -> Optional[List[int]]:
    """Rounded counts when every ``(rows, people)`` cell passes, else ``None``.

    A cell passes with at least ``MINIMUM_CELL`` rows and as many distinct people. A cell with
    no rows fails like any other (ACRO's ``zeros_are_disclosive``). One failing cell withholds
    the whole table, so no published cell can be subtracted from a published total.
    """
    if not cells or any(rows < MINIMUM_CELL or people < MINIMUM_CELL for rows, people in cells):
        return None
    return [round_count(rows) for rows, _ in cells]


def coverage_passes(con: Any) -> bool:
    """Whether every pattern of tables a return falls out of passes as a cell.

    The three tables are published together, so the rows one covers and another leaves out are
    found by subtracting their totals. Each pattern that holds a row, the returns that join no
    sale among them, is checked as one more cell and never written: the profiler's rule for the
    records a groupBy null leaves out (forge-cli ``fluid_build/profiler/disclosure.py``), carried
    across the tables of one relationship. A cell whose rows name no customer counts rows only.
    """
    flags = ", ".join(f"coalesce({cover}, false)" for cover in COVERS.values())
    cells = con.execute(
        f"SELECT {flags}, s.ss_item_sk IS NULL, count(*), count(DISTINCT r.sr_customer_sk), "
        "count(r.sr_customer_sk) FROM store_returns r LEFT JOIN store_sales s "
        "ON r.sr_item_sk = s.ss_item_sk AND r.sr_ticket_number = s.ss_ticket_number GROUP BY ALL"
    ).fetchall()
    return all(
        rows >= MINIMUM_CELL and (people >= MINIMUM_CELL or named == 0)
        for *_, rows, people, named in cells
    )


def _binned(con: Any, where: str, bucket: str, buckets: int) -> Optional[List[int]]:
    rows = con.execute(
        f"SELECT {bucket} AS b, count(*), count(DISTINCT r.sr_customer_sk) {_JOINED} "
        f"WHERE {where} GROUP BY 1 ORDER BY 1"
    ).fetchall()
    found = {b: (n, people) for b, n, people in rows}
    if any(b is None or not 0 <= b < buckets for b in found):
        return None  # a value outside the declared bins: the declaration does not cover the data
    return checked_cells([found.get(b, (0, 0)) for b in range(buckets)])


def owner_statistics(database: Path) -> Dict[str, Any]:
    """The tables the ``parent`` layer reads, computed where the data lives, after the checks."""
    import duckdb

    with duckdb.connect(str(database), read_only=True) as con:
        covered = coverage_passes(con)
        same, other = (
            con.execute(
                "SELECT count(*), count(DISTINCT r.sr_customer_sk) "
                f"{_JOINED} WHERE {COVERS['customerSameAsSale']} "
                f"AND (r.sr_customer_sk = s.ss_customer_sk) = {flag}"
            ).fetchone()
            for flag in ("true", "false")
        )
        lag = _binned(
            con,
            COVERS["lagDays"],
            f"floor((r.sr_returned_date_sk - s.ss_sold_date_sk) / {LAG_EDGES[1]})::BIGINT",
            len(LAG_EDGES) - 1,
        )
        quantity = _binned(
            con,
            COVERS["quantityShare"],
            "ceil(r.sr_return_quantity * 10.0 / s.ss_quantity)::BIGINT - 1",
            len(QUANTITY_EDGES) - 1,
        )
    customer = checked_cells([same, other]) if covered else None
    lag = lag if covered else None
    quantity = quantity if covered else None
    return {
        "rule": "acro-frequency-table",
        "minimumCell": MINIMUM_CELL,
        "roundBase": ROUND_BASE,
        "unit": "rows and distinct customers",
        "store_returns": {
            "customerSameAsSale": (
                {"same": customer[0], "other": customer[1]} if customer else {"status": "D"}
            ),
            "lagDays": ({"edges": list(LAG_EDGES), "counts": lag} if lag else {"status": "D"}),
            "quantityShare": (
                {"edges": [str(edge) for edge in QUANTITY_EDGES], "counts": quantity}
                if quantity
                else {"status": "D"}
            ),
        },
    }


# ---- generator side --------------------------------------------------------------------


def _uniform(*parts: str) -> str:
    """A uniform draw in [0, 1) keyed by the row's key columns and a label.

    The parts are hashed as one string. DuckDB's ``hash(a, b, c)`` combines the hashes of its
    arguments, so two labels over one key give draws that move together (a correlation of
    about -0.6 on store returns), and a bin's draw would then decide the value inside it.
    """
    return f"(hash(concat_ws(':', {', '.join(parts)})) / {_TWO_64})"


def _pick(edges: Sequence[Any], counts: Sequence[int], draw: str, within: str) -> str:
    """SQL drawing a value from a histogram: a bin by its share, then uniformly inside it."""
    total = sum(counts)
    arms, running = [], 0
    for low, high, count in zip(edges[:-1], edges[1:], counts, strict=True):
        if not count:
            continue
        running += count
        span = f"({float(low)!r} + {within} * {float(high - low)!r})"
        arms.append(f"WHEN {draw} < {running / total!r} THEN {span}")
    return "CASE " + " ".join(arms) + f" ELSE {float(edges[-1])!r} END"


def _by_bin(counts: Sequence[int], draw: str, arm: Any) -> str:
    """SQL choosing a bin by its share of ``counts`` and giving ``arm(i)`` for the bin chosen."""
    total = sum(counts)
    chosen = [i for i, count in enumerate(counts) if count]
    if len(chosen) == 1:
        return arm(chosen[0])
    arms, running = [], 0
    for i in chosen[:-1]:
        running += counts[i]
        arms.append(f"WHEN {draw} < {running / total!r} THEN {arm(i)}")
    return "CASE " + " ".join(arms) + f" ELSE {arm(chosen[-1])} END"


def _calendar(columns: Mapping[str, str]) -> Dict[str, str]:
    dd = "__dd"
    qoy = (
        f"((month({dd}) - 1) // 3 + 1 - CASE WHEN day({dd}) = 1 AND month({dd}) IN (4, 7, 10) "
        f"AND year({dd}) % 4 <> 0 THEN 1 ELSE 0 END)"
    )
    quarter_seq = f"((year({dd}) - 1900) * 4 + month({dd}) // 3 + 1)"
    week_seq = "((d_date_sk - 2415015) // 7)"
    derived = {
        "d_date": dd,
        "d_year": f"year({dd})",
        "d_moy": f"month({dd})",
        "d_dom": f"day({dd})",
        "d_qoy": qoy,
        "d_dow": f"dayofweek({dd})",
        "d_day_name": f"dayname({dd})",
        "d_month_seq": f"((year({dd}) - 1900) * 12 + month({dd}) - 1)",
        "d_week_seq": week_seq,
        "d_quarter_seq": quarter_seq,
        "d_fy_year": f"year({dd})",
        "d_fy_quarter_seq": quarter_seq,
        "d_fy_week_seq": week_seq,
        "d_quarter_name": f"(year({dd})::VARCHAR || 'Q' || {qoy}::VARCHAR)",
        "d_weekend": f"CASE WHEN dayofweek({dd}) IN (5, 6) THEN 'Y' ELSE 'N' END",
        "d_first_dom": f"(d_date_sk - day({dd}) + 1)",
        "d_same_day_ly": f"(d_date_sk - ({dd} - ({dd} - INTERVAL 1 YEAR)::DATE))",
    }
    return {name: expr for name, expr in derived.items() if name in columns}


def _clock(columns: Mapping[str, str]) -> Dict[str, str]:
    hour = "(t_time_sk // 3600)"
    derived = {
        "t_time": "t_time_sk",
        "t_hour": hour,
        "t_minute": "((t_time_sk // 60) % 60)",
        "t_second": "(t_time_sk % 60)",
        "t_am_pm": "CASE WHEN t_time_sk < 43200 THEN 'AM' ELSE 'PM' END",
        "t_shift": (
            f"CASE WHEN {hour} BETWEEN 7 AND 14 THEN 'first' "
            f"WHEN {hour} BETWEEN 15 AND 22 THEN 'second' ELSE 'third' END"
        ),
        "t_sub_shift": (
            f"CASE WHEN {hour} < 6 THEN 'night' WHEN {hour} < 12 THEN 'morning' "
            f"WHEN {hour} < 17 THEN 'afternoon' ELSE 'evening' END"
        ),
        "t_meal_time": (
            f"CASE WHEN {hour} BETWEEN 6 AND 9 THEN 'breakfast' "
            f"WHEN {hour} BETWEEN 12 AND 14 THEN 'lunch' "
            f"WHEN {hour} BETWEEN 17 AND 19 THEN 'dinner' END"
        ),
    }
    return {name: expr for name, expr in derived.items() if name in columns}


class _Copy:
    """One synthetic copy's tables, read from and written back to FLUX's Parquet files."""

    def __init__(self, gen_root: Path) -> None:
        import duckdb

        self.root = gen_root
        self.con = duckdb.connect()

    def files(self, table: str) -> List[Path]:
        files = generator.output_files(self.root, table)
        if not files:
            raise ValueError(f"the synthetic copy has no {table} output")
        for path in files:
            # The generator names these files and the SQL below quotes them, so a name with a
            # quote, a glob character or a separator could write SQL of its own. privacy.py
            # refuses a quoted path for the same reason.
            if not _SAFE_NAME.fullmatch(path.name) or "'" in str(path):
                raise ValueError(f"refusing a generator output named {path.name!r}")
        return files

    def read(self, table: str) -> str:
        paths = ", ".join(f"'{path}'" for path in self.files(table))
        return f"read_parquet([{paths}])"

    def types(self, table: str) -> Dict[str, str]:
        return {
            name: kind
            for name, kind, *_ in self.con.execute(
                f"DESCRIBE SELECT * FROM {self.read(table)}"
            ).fetchall()
        }

    def rewrite(self, table: str, select: str) -> None:
        """Write ``select`` (which reads ``src``) as the table's only Parquet file."""
        old = self.files(table)
        folder = old[0].parent
        target = folder / "follow-00000.parquet.tmp"
        self.con.execute(f"CREATE OR REPLACE TEMP VIEW src AS SELECT * FROM {self.read(table)}")
        self.con.execute(f"COPY ({select}) TO '{target}' (FORMAT parquet, COMPRESSION zstd)")
        for path in old:
            path.unlink()
        target.rename(folder / "follow-00000.parquet")


def _replace(types: Mapping[str, str], derived: Mapping[str, str]) -> str:
    return ", ".join(f"CAST({expr} AS {types[name]}) AS {name}" for name, expr in derived.items())


def _reference(copy: _Copy) -> Dict[str, Any]:
    date_types = copy.types("date_dim")
    calendar = _calendar(date_types)
    copy.rewrite(
        "date_dim",
        "SELECT * EXCLUDE (__dd) REPLACE (" + _replace(date_types, calendar) + ") FROM "
        "(SELECT *, DATE '1970-01-01' + CAST(d_date_sk - 2440588 AS INTEGER) AS __dd FROM src)",
    )
    time_types = copy.types("time_dim")
    clock = _clock(time_types)
    copy.rewrite("time_dim", "SELECT * REPLACE (" + _replace(time_types, clock) + ") FROM src")
    return {"date_dim": sorted(calendar), "time_dim": sorted(clock)}


def _ticket(copy: _Copy) -> Dict[str, Any]:
    types = copy.types("store_sales")
    columns = [name for name in TICKET_COLUMNS if name in types]
    firsts = ", ".join(
        f"arg_min({name}, ss_item_sk) FILTER (WHERE {name} IS NOT NULL) AS first_{name}"
        for name in columns
    )
    derived = {
        name: f"CASE WHEN src.{name} IS NULL THEN NULL ELSE coalesce(t.first_{name}, src.{name}) END"
        for name in columns
    }
    copy.rewrite(
        "store_sales",
        "SELECT src.* REPLACE (" + _replace(types, derived) + ") FROM src LEFT JOIN "
        f"(SELECT ss_ticket_number, {firsts} FROM src GROUP BY 1) t USING (ss_ticket_number)",
    )
    return {"store_sales": columns}


#: The key of a store return, which keys its draws.
_RETURN_KEY = ("r.sr_item_sk", "r.sr_ticket_number")


def _follow_customer(table: Mapping[str, Any], drawn: Dict[str, str], derived: Dict[str, str]):
    """The sale's customer, on the published share of the returns where both are known."""
    if "same" not in table:
        return
    share = table["same"] / (table["same"] + table["other"])
    drawn["__customer_draw"] = _uniform(*_RETURN_KEY, repr("customer"))
    derived["sr_customer_sk"] = (
        "CASE WHEN j.sr_customer_sk IS NOT NULL AND j.__p_customer IS NOT NULL AND "
        f"j.__customer_draw < {share!r} THEN j.__p_customer ELSE j.sr_customer_sk END"
    )


def _follow_lag(
    table: Mapping[str, Any], drawn: Dict[str, str], derived: Dict[str, str], date_dim: str
) -> str:
    """The sale's date plus a lag from the published histogram; the join that snaps it."""
    if "counts" not in table:
        return ""
    days = _pick(
        table["edges"],
        table["counts"],
        _uniform(*_RETURN_KEY, repr("lag")),
        _uniform(*_RETURN_KEY, repr("lag-day")),
    )
    drawn["__target"] = f"s.ss_sold_date_sk + floor({days})::BIGINT"
    derived["sr_returned_date_sk"] = (
        "CASE WHEN j.sr_returned_date_sk IS NULL OR j.__target IS NULL "
        "THEN j.sr_returned_date_sk ELSE coalesce(k.d_date_sk, j.sr_returned_date_sk) END"
    )
    # The target moves down to the nearest key date_dim holds, so the key still names a row.
    return f" ASOF LEFT JOIN (SELECT d_date_sk FROM {date_dim}) k ON j.__target >= k.d_date_sk"


def _follow_quantity(table: Mapping[str, Any], drawn: Dict[str, str], derived: Dict[str, str]):
    """A published share of the sold quantity, and the amount at the sale's price.

    The owner put a return in bin ``i`` when ``edges[i] < returned / sold <= edges[i + 1]``,
    and both quantities are integers, so a bin is drawn first and then one of the integers it
    holds for this sale's quantity, uniformly. When it holds none (a small sold quantity), the
    nearest quantity above the bin's lower edge stands in.
    """
    if "counts" not in table:
        return
    edges = [Fraction(edge) for edge in table["edges"]]
    draw = _uniform(*_RETURN_KEY, repr("quantity"))
    drawn["__q_low"] = _by_bin(
        table["counts"],
        draw,
        lambda i: f"((s.ss_quantity * {edges[i].numerator}) // {edges[i].denominator} + 1)",
    )
    drawn["__q_high"] = _by_bin(
        table["counts"],
        draw,
        lambda i: f"((s.ss_quantity * {edges[i + 1].numerator}) // {edges[i + 1].denominator})",
    )
    drawn["__q_draw"] = _uniform(*_RETURN_KEY, repr("quantity-share"))
    drawn["__p_quantity"] = "s.ss_quantity"
    quantity = (
        "CASE WHEN j.__q_high >= j.__q_low "
        "THEN j.__q_low + floor(j.__q_draw * (j.__q_high - j.__q_low + 1))::BIGINT "
        "ELSE least(j.__p_quantity, greatest(1, j.__q_low)) END"
    )
    unknown = "j.sr_return_quantity IS NULL OR j.__p_quantity IS NULL OR j.__p_quantity < 1"
    derived["sr_return_quantity"] = (
        f"CASE WHEN {unknown} THEN j.sr_return_quantity ELSE {quantity} END"
    )
    derived["sr_return_amt"] = (
        f"CASE WHEN {unknown} OR j.sr_return_amt IS NULL OR j.__p_price IS NULL "
        f"THEN j.sr_return_amt ELSE round(j.__p_price * ({quantity}), 2) END"
    )


def _parent(copy: _Copy, statistics: Mapping[str, Any]) -> Dict[str, Any]:
    published = statistics["store_returns"]
    # A first pass joins each return to its sale and draws; the second sets the columns.
    drawn = {"__p_customer": "s.ss_customer_sk", "__p_price": "s.ss_sales_price"}
    derived: Dict[str, str] = {}
    _follow_customer(published["customerSameAsSale"], drawn, derived)
    snap = _follow_lag(published["lagDays"], drawn, derived, copy.read("date_dim"))
    _follow_quantity(published["quantityShare"], drawn, derived)
    if not derived:
        return {"store_returns": []}
    first = (
        "SELECT r.*, "
        + ", ".join(f"{expr} AS {name}" for name, expr in drawn.items())
        + f" FROM src r LEFT JOIN {copy.read('store_sales')} s "
        "ON r.sr_item_sk = s.ss_item_sk AND r.sr_ticket_number = s.ss_ticket_number"
    )
    types = copy.types("store_returns")
    copy.rewrite(
        "store_returns",
        f"SELECT j.* EXCLUDE ({', '.join(drawn)}) REPLACE ({_replace(types, derived)}) "
        f"FROM ({first}) j" + snap,
    )
    return {"store_returns": sorted(derived)}


def _check_layers(layers: Sequence[str]) -> None:
    unknown = [layer for layer in layers if layer not in LAYERS]
    if unknown or not layers:
        named = repr(unknown[0]) if unknown else "none"
        raise ValueError(f"unknown layer {named}; choose from {', '.join(LAYERS)}")


def apply(gen_root: Path, statistics: Mapping[str, Any], layers: Sequence[str]) -> Dict[str, Any]:
    """Rewrite a synthetic copy in place, layer by layer; say which columns each layer set."""
    _check_layers(layers)
    copy = _Copy(gen_root)
    done: Dict[str, Any] = {}
    for layer in LAYERS:  # in their own order, whatever order they were named in
        if layer == "reference" and layer in layers:
            done[layer] = _reference(copy)
        elif layer == "ticket" and layer in layers:
            done[layer] = _ticket(copy)
        elif layer == "parent" and layer in layers:
            done[layer] = _parent(copy, statistics)
    return done


# ---- a clone of a finished run ---------------------------------------------------------


def _copy_tree(source: Path, target: Path) -> None:
    # APFS clones the files (cp -c), so the copy costs no space until it is rewritten.
    if sys.platform == "darwin":
        subprocess.run(["cp", "-c", "-R", str(source), str(target)], check=True)
    else:
        shutil.copytree(source, target, symlinks=True)


def clone_run(work_root: Path, from_run: str, run_id: str, layers: Sequence[str]) -> Path:
    """Clone a finished run, rewrite the clone's copy, clear the steps that read the copy.

    Everything that can be refused is refused before the copy is made, and a clone that fails
    after it is removed, so a run id never names a half-made clone.
    """
    _check_layers(layers)
    source = work_root / aws.check_run_id(from_run)
    target = work_root / aws.check_run_id(run_id)
    if not (source / "run.json").is_file():
        raise ValueError(f"no run {from_run} under {work_root}")
    record = json.loads((source / "run.json").read_text(encoding="utf-8"))
    if "generate" not in record.get("steps", {}):
        raise ValueError(f"run {from_run} has not generated yet")
    if target.exists():
        raise ValueError(f"{target} exists; give a new run id")
    try:
        _copy_tree(source, target)
        for name in ("judges", "privacy", "report.md", "report.json"):
            path = target / name
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        statistics = owner_statistics(target / "source" / "tpcds.duckdb")
        published = target / "owner" / "follow.statistics.json"
        published.write_text(
            json.dumps(statistics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        published.chmod(0o600)
        done = apply(target / "generator", statistics, layers)
        record["runId"] = run_id
        for step in _CLEARED_STEPS:
            record["steps"].pop(step, None)
        for stale in ("published", "sweep"):  # the original's AWS record
            record.pop(stale, None)
        # The history compares runs by these digests, so they name the rewritten bytes.
        record["steps"]["generate"]["digests"] = generator.digests(target / "generator")
        config = record.setdefault("config", {})
        config["label"] = f"{config.get('label', '')} + follow prototype: {', '.join(layers)}"
        record["follow"] = {"fromRun": from_run, "layers": list(layers), "columns": done}
        (target / "run.json").write_text(
            json.dumps(record, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m flux_e2e.follow",
        description="Clone a finished run and rewrite its synthetic copy with the ADR 0011 layers.",
    )
    parser.add_argument("--work-root", type=Path, default=_default_work_root())
    parser.add_argument("--from-run", required=True, help="a run that has generated")
    parser.add_argument("--run-id", required=True, help="the clone's new run id")
    parser.add_argument(
        "--layers", default=",".join(LAYERS), help=f"comma-separated, from {', '.join(LAYERS)}"
    )
    args = parser.parse_args(argv)
    layers = [layer for layer in args.layers.split(",") if layer]
    import duckdb

    try:
        target = clone_run(args.work_root.expanduser(), args.from_run, args.run_id, layers)
    except ValueError as refusal:
        parser.exit(2, f"python -m flux_e2e.follow: error: {refusal}\n")
    except (RuntimeError, OSError, subprocess.CalledProcessError, duckdb.Error) as failure:
        parser.exit(1, f"python -m flux_e2e.follow: failed: {failure}\n")
    print(f"rewritten: {target}")
    print(
        f"publish and judge it: python -m flux_e2e run --run-id {args.run_id} <the run's settings>"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
