# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The boundary side of the fidelity check -- SEAM-FIDELITY-SPEC §4.

Runs where the source data lives. It fixes a record set, commits to it
per column (``C1``), forms the derived statistic ``S`` over it, commits to
that under a blinding value (``C2``), and counts the checks one state has
answered. What crosses out of here is §4's closed list -- ``S``, ``C1``,
``C2``, ``stateOrdinal``, the record count and the commitment time --
never a record.

The one module here that is shared rather than private is
:mod:`~flux_engine.concordance.boundary.statistic`: both sides have to
form their aggregates identically or the integer comparison of §5 is
comparing two different grids. What stays private is
:mod:`~flux_engine.concordance.boundary.store` -- the check counter -- and
:mod:`~flux_engine.concordance.boundary.vault`, which keeps the per-state
blinding value under the owner's state directory so that every check of
one record set commits to the same ``C2``.
"""

from __future__ import annotations

from flux_engine.concordance.boundary.merkle import (
    ColumnCommitment,
    LeafFunction,
    LeafStatus,
    Sha256Leaf,
    commit_column,
    merkle_root,
)
from flux_engine.concordance.boundary.slice import (
    CommittedState,
    SliceReference,
    commit_state,
    commitment_c2,
    generate_blinding,
    read_slice,
    refuse_weak_blinding,
    slice_content_digest,
)
from flux_engine.concordance.boundary.statistic import (
    DerivedStatistic,
    GroupStatistic,
    PropertyStatistic,
    Row,
    compute_statistic,
)
from flux_engine.concordance.boundary.store import BoundaryStore, StateRecord
from flux_engine.concordance.boundary.vault import BlindingVault, VaultedState

__all__ = [
    "BlindingVault",
    "BoundaryStore",
    "ColumnCommitment",
    "CommittedState",
    "DerivedStatistic",
    "GroupStatistic",
    "LeafFunction",
    "LeafStatus",
    "PropertyStatistic",
    "Row",
    "Sha256Leaf",
    "SliceReference",
    "StateRecord",
    "VaultedState",
    "commit_column",
    "commit_state",
    "commitment_c2",
    "compute_statistic",
    "generate_blinding",
    "merkle_root",
    "read_slice",
    "refuse_weak_blinding",
    "slice_content_digest",
]
