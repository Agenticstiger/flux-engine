# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The privacy judge's owner side: a holdout of people, and one row per person.

**Holdout.** Before anything is profiled, a share of the customers is moved out of
the source, together with every sales and returns row that refers to them (and
every return of a moved sale). FLUX is profiled and generated from what is left.
The held-out people are the control set an attack is measured against: an attack
that does as well on people FLUX never saw as on the people it was built from has
learned the population, not the individuals. Customers are chosen by a keyed
hash of their key, so a seed gives the same split on any machine.

Addresses and demographics are not held out: other customers live at the same
address or share the same demographics, and other customers' sales refer to them.
What leaves is every link from a held-out person to them. The split records how
many of the held-out people's addresses no kept customer lives at.

**Person view.** Each side (the part FLUX was built from, the synthetic copy, the
holdout) becomes one row per customer: the customer joined with its address and
its demographics, plus a count of its store sales and its total spend. The
attacks run on these rows. The columns an attacker is assumed to know already
(auxiliary) and the columns they try to learn (secret) are fixed below and
recorded with every run.

The three attacks follow the anonymisation criteria the EDPB draws on (isolation,
linkage and inference; singling out, linkability and inference in the Article 29
Working Party's Opinion 05/2014). They measure a method on this TPC-DS-shaped
source, which holds no real people.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Union

#: What an attacker is assumed to know about a person.
AUXILIARY = ("c_birth_year", "c_birth_month", "cd_gender", "ca_city", "ca_state", "ca_zip")
#: What an attacker tries to learn, and whether it is a number (a regression).
SECRETS: Mapping[str, bool] = {
    "cd_credit_rating": False,
    "cd_purchase_estimate": True,
    "cd_education_status": False,
    "total_spend": True,
}
#: The two datasets a linkage attacker holds, by their columns. Three columns each
#: leave many people tied; they stay so that earlier runs remain comparable.
LINKAGE_SETS = (("c_birth_year", "c_birth_month", "cd_gender"), ("ca_city", "ca_state", "ca_zip"))
#: Wider sets for a second linkage attack: marital status and education join the
#: first dataset, and the preferred-customer flag joins the second.
WIDE_LINKAGE_SETS = (
    ("c_birth_year", "c_birth_month", "cd_gender", "cd_marital_status", "cd_education_status"),
    ("ca_city", "ca_state", "ca_zip", "c_preferred_cust_flag"),
)
#: Columns of the person view beyond the auxiliary and secret ones.
OTHER = ("cd_marital_status", "c_preferred_cust_flag", "sales_count")
#: Tables a person view reads.
PERSON_TABLES = ("customer", "customer_address", "customer_demographics", "store_sales")


def held_out(keys: Sequence[int], fraction: float, seed: int) -> List[int]:
    """The keys a keyed hash puts in the holdout: about *fraction* of them."""
    if not 0 < fraction < 1:
        raise ValueError("a holdout fraction lies strictly between 0 and 1")
    cut = int(fraction * 2**64)
    chosen = []
    for key in keys:
        digest = hashlib.sha256(f"{seed}:{key}".encode("ascii")).digest()
        if int.from_bytes(digest[:8], "big") < cut:
            chosen.append(key)
    return sorted(chosen)


def split_holdout(database: Path, fraction: float, seed: int) -> Dict[str, Any]:
    """Move the held-out customers and their rows into the ``holdout`` schema."""
    import duckdb

    with duckdb.connect(str(database)) as con:
        keys = [row[0] for row in con.execute("SELECT c_customer_sk FROM customer").fetchall()]
        chosen = held_out(keys, fraction, seed)
        con.execute("CREATE SCHEMA holdout")
        con.execute("CREATE TABLE holdout.held AS SELECT unnest(?::BIGINT[]) AS sk", [chosen])
        held = "IN (SELECT sk FROM holdout.held)"
        con.execute(
            f"CREATE TABLE holdout.customer AS SELECT * FROM customer WHERE c_customer_sk {held}"
        )
        con.execute(
            f"CREATE TABLE holdout.store_sales AS SELECT * FROM store_sales WHERE ss_customer_sk {held}"
        )
        # A return goes with its customer, and with its sale when that sale moved.
        moved_return = (
            f"sr_customer_sk {held} OR EXISTS (SELECT 1 FROM holdout.store_sales s "
            "WHERE s.ss_item_sk = sr_item_sk AND s.ss_ticket_number = sr_ticket_number)"
        )
        con.execute(
            f"CREATE TABLE holdout.store_returns AS SELECT * FROM store_returns WHERE {moved_return}"
        )
        con.execute(f"DELETE FROM store_returns WHERE {moved_return}")
        con.execute(f"DELETE FROM store_sales WHERE ss_customer_sk {held}")
        con.execute(f"DELETE FROM customer WHERE c_customer_sk {held}")
        # Addresses and demographics stay: other customers and other sales refer to
        # them. Count the held-out people's addresses no kept customer lives at.
        (alone,) = con.execute(
            "SELECT count(DISTINCT c_current_addr_sk) FROM holdout.customer "
            "WHERE c_current_addr_sk NOT IN (SELECT c_current_addr_sk FROM customer "
            "WHERE c_current_addr_sk IS NOT NULL)"
        ).fetchone()
        counts = {
            table: con.execute(f"SELECT count(*) FROM holdout.{table}").fetchone()[0]
            for table in ("customer", "store_sales", "store_returns")
        }
    return {
        "fraction": fraction,
        "seed": seed,
        "customers": len(keys),
        "heldOut": counts,
        "addressesOfHeldOutOnly": alone,
    }


def export_holdout(database: Path, out_dir: Path) -> None:
    """Write the held-out tables to ``<out_dir>/<table>.parquet``."""
    import duckdb

    out_dir.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database), read_only=True) as con:
        for table in ("customer", "store_sales", "store_returns"):
            target = out_dir / f"{table}.parquet"
            con.execute(f"COPY holdout.{table} TO '{target}' (FORMAT parquet, COMPRESSION zstd)")


