# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The plans of a contract's emits with a history bound to one of them, for the history tests."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from flux_engine.generator.history import HistoryBinding
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import GENERATOR_VERSION_2, GeneratorPlan, build_plan
from flux_engine.generator.rules import plan_rules
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.counts import CountsEntry
from flux_engine.seam.keys import compile_keys, mark_key_columns

from .test_history_layout import BOUNDS, SIM_ID
from .test_relations import _h


def plans_of(
    document: Dict[str, Any],
    declared: Mapping[str, Tuple[Dict[str, Any], int]],
    bindings: Mapping[str, HistoryBinding],
    *,
    emit: str,
    counter_based: bool = True,
    counts: Optional[Mapping[str, Mapping[str, CountsEntry]]] = None,
    rules_for: Optional[Callable[[str], Mapping[str, Any]]] = None,
) -> Dict[str, GeneratorPlan]:
    """The related plans of ``document``'s exposes; ``declared`` has each one's columns and rows.

    ``emit`` is the versioned expose: its ``sub_id`` is a related column too. ``rules_for`` names
    the arguments of one expose's ``plan_rules`` beyond the usual ones.
    """
    keys = compile_keys(document)
    assert keys is not None
    plans: Dict[str, GeneratorPlan] = {}
    for expose in document["exposes"]:
        expose_id = expose["exposeId"]
        expose_keys = keys.expose(expose_id)
        compiled = mark_key_columns(compile_expose(expose), expose_keys)
        columns, count = declared[expose_id]
        related = related_columns(expose_keys) | ({"sub_id"} if expose_id == emit else set())
        plans[expose_id] = build_plan(
            compiled,
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": columns},
            time_bounds=BOUNDS,
            related=related,
            rules=plan_rules(
                **{
                    "contract_document": document,
                    "expose_id": expose_id,
                    "settings": None,
                    "related": related,
                    "mapping": None,
                    "has_fidelity": True,
                    "strict": False,
                    "flux_rules": None,
                    "keys": expose_keys,
                    "rows": count,
                    "null_fractions": {},
                    "counter_based": counter_based,
                    "links": (),
                    "copies": False,
                    **(rules_for(expose_id) if rules_for else {}),
                }
            ),
            generator_version=GENERATOR_VERSION_2,
        )
    return relate_plans(plans, keys, None, None, counts, document, None, dict(bindings))


def by_entity(data: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    """A versioned emit's rows grouped by ``sub_id``, in the order they were written."""
    found: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for row in data:
        found[row["sub_id"]].append(row)
    return found
