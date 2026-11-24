# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink's audience gate (ADR 0038 section 2).

A Kafka target says who reads it, ``audience=internal`` or ``audience=external``, and the run's
privacy grade (ADR 0020) must be the one those readers need: ``P1`` or better for readers who
work for the data's owner, ``P3`` or better for readers who do not. Grades compare in ADR 0020's
order, ``P5 < P1 < P3 < P4 < P0``.

Nothing here contacts anything and nothing names a value of the data: a refusal names the
stream, the grade, the audience and its minimum.

Standard library and :mod:`flux_engine` only.
"""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern, at_least
from flux_engine.sinks.errors import FLX_3504, FLX_3512
from flux_engine.sinks.receipts import RecordedGrade

__all__ = ["AUDIENCES", "MINIMUM_GRADE", "parse_audience", "require_grade"]

#: Who may read a Kafka target, as ``--sink-opt audience=...`` spells it.
AUDIENCES: Tuple[str, ...] = ("internal", "external")

#: The weakest grade each audience accepts.
MINIMUM_GRADE: Mapping[str, Pattern] = {"internal": Pattern.P1, "external": Pattern.P3}


def parse_audience(value: Optional[str]) -> str:
    """The audience *value* names; ``FLX-3504`` when it is absent or not one of :data:`AUDIENCES`."""
    if value is None or value == "":
        raise FluxError(
            FLX_3504,
            "the kafka sink needs --sink-opt audience=internal or audience=external: it has no "
            "default, because the run's privacy grade is held against who reads the topics",
        )
    if value not in AUDIENCES:
        raise FluxError(
            FLX_3504,
            f"the option audience takes one of {', '.join(AUDIENCES)}, not {value!r}",
        )
    return value


def require_grade(audience: str, recorded: Optional[RecordedGrade], *, expose_id: str) -> Pattern:
    """The run's recorded grade as a :class:`~flux_engine.core.grades.Pattern`, when *audience* may read it.

    *recorded* is what the engine read from the run's own plan
    (:class:`~flux_engine.sinks.receipts.RecordedGrade`). ``FLX-3512`` for every audience when the
    sink was handed none, when the record states no grade (the message names what is missing), when
    the text is not a grade, and when the grade is below the audience's minimum.
    """
    if audience not in MINIMUM_GRADE:
        raise FluxError(
            FLX_3504,
            f"the option audience takes one of {', '.join(AUDIENCES)}, not {audience!r}",
        )
    if recorded is None:
        raise FluxError(
            FLX_3512,
            f"the engine did not hand the sink the grade the run's plan records for expose "
            f"{expose_id!r}, so it cannot tell whether an {audience} audience may read it; "
            "nothing was sent",
        )
    if recorded.grade is None:
        problem = recorded.problem or "the run's plan states no grade"
        raise FluxError(
            FLX_3512,
            f"expose {expose_id!r} has no privacy grade an {audience} audience can be held "
            f"against: {problem}; nothing was sent",
        )
    try:
        pattern = Pattern(recorded.grade)
    except ValueError:
        raise FluxError(
            FLX_3512,
            f"the privacy grade the sink was given for expose {expose_id!r} is not one of "
            f"{', '.join(item.value for item in Pattern)}; nothing was sent",
        ) from None
    minimum = MINIMUM_GRADE[audience]
    if not at_least(pattern, minimum):
        remedy = (
            "pin a profile released under differential privacy, or deliver to an internal "
            "target if the run's grade reaches it"
            if audience == "external"
            else "pin a profile so the run is graded P1 or better"
        )
        raise FluxError(
            FLX_3512,
            f"expose {expose_id!r} has privacy grade {pattern.value} ({pattern.label}), and "
            f"an {audience} audience needs {minimum.value} ({minimum.label}) or stronger; "
            f"{remedy}; nothing was sent",
        )
    return pattern
