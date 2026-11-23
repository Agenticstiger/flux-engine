# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Reading a sink's ``KEY=VALUE`` options, as the command line spells them.

A sink's Python API takes typed arguments; ``fluxsim run --sink-opt`` and a
third-party factory receive strings. These helpers turn one into the other
and refuse, naming the option, anything they cannot read -- before the
simulation runs, so a typo costs a second rather than a whole run.

Standard library only.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from flux_engine.core.errors import FluxError
from flux_engine.sinks.errors import FLX_3504

__all__ = [
    "PLAIN_OPTIONS",
    "bool_option",
    "int_option",
    "option_name",
    "require_known",
    "text_option",
]

#: The options of the built-in sinks that never hold a secret: a mode, a size, a switch, a name. The
#: command a failed delivery prints shows the value of these whole and every other option's as ``…``
#: (``uri`` is the one built-in option that may hold one). A test keeps this set and each sink's
#: ``OPTIONS`` in step.
PLAIN_OPTIONS = frozenset(
    {
        "batch-size",
        "compression",
        "compression-level",
        "connect-timeout",
        "instance-identity",
        "max-retries",
        "mode",
        "prefix",
        "request-timeout",
        "retry-timeout",
        "row-group-size",
        "rows-per-file",
        "schema",
        "verify",
    }
)

#: What an option's name looks like.
_OPTION_NAME = re.compile(r"[a-z][a-z0-9._-]{0,39}")


def option_name(key: str) -> str:
    """*key* when it is shaped like an option's name, else a phrase that names none: a secret typed in its place is not echoed."""
    return key if _OPTION_NAME.fullmatch(key) else "a name that is not an option's"


_TRUE = ("true", "yes", "on", "1")
#: ASCII digits, optionally grouped with underscores (``1_000_000``). Not
#: str.isdigit(), which also takes superscripts and other scripts' digits.
_INTEGER = re.compile(r"^[0-9]+(?:_[0-9]+)*$")
_FALSE = ("false", "no", "off", "0")


def require_known(sink: str, options: Mapping[str, str], known: Iterable[str]) -> None:
    """Refuse an option *sink* does not take, listing the ones it does."""
    allowed = sorted(set(known))
    unknown = sorted(key for key in options if key not in allowed)
    if unknown:
        raise FluxError(
            FLX_3504,
            f"the {sink} sink takes no option {', '.join(option_name(key) for key in unknown)}; "
            f"it takes {', '.join(allowed) if allowed else 'no options'}",
        )


def text_option(
    options: Mapping[str, str], key: str, default: Optional[str] = None
) -> Optional[str]:
    """The option's text, or *default* when it is absent."""
    return options.get(key, default)


def int_option(options: Mapping[str, str], key: str, default: Optional[int]) -> Optional[int]:
    """The option as a positive integer, or *default* when it is absent."""
    raw = options.get(key)
    if raw is None:
        return default
    text = raw.strip()
    if not _INTEGER.match(text) or int(text.replace("_", "")) < 1:
        raise FluxError(FLX_3504, f"{key} must be a positive integer")
    return int(text.replace("_", ""))


def bool_option(options: Mapping[str, str], key: str, default: bool) -> bool:
    """The option as true or false (``true/false``, ``yes/no``, ``on/off``, ``1/0``)."""
    raw = options.get(key)
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise FluxError(FLX_3504, f"{key} must be true or false")
