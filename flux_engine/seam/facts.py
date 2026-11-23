# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The facts a Simulation declares about its run (ADR 0018).

A fact is the share of one expose's rows that satisfy one of its rules, and the profile can
state the share it measured on the data (PROFILE-SPEC.md, the ``invariants`` section of format
1-2-0). A Simulation declares facts under the top-level ``facts`` key of its namespace, in the
FLUX bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        facts:
          - { emit: orders, rule: paid_after_placed, tolerance: "0.02" }

``tolerance`` is optional: the largest gap between the share the run holds and the share the
profile measured, an exact decimal in ``[0, 1]``. A rule with no entry is still a fact, named
``<exposeId>.<ruleId>``, and still checked on the output (:mod:`flux_engine.concordance.rule_shares`);
a declaration only adds a tolerance to it.

**A fact with a tolerance is part of the run's fidelity predicate**, so it is bound into ``H``
before any row is drawn, not only into ``inputDigest``: :func:`bound_into_h` is the member the
emit's entry for ``H`` gains, present only when the emit has such a fact, so a bundle that
declares none hashes exactly as before. ``G`` leaves the member out
(:func:`flux_engine.seam.emit.seam_reusable_digest`): a tolerance edit moves ``H`` and not ``G``,
as every other tolerance does (SEAM-FIDELITY-SPEC section 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.seam.scorecard import _operand

__all__ = [
    "FACTS_KEY",
    "Fact",
    "bound_into_h",
    "contract_rule_ids",
    "fact_name",
    "read_facts_block",
    "rule_ids_of",
]

#: The top-level key of the namespace that declares facts, and the member an emit's ``H`` gains.
FACTS_KEY = "facts"

_ENTRY_KEYS = ("emit", "rule", "tolerance")
_MOST_FACTS = 4096


def fact_name(expose_id: str, rule_id: str) -> str:
    """The name a fact goes by in a request, a profile and a scorecard: ``<exposeId>.<ruleId>``."""
    return f"{nfc(expose_id)}.{nfc(rule_id)}"


def contract_rule_ids(expose: Mapping[str, Any]) -> Tuple[str, ...]:
    """The ids of the rules one ``exposes[]`` entry of a contract declares, in declared order."""
    contract = expose.get("contract")
    raw = contract.get("invariants") if isinstance(contract, Mapping) else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(
        nfc(entry["id"])
        for entry in raw
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str) and entry["id"]
    )


def rule_ids_of(
    expose: Mapping[str, Any], extension: Optional[Mapping[str, Any]]
) -> Tuple[str, ...]:
    """The rules one emit holds: its contract's, then the bundle's own (the extension's ``rules``)."""
    raw = extension.get("rules") if extension is not None else None
    own = (
        tuple(
            nfc(entry["id"])
            for entry in raw
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str) and entry["id"]
        )
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
        else ()
    )
    return contract_rule_ids(expose) + own


@dataclass(frozen=True)
class Fact:
    """One declared fact: an emit, one of its rules and, optionally, its tolerance.

    ``declared`` is the tolerance as written (a decimal string), ``tolerance`` its exact value.
    """

    emit: str
    rule: str
    tolerance: Optional[Fraction] = None
    declared: Optional[str] = None

    @property
    def name(self) -> str:
        """``<exposeId>.<ruleId>``."""
        return fact_name(self.emit, self.rule)


def read_facts_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[Fact, ...]:
    """Read a ``facts`` block, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused: a block that is not a list, more
    than 4 096 entries, an entry that is not a mapping or carries a key other than ``emit``,
    ``rule`` and ``tolerance``, an emit the Simulation does not emit, a name that is not a
    non-empty string, a fact declared twice, and a tolerance that is not an exact decimal between
    0 and 1 (never a float or a bool). Whether the emit's contract has the rule is checked where
    the contract is known (:func:`flux_engine.seam.resolve.resolve_emit`).
    """
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{emit, rule, tolerance}} entries")
    if len(block) > _MOST_FACTS:
        raise FluxError(code, f"{where} holds more than {_MOST_FACTS} facts")
    found: Dict[Tuple[str, str], Fact] = {}
    for index, entry in enumerate(block):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            raise FluxError(code, f"{at} is a mapping with emit, rule and an optional tolerance")
        unknown = sorted(str(key) for key in entry if str(key) not in _ENTRY_KEYS)
        if unknown:
            raise FluxError(code, f"{at} declares {unknown}; a fact carries {list(_ENTRY_KEYS)}")
        names = []
        for key in ("emit", "rule"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                raise FluxError(code, f"{at}.{key} is a non-empty string")
            names.append(nfc(value))
        emit, rule = names
        if emit not in emits:
            raise FluxError(
                code,
                f"{at}.emit names {emit!r}, which this Simulation does not emit "
                f"(it emits {sorted(emits)})",
            )
        if (emit, rule) in found:
            raise FluxError(code, f"{at} declares the fact {fact_name(emit, rule)!r} twice")
        tolerance: Optional[Fraction] = None
        declared: Optional[str] = None
        if "tolerance" in entry:
            tolerance, declared = _operand(entry["tolerance"], f"{at}.tolerance", code)
        found[(emit, rule)] = Fact(emit=emit, rule=rule, tolerance=tolerance, declared=declared)
    return tuple(found[key] for key in sorted(found))


def bound_into_h(facts: Sequence[Fact], emit: str) -> Optional[Dict[str, Any]]:
    """The member of *emit*'s entry for ``H`` that its facts with a tolerance make, or ``None``.

    ``{"<ruleId>": {"tolerance": "<decimal as written>"}}`` for each fact of *emit* that carries
    a tolerance, in rule order; ``None`` when it has none, so the entry gains no key.
    """
    wanted = nfc(emit)
    member = {
        fact.rule: {"tolerance": fact.declared}
        for fact in sorted(facts, key=lambda item: item.rule)
        if fact.emit == wanted and fact.tolerance is not None
    }
    return member or None
