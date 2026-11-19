# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The source's shape: which TPC-DS-shaped tables are in scope, and their keys.

The tables come from DuckDB's tpcds extension (``CALL dsdgen(sf=..., keys=true)``).
That call declares each table's primary key and no foreign key, so the foreign
keys are listed here, from the TPC-DS schema's own table definitions. The key
judge measures every one of them on the source too, so a key listed wrongly shows
up as orphans on the source side, not as a FLUX defect.

Wording: this is a TPC-DS-shaped source generated with DuckDB's tpcds
extension. Nothing the harness reports is a TPC-DS benchmark result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

#: One sales channel's star: store sales and returns with every table they reach.
STORE_STAR: Tuple[str, ...] = (
    "store_sales",
    "store_returns",
    "date_dim",
    "time_dim",
    "item",
    "customer",
    "customer_address",
    "customer_demographics",
    "household_demographics",
    "income_band",
    "store",
    "promotion",
    "reason",
)

#: The scopes a run can take, by name.
SCOPES: Dict[str, Tuple[str, ...]] = {"store": STORE_STAR}


@dataclass(frozen=True)
class ForeignKey:
    """One relationship: ``table(columns)`` references ``target(target_columns)``."""

    table: str
    columns: Tuple[str, ...]
    target: str
    target_columns: Tuple[str, ...]

    @property
    def name(self) -> str:
        return f"{self.table}_{'_'.join(self.columns)}_to_{self.target}"


def _fk(table: str, column: str, target: str, target_column: str) -> ForeignKey:
    return ForeignKey(table, (column,), target, (target_column,))


#: The foreign keys among the store star's tables.
FOREIGN_KEYS: Tuple[ForeignKey, ...] = (
    _fk("store_sales", "ss_sold_date_sk", "date_dim", "d_date_sk"),
    _fk("store_sales", "ss_sold_time_sk", "time_dim", "t_time_sk"),
    _fk("store_sales", "ss_item_sk", "item", "i_item_sk"),
    _fk("store_sales", "ss_customer_sk", "customer", "c_customer_sk"),
    _fk("store_sales", "ss_cdemo_sk", "customer_demographics", "cd_demo_sk"),
    _fk("store_sales", "ss_hdemo_sk", "household_demographics", "hd_demo_sk"),
    _fk("store_sales", "ss_addr_sk", "customer_address", "ca_address_sk"),
    _fk("store_sales", "ss_store_sk", "store", "s_store_sk"),
    _fk("store_sales", "ss_promo_sk", "promotion", "p_promo_sk"),
    _fk("store_returns", "sr_returned_date_sk", "date_dim", "d_date_sk"),
    _fk("store_returns", "sr_return_time_sk", "time_dim", "t_time_sk"),
    _fk("store_returns", "sr_item_sk", "item", "i_item_sk"),
    _fk("store_returns", "sr_customer_sk", "customer", "c_customer_sk"),
    _fk("store_returns", "sr_cdemo_sk", "customer_demographics", "cd_demo_sk"),
    _fk("store_returns", "sr_hdemo_sk", "household_demographics", "hd_demo_sk"),
    _fk("store_returns", "sr_addr_sk", "customer_address", "ca_address_sk"),
    _fk("store_returns", "sr_store_sk", "store", "s_store_sk"),
    _fk("store_returns", "sr_reason_sk", "reason", "r_reason_sk"),
    ForeignKey(
        "store_returns",
        ("sr_item_sk", "sr_ticket_number"),
        "store_sales",
        ("ss_item_sk", "ss_ticket_number"),
    ),
    _fk("customer", "c_current_cdemo_sk", "customer_demographics", "cd_demo_sk"),
    _fk("customer", "c_current_hdemo_sk", "household_demographics", "hd_demo_sk"),
    _fk("customer", "c_current_addr_sk", "customer_address", "ca_address_sk"),
    _fk("customer", "c_first_shipto_date_sk", "date_dim", "d_date_sk"),
    _fk("customer", "c_first_sales_date_sk", "date_dim", "d_date_sk"),
    _fk("household_demographics", "hd_income_band_sk", "income_band", "ib_income_band_sk"),
    _fk("store", "s_closed_date_sk", "date_dim", "d_date_sk"),
    _fk("promotion", "p_start_date_sk", "date_dim", "d_date_sk"),
    _fk("promotion", "p_end_date_sk", "date_dim", "d_date_sk"),
    _fk("promotion", "p_item_sk", "item", "i_item_sk"),
)


def foreign_keys(tables: Sequence[str]) -> List[ForeignKey]:
    """The relationships whose two ends are both in *tables*."""
    present = set(tables)
    return [fk for fk in FOREIGN_KEYS if fk.table in present and fk.target in present]
