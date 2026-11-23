# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The kernels this engine implements, by ``kernelVersion`` (ADR 0010, step 2).

A plan names its kernel, and ``plan.json`` records it as ``kernelVersion``. Every
stage that runs or stamps a recorded plan takes the kernel from that plan:
``apply`` and ``replay`` run it, ``verify`` recomputes the ``inputDigest`` and
records a golden baseline under it, and ``ship`` writes it into the evidence
pack. :data:`DEFAULT_KERNEL` is only the kernel a new plan takes, so moving it
moves no recorded plan's digests. Go's toolchain does the same with the ``go``
line of a ``go.mod``: a module keeps the behaviour of the version it names under
a newer toolchain. A plan that names a kernel this engine does not implement is
refused (``FLX-2119``), as Terraform refuses a plan file written by another
version, rather than run under a kernel it was not made with.

The registry is data. A module that draws or stamps reads the kernel its run
carries (:attr:`flux_engine.stages.pipeline.PipelineResult.kernel`), never
:data:`DEFAULT_KERNEL`, and ``tests/stages/test_kernel_from_plan.py`` holds the
stages to that.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from flux_engine.core import kernel2
from flux_engine.core.seedtree import SEED_TREE_VERSION
from flux_engine.core.trace import TRACE_VERSION

__all__ = [
    "DEFAULT_KERNEL",
    "KERNELS",
    "KERNEL_V1",
    "KERNEL_V2",
    "Kernel",
    "kernel_named",
    "new_plan_is_counter_based",
]


@dataclass(frozen=True)
class Kernel:
    """One kernel: the three version strings KERNEL-SPEC section 1 lists for it.

    All three are inside ``inputDigest`` (KERNEL-SPEC section 11), so a plan is
    bound to the kernel that made it. ``version`` is the plan's
    ``kernelVersion``. ``trace_version`` is the trace format the kernel's runs
    are written in: :mod:`flux_engine.core.trace` writes one format, so every
    kernel here names it until that module writes another.
    """

    version: str
    seed_tree_version: str
    trace_version: str


#: Kernel ``0.1.0``, specified by docs/spec/KERNEL-SPEC.md.
KERNEL_V1 = Kernel(
    version="0.1.0", seed_tree_version=SEED_TREE_VERSION, trace_version=TRACE_VERSION
)

#: Kernel ``0.2.0``, specified by docs/spec/KERNEL-SPEC-2.md (ADR 0010): counter-based
#: streams and binary64 transforms. Its trace format is kernel ``0.1.0``'s.
KERNEL_V2 = Kernel(
    version="0.2.0", seed_tree_version=kernel2.SEED_TREE_VERSION, trace_version=TRACE_VERSION
)

#: Every kernel this engine implements, by ``kernelVersion``. Read-only: a
#: kernel is added here, in the source, with its specification and vectors.
KERNELS: Mapping[str, Kernel] = MappingProxyType(
    {KERNEL_V1.version: KERNEL_V1, KERNEL_V2.version: KERNEL_V2}
)

#: The kernel a new plan takes when it names none (``fluxsim plan --kernel``). A
#: recorded plan never reads it. Kernel 0.2.0 is the default from 0.9.0, the minor
#: release of its own that ADR 0010's Migration asks for; a plan that names 0.1.0, and
#: every recorded plan and evidence pack that does, runs on it to the digests it had.
DEFAULT_KERNEL: Kernel = KERNEL_V2


def kernel_named(version: object) -> Optional[Kernel]:
    """The kernel *version* names, or ``None`` when this engine implements none by that name."""
    return KERNELS.get(version) if isinstance(version, str) else None


def new_plan_is_counter_based() -> bool:
    """Whether a new plan runs kernel 0.2.0's counter-based generator.

    ``validate`` and ``fluxsim init``'s pre-flight read a bundle's rules before any plan exists,
    so they read them as the plan the bundle would get does, without naming the default
    themselves (a recorded plan never reads it).
    """
    return DEFAULT_KERNEL.seed_tree_version == kernel2.SEED_TREE_VERSION
