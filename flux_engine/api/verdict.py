# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The verify readout: two questions, two answers, never one number.

``fluxsim verify`` answers two questions that are constantly confused with
each other:

* **reproducibility** -- did the same bundle and the same seed produce the
  same bytes?
* **validity** -- does the scenario describe the world?

The first is a property of this engine and is fully in its gift. The
second is a property of the world and is not. A readout that merges them
into a single pass/fail lets the answer the engine *can* give stand in for
the answer it *cannot*, and the reader takes away the stronger of the two.

So :class:`VerifyVerdict` carries them as two fields that never combine.
There is deliberately no ``__bool__``, no ``ok``, no ``passed``, no
``overall`` -- nothing that would let ``if verdict:`` compile. A caller
that wants a decision has to name which of the two it is deciding on. The
snapshot test in ``tests/api/test_api_surface.py`` pins that absence, so
adding a convenience accessor later is a test failure, not a quiet
regression.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flux_engine.api.claims import Claim

__all__ = ["Reproducibility", "Validity", "VerifyVerdict"]


class Reproducibility(str, Enum):
    """Whether re-running the bundle reached the same bytes."""

    REPRODUCIBLE = "reproducible"
    NOT_REPRODUCIBLE = "not-reproducible"
    #: The re-run was not performed. Distinct from a negative answer.
    NOT_CHECKED = "not-checked"


class Validity(str, Enum):
    """Whether a predicted outcome was measured against the world."""

    #: The default. Nothing was measured, and the readout says so.
    NOT_ASSERTED = "not-asserted"
    SUPPORTED = "supported"
    NOT_SUPPORTED = "not-supported"


#: Rendering used by :meth:`VerifyVerdict.headline`. Fixed strings, not
#: derived from the enum values, so the sentence a reader sees is reviewed
#: prose rather than an accident of an identifier.
_REPRODUCIBILITY_WORDS = {
    Reproducibility.REPRODUCIBLE: "yes",
    Reproducibility.NOT_REPRODUCIBLE: "no",
    Reproducibility.NOT_CHECKED: "not checked",
}

_VALIDITY_WORDS = {
    Validity.NOT_ASSERTED: "not asserted",
    Validity.SUPPORTED: "supported",
    Validity.NOT_SUPPORTED: "not supported",
}


@dataclass(frozen=True)
class VerifyVerdict:
    """The result of ``fluxsim verify``: two disjoint answers and their receipts.

    Attributes
    ----------
    reproducibility
        Answers the engine's own question. Defaults to
        :attr:`Reproducibility.NOT_CHECKED`.
    validity
        Answers the world's question. Defaults to
        :attr:`Validity.NOT_ASSERTED`, which is the honest answer for every
        run that measured nothing against the world -- that is, for every
        run of the v0.1 engine.
    claims
        The individual claims behind the two fields, each with its own
        basis and evidence.
    """

    reproducibility: Reproducibility = Reproducibility.NOT_CHECKED
    validity: Validity = Validity.NOT_ASSERTED
    claims: tuple["Claim", ...] = field(default_factory=tuple)

    def headline(self) -> str:
        """Render both answers in one line, with neither able to hide the other.

        Example: ``"reproducible: yes | validity: not asserted"``. The
        second half is always present, including when it is the weakest
        thing the engine can say, because the reader's default assumption
        in its absence is the strongest thing.
        """
        return (
            f"reproducible: {_REPRODUCIBILITY_WORDS[self.reproducibility]}"
            f" | validity: {_VALIDITY_WORDS[self.validity]}"
        )
