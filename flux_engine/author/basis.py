# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What one number of an authored bundle rests on, and where it sits.

Every number ``fluxsim init --profile`` writes carries a :class:`Basis`: a
statistic of the profile (a JSON Pointer into it and the definition it lands
in), a place in the FLUID contract, a stated assumption with its reason, or a
template default with its reason. :class:`Label` ties a basis to the position
it speaks for, and the bundle writer turns each label into one entry of the
Simulation's parameter register (docs/spec/PARAMETER-REGISTER-SPEC.md).

The shape follows Deequ's constraint suggestions (Apache-2.0), where every
suggested check carries the rule that produced it and the value it read, so a
reviewer can see why each line is there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from flux_engine.seam.parameters import Source, render_pointer

__all__ = ["Basis", "Label", "assumed", "default", "from_contract", "from_profile"]


@dataclass(frozen=True)
class Basis:
    """The source of a number, with its citation or its reason."""

    source: Source
    #: A profile citation: pointer tokens into the profile and the definition, if any.
    statistic: Optional[Tuple[Tuple[str, ...], Optional[str]]] = None
    #: A contract citation: the contract's id and pointer tokens into it.
    contract: Optional[Tuple[str, Tuple[str, ...]]] = None
    reason: Optional[str] = None

    def entry_fields(self) -> Dict[str, Any]:
        """The source and its citation or reason, as a register entry spells them."""
        fields: Dict[str, Any] = {"source": self.source.value}
        if self.statistic is not None:
            tokens, definition = self.statistic
            citation: Dict[str, Any] = {"pointer": render_pointer(tokens)}
            if definition is not None:
                citation["definition"] = definition
            fields["statistic"] = citation
        if self.contract is not None:
            product_ref, tokens = self.contract
            fields["contract"] = {"productRef": product_ref, "pointer": render_pointer(tokens)}
        if self.reason is not None:
            fields["reason"] = self.reason
        return fields


@dataclass(frozen=True)
class Label:
    """A basis and the position it speaks for: tokens below the emit's own block."""

    tokens: Tuple[str, ...]
    basis: Basis


def from_profile(tokens: Tuple[str, ...], definition: Optional[str]) -> Basis:
    """A number resting on the profile: *tokens* point into it, at *definition*."""
    return Basis(Source.PROFILE, statistic=(tokens, definition))


def from_contract(product_ref: str, tokens: Tuple[str, ...]) -> Basis:
    """A number the contract fixes: *tokens* point into the contract named *product_ref*."""
    return Basis(Source.CONTRACT, contract=(product_ref, tokens))


def assumed(reason: str) -> Basis:
    """A number the profile does not give, assumed for the stated *reason*."""
    return Basis(Source.ASSUMPTION, reason=reason)


def default(reason: str) -> Basis:
    """A template value nothing measured or assumed: a default, with what it is for."""
    return Basis(Source.DEFAULT, reason=reason)
