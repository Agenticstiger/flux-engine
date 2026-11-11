# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``require_supported_fidelity``: one exception type for every refusal.

H5 from the API review: the module docstring claims agentic and
stochastic "both raise the same UnsupportedFidelity shape" -- verified
here rather than merely asserted, and extended to the review's other two
findings: an unrecognised ``fidelity`` value is refused rather than
silently treated as executable, and the stochastic refusal carries its
own code (``FLX-4103``) distinct from the agentic one (``FLX-4101``).
"""

from __future__ import annotations

import pytest

from flux_engine.governance.skills import UnsupportedFidelity
from flux_engine.stages.errors import FLX_4103, FLX_4104, SweepExecutorUnavailable
from flux_engine.stages.pipeline import require_supported_fidelity


def test_deterministic_is_always_supported() -> None:
    require_supported_fidelity("deterministic")  # must not raise


def test_agentic_without_the_extension_raises_unsupported_fidelity() -> None:
    with pytest.raises(UnsupportedFidelity) as excinfo:
        require_supported_fidelity("agentic")
    assert excinfo.value.code.code == "FLX-4101"


def test_stochastic_raises_the_same_exception_type_as_agentic() -> None:
    """The docstring's central claim: one ``except UnsupportedFidelity``
    catches both refusals, even though they carry different codes."""
    with pytest.raises(UnsupportedFidelity) as excinfo:
        require_supported_fidelity("stochastic")
    assert isinstance(excinfo.value, SweepExecutorUnavailable)
    assert excinfo.value.code.code == FLX_4103.code
    assert excinfo.value.code.code != "FLX-4101"


def test_an_unrecognised_fidelity_value_is_refused_not_silently_accepted() -> None:
    """Before this fix, ``require_supported_fidelity("turbo")`` returned
    silently -- the schema was the only guard, and `fluxsim validate` would
    have reported ``fidelity: "turbo"`` as executable."""
    from flux_engine.core.errors import FluxError

    with pytest.raises(FluxError) as excinfo:
        require_supported_fidelity("turbo")
    assert excinfo.value.code.code == FLX_4104.code
