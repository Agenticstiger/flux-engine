# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Where the documentation lives, in one place.

Every URL the command line prints comes from here: the docs link at the foot of
``--help``, the page a failing command points at, the support link. That is what
keeps a printed URL stable. It is a function of a code or a command name and
nothing else, so a page can be moved by changing one constant, and a test
(``tests/docs/test_generated_docs.py``) holds that every URL this module builds
names a page that exists in ``docs/``.

The layout it relies on, relative to the root of these docs::

    reference/cli/README.md          every command          <base>reference/cli/
    reference/cli/<command>.md       one command            <base>reference/cli/<command>.html
    reference/errors/README.md       every FLX code         <base>reference/errors/
    reference/errors/FLX-nnnn.md     one code               <base>reference/errors/FLX-nnnn.html

The base is the address these docs are published at. They are written in this
repository and join the FLUX specification's site as its ``engine/`` folder. That
site is built with VuePress at ``base: '/flux/'``, which its config locks because
every published schema ``$id`` depends on it, so ``docs/engine/reference/errors/FLX-nnnn.md``
there is served as ``/flux/engine/reference/errors/FLX-nnnn.html``.

**These addresses are public.** Every released version prints them, and a printed
address cannot be recalled. Moving a page means leaving something behind at the
old address. ``test_the_published_addresses_do_not_move`` in
``tests/docs/test_generated_docs.py`` holds the literal strings, so a change to
the base or to the layout above fails a test instead of passing quietly because
the page and the address moved together.
"""

from __future__ import annotations

__all__ = ["DOCS_URL", "ISSUES_URL", "command_url", "error_url"]

#: The folder of the published site these docs live in. Ends with a slash.
DOCS_URL = "https://agenticstiger.github.io/flux/engine/"

#: Where a person reports a problem with the engine.
ISSUES_URL = "https://github.com/Agenticstiger/flux-engine/issues"


def error_url(code: str) -> str:
    """The page that explains *code* (``FLX-nnnn``)."""
    return f"{DOCS_URL}reference/errors/{code}.html"


def command_url(command: str = "") -> str:
    """The reference page of one top-level command, or the index when *command* is empty."""
    return f"{DOCS_URL}reference/cli/{command}.html" if command else f"{DOCS_URL}reference/cli/"