def view_columns() -> List[str]:
    return [*AUXILIARY, *SECRETS, *OTHER]


def _scan(source: Union[Path, Sequence[Path]]) -> str:
    """A ``read_parquet`` of one Parquet file or of a list of them."""
    paths = [source] if isinstance(source, Path) else list(source)
    if not paths:
        raise ValueError("a person view needs at least one Parquet file per table")
    if any("'" in str(path) for path in paths):
        raise ValueError(f"a Parquet path cannot hold a quote: {paths}")
    return "read_parquet([" + ", ".join(f"'{path}'" for path in paths) + "])"


def person_view(sources: Mapping[str, Union[Path, Sequence[Path]]], out: Path) -> int:
    """One row per customer of *sources*, written to *out*; returns the row count.

    Duplicated dimension keys (the synthetic copy may repeat a key) resolve to one
    row each, picked deterministically, so the view keeps one row per customer row.
    """
    import duckdb

    sql = f"""
        WITH addr AS (
            SELECT * FROM {_scan(sources["customer_address"])}
            QUALIFY row_number() OVER (
                PARTITION BY ca_address_sk ORDER BY ca_city, ca_state, ca_zip) = 1
        ), demo AS (
            SELECT * FROM {_scan(sources["customer_demographics"])}
            QUALIFY row_number() OVER (
                PARTITION BY cd_demo_sk
                ORDER BY cd_gender, cd_marital_status, cd_education_status,
                         cd_purchase_estimate, cd_credit_rating) = 1
        ), sales AS (
            SELECT ss_customer_sk AS sk, count(*) AS sales_count,
                   CAST(sum(ss_net_paid) AS DOUBLE) AS total_spend
            FROM {_scan(sources["store_sales"])}
            WHERE ss_customer_sk IS NOT NULL GROUP BY 1
        )
        SELECT
            CAST(c.c_birth_year AS DOUBLE) AS c_birth_year,
            CAST(c.c_birth_month AS DOUBLE) AS c_birth_month,
            d.cd_gender, a.ca_city, a.ca_state, a.ca_zip,
            d.cd_credit_rating,
            CAST(d.cd_purchase_estimate AS DOUBLE) AS cd_purchase_estimate,
            d.cd_education_status,
            coalesce(s.total_spend, 0.0) AS total_spend,
            d.cd_marital_status, c.c_preferred_cust_flag,
            CAST(coalesce(s.sales_count, 0) AS DOUBLE) AS sales_count
        FROM {_scan(sources["customer"])} c
        LEFT JOIN addr a ON c.c_current_addr_sk = a.ca_address_sk
        LEFT JOIN demo d ON c.c_current_cdemo_sk = d.cd_demo_sk
        LEFT JOIN sales s ON c.c_customer_sk = s.sk
        ORDER BY c.c_customer_sk, c.c_birth_year, c.c_birth_month
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as con:
        con.execute(f"COPY ({sql}) TO '{out}' (FORMAT parquet)")
        (rows,) = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()
    return int(rows)


def spec(attacks: int, seed: int) -> Dict[str, Any]:
    """What the attacks read: recorded with the run, and handed to the private judge."""
    return {
        "auxiliary": list(AUXILIARY),
        "secrets": dict(SECRETS),
        "linkageSets": [list(columns) for columns in LINKAGE_SETS],
        "wideLinkageSets": [list(columns) for columns in WIDE_LINKAGE_SETS],
        "columns": view_columns(),
        "attacks": attacks,
        "seed": seed,
    }
