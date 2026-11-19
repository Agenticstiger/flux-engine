# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A test for a count, an ordinal or a size: an ``int``, never a ``bool``, not below a floor.

``bool`` is a subclass of ``int``, so ``True`` passes a bare
``isinstance(value, int)`` as 1. The test lives here, and not in the engine,
because both packages read such numbers and the engine imports this package,
never the other way round (ADR 0009). A caller keeps its own refusal: the
message and the FLX code are the caller's, and the test is this one.
"""

from __future__ import annotations

from typing import TypeGuard

__all__ = ["is_int_at_least"]


def is_int_at_least(value: object, minimum: int) -> TypeGuard[int]:
    """Whether *value* is an ``int``, not a ``bool``, and not below *minimum*.

    A subclass of ``int`` other than ``bool``, such as an ``IntEnum`` member,
    is an ``int`` here, as it is to :func:`isinstance`.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum
