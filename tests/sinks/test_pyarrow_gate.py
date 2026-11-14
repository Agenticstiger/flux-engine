# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The sinks tests skip where pyarrow cannot import, and only there.

``pyarrow_or_skip`` (``conftest.py``) is what keeps the nightly's NumPy 1.26.0
cells collecting the whole tree. These tests give it a pyarrow that raises the
way pyarrow 26 does on NumPy 1.x, or none at all, so they need neither pyarrow
nor an old NumPy.

Each test names what the helper did, rather than expecting an exception: a
skip raised where an ``ImportError`` was expected would end the test as
skipped, and a regression of exactly that kind would read as a pass.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Tuple

import numpy
import pytest

from .conftest import pyarrow_or_skip

#: What ``import pyarrow`` raises on NumPy 1.x, from pyarrow 26's ``lib.pyx``.
TOO_OLD = 'raise ImportError("pyarrow requires NumPy 2.0 or newer, found 1.26.0")\n'


def done() -> Tuple[str, str]:
    """What ``pyarrow_or_skip`` did: ``skip``, ``fail`` or ``imports``, with its message."""
    try:
        pyarrow_or_skip()
    except pytest.skip.Exception as skipped:
        return "skip", str(skipped)
    except ImportError as error:
        return "fail", str(error)
    return "imports", ""


@pytest.mark.parametrize("numpy_version", ["1.26.0", "2.1.0"])
def test_no_pyarrow_is_a_skip_on_either_numpy(
    monkeypatch: pytest.MonkeyPatch, numpy_version: str
) -> None:
    monkeypatch.setattr(numpy, "__version__", numpy_version)
    monkeypatch.setitem(sys.modules, "pyarrow", None)  # import raises ModuleNotFoundError
    kind, message = done()
    assert kind == "skip"
    assert "could not import 'pyarrow'" in message


@pytest.mark.parametrize(
    ("numpy_version", "kind"),
    [
        pytest.param("1.26.0", "skip", id="numpy-1-skips"),
        pytest.param("2.1.0", "fail", id="numpy-2-fails"),
    ],
)
def test_a_pyarrow_that_will_not_import_skips_only_below_numpy_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, numpy_version: str, kind: str
) -> None:
    monkeypatch.setattr(numpy, "__version__", numpy_version)
    (tmp_path / "pyarrow").mkdir()
    (tmp_path / "pyarrow" / "__init__.py").write_text(TOO_OLD, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "pyarrow", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    got, message = done()
    assert got == kind
    assert "requires NumPy 2.0 or newer, found 1.26.0" in message  # the skip names why
