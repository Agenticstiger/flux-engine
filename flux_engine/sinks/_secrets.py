# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What counts as a credential in a sink's options, its target and its errors.

Three questions, one answer each, shared by every sink:

* :func:`credential_key` -- does an option's name name a credential
  (``password``, ``access-key``, ``sas``)?
* :func:`credential_url` -- is a target a URL that carries user information,
  a query or a fragment? A best effort: a refusal that points a person at ``uri-env``.
  What is printed about a target never depends on it: that is
  :func:`~flux_engine.core.target_label.target_label`.
* :func:`scrub` -- a message with every target and option value the caller knows written as its
  label, every URL token in it written as its label, and every value of an environment variable
  whose name names a credential replaced by ``***``.

Standard library only.
"""

from __future__ import annotations

import os
import re
from typing import List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.credentials import (
    MASK,
    credential_key,
    credential_option,
    credential_query,
    credential_url,
    names_environment_variable,
)
from flux_engine.core.target_label import replace_target, spellings, target_label
from flux_engine.sinks._options import PLAIN_OPTIONS

__all__ = [
    "Known",
    "credential_key",
    "credential_option",
    "credential_query",
    "credential_url",
    "known_strings",
    "names_environment_variable",
    "scrub",
]

#: The shortest environment value that is replaced: a one- or two-character
#: value would mangle every message it appears in.
_MIN_SECRET = 4

#: Variables whose names read as ``pwd`` but hold a directory, which is no credential.
_NOT_CREDENTIALS = frozenset({"PWD", "OLDPWD"})


#: A run of text that may be a URL: no white space, quote, angle bracket or backtick. Linear time.
_TOKEN = re.compile(r"[^\s\"'<>`]+")
#: What a scheme is made of, read back from its ``://`` (``jdbc:mysql`` is one).
_SCHEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.:-")
#: Punctuation that ends a sentence or a bracket around a URL, not the URL.
_TRAILING = ").,;:!}"


def _scheme_start(token: str, mark: int) -> int:
    """Where the scheme of the ``://`` at *mark* starts: after any ``name:`` before it, but for ``jdbc:``."""
    start = mark
    while start > 0 and token[start - 1] in _SCHEME_CHARS:
        start -= 1
    while start < mark and not token[start].isalpha():
        start += 1
    head = token[start:mark].split(":")
    keep = 2 if len(head) > 1 and head[-2].casefold() == "jdbc" else 1
    return mark - len(":".join(head[-keep:]))


def _label_urls(text: str, sink: str) -> str:
    """*text* with every URL token written as its label: scheme, host, port and path only."""

    def label(match: "re.Match[str]") -> str:
        token = match.group(0)
        mark = token.find("://")
        if mark < 0:
            return token
        start = _scheme_start(token, mark)
        end = len(token)
        while end > mark + 3 and token[end - 1] in _TRAILING:
            end -= 1
        return token[:start] + target_label(sink or "url", token[start:end]) + token[end:]

    return _TOKEN.sub(label, text)


#: A string a library may echo, or that string and what to write for it.
Known = Union[str, Tuple[str, str]]


def known_strings(target: str, options: Optional[Mapping[str, str]] = None) -> List[Known]:
    """Every string a person gave for a sink that a library may echo: the target, each option's value, and what each ``-env`` option's variable holds.

    ``uri-env=SIM_URI`` and ``db.password-env=SIM_PW`` name variables; the values of those
    variables are what a driver is given, and what it may print. Each is written as ``$SIM_URI``,
    the variable's name, never its value. The options that never hold a secret
    (:data:`~flux_engine.sinks._options.PLAIN_OPTIONS`: a mode, a size, a switch) are not among them,
    so a message that says ``mode`` is ``append`` is left as it is; a value of one of them that a
    sink cannot take is never echoed (a validation error names the option only).
    """
    strings: List[Known] = [target]
    for key, value in (options or {}).items():
        if names_environment_variable(key):
            resolved = os.environ.get(value)
            if resolved:
                strings.append((resolved, f"${value}"))
        elif key not in PLAIN_OPTIONS and value:
            # An option's value is never a path: it is written as the label of any other string.
            strings.append((value, target_label("option", value)))
    return [item for item in strings if item]


def scrub(text: str, known: Sequence[Known] = (), *, sink: str = "") -> str:
    """*text* with every credential this process can name removed.

    Each string of *known* (the raw target, the raw values of the options, what an ``-env`` option
    resolved to: :func:`known_strings`) that *text* holds is replaced by its label, as are the parts
    of it the label withholds (:func:`~flux_engine.core.target_label.replace_target`). Every URL
    token left in the text (a run holding ``://`` and no white space, quote, angle bracket or
    backtick) is written as its label too, so a URL a library built is shown without its user
    information, query or fragment. Every value (four characters or more) of an environment
    variable whose name :func:`credential_key` flags is replaced by ``***``, in each of its
    :func:`~flux_engine.core.target_label.spellings` (percent-encoded, quoted). Nothing else in free
    text is searched for a secret: a caller that knows no string and wants none of the text
    prints a fixed message instead.
    """
    text = _label_urls(replace_target(text, sink, known), sink)
    forms = {
        form
        for name, value in os.environ.items()
        if len(value) >= _MIN_SECRET and name not in _NOT_CREDENTIALS and credential_key(name)
        for form in spellings(value)
        if len(form) >= _MIN_SECRET
    }
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, MASK)
    return text
