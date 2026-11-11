# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The governance suite is a package.

Without this file pytest inserted ``tests/governance`` itself on
``sys.path`` at collection time, and this directory's ``conftest.py``
became importable as the bare name ``conftest`` -- the same bare name
``tests/conftest.py`` and six other test directories answer to. Which one
a module got depended on collection order, so ``from conftest import
DEFECTS`` in this suite resolved to the right file only by accident, and
shadowed the suite-wide ``conftest`` for every module collected after it.

With the package, ``tests`` is the directory on ``sys.path``, this suite's
modules are ``governance.*``, and its shared fixtures are imported as
``governance.conftest`` -- a name that can only mean one file.
"""
