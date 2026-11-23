# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The six-step loop's seven stages: validate, plan, apply, run, verify, ship, replay.

Each stage is a pure function of its arguments returning a
:class:`flux_engine.api.stages.StageResult` -- nothing here is a CLI
command in itself; :mod:`flux_engine.cli` and :mod:`flux_engine.engine`
are the two callers, one synchronous and one async, that wrap these for a
terminal and for an in-process consumer respectively. Neither wrapper
re-derives any stage's behaviour: they only decide how its result is
presented.

:mod:`flux_engine.stages.pipeline` is the shared execution core every
stage other than ``ship`` and ``replay`` calls at least once; it is not
itself one of the seven CLI-facing stages.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any, List

if TYPE_CHECKING:
    from flux_engine.stages.apply import apply
    from flux_engine.stages.plan import plan
    from flux_engine.stages.replay import replay
    from flux_engine.stages.run import run
    from flux_engine.stages.ship import ship
    from flux_engine.stages.validate import validate
    from flux_engine.stages.verify import verify

__all__ = ["apply", "plan", "replay", "run", "ship", "validate", "verify"]

# Each public name is the stage function, and each is also the name of the module that
# defines it. Importing the package used to import all seven modules (the whole engine, some
# 400 modules, for a worker process that needs none of them); the names are resolved on first
# use now (PEP 562), so importing a submodule such as ``flux_engine.stages._parallel`` loads
# that module and nothing else.


class _LazyStages(ModuleType):
    """The package, refusing the submodule where a stage function is the public name.

    Importing ``flux_engine.stages.verify`` binds the module to the package's ``verify``
    attribute once it loads, which would shadow the function ``from flux_engine.stages import
    verify`` has always returned. That binding is dropped for the seven stage names, so the
    attribute is the function whenever it is asked for, however the module came to be loaded.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in __all__ and isinstance(value, ModuleType):
            return
        super().__setattr__(name, value)


def __getattr__(name: str) -> Any:
    if name in __all__:
        function = getattr(importlib.import_module(f"{__name__}.{name}"), name)
        globals()[name] = function
        return function
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> List[str]:
    return sorted({*globals(), *__all__})


sys.modules[__name__].__class__ = _LazyStages
