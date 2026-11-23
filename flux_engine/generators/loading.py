# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which declared generator plugins load, and whether each states what its bundle declares.

ADR 0032 section 3, steps 3 to 7, in the order the engine takes them. Each refusal comes before the
code it guards runs:

1. :func:`require_allowed`: a plugin the bundle names and the person did not name with
   ``--allow-generator`` is refused (``FLX-3119``). Nothing was looked up.
2. :func:`load_declared` resolves every declared plugin to one installed entry point whose code
   lies in an installation directory and outside the bundle directory, the state directory and the
   current directory (``FLX-3114`` to ``FLX-3117``), before it imports any of them.
3. It then loads each entry point (``FLX-3118`` when the code that loaded is not the code that was
   checked) and calls ``factory(options)``.
4. :func:`check_statement` reads the plugin's ``statement(request)`` and refuses (``FLX-3120``)
   any part that differs from the declaration: the grade, the privacy account (its unit, its loss
   and its accounting), the model digest and the reproducibility.

The editable flag :func:`~flux_engine.generators.registry.resolve` found travels on the loaded
plugin, so the stage that records the run can say the plugin's code lies in a source tree.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from flux_engine.core.errors import FluxError
from flux_engine.generators import registry
from flux_engine.generators.errors import FLX_3119, FLX_3120, FLX_3124
from flux_engine.generators.registry import ResolvedGenerator
from flux_engine.generators.request import Request
from flux_engine.seam.coverage import printable
from flux_engine.seam.generators import GeneratorDeclaration

__all__ = [
    "LoadedGenerator",
    "check_statement",
    "declared_statement",
    "load_declared",
    "require_allowed",
]

#: The longest a stated value is quoted in a refusal.
_SHOWN = 60


@dataclass(frozen=True)
class LoadedGenerator:
    """A declared plugin that was allowed, resolved and loaded: the object ``factory`` returned.

    ``editable`` is what :func:`~flux_engine.generators.registry.resolve` found: the plugin's code
    lies in a source tree, so the run it generates is recorded as not reproducible.
    """

    declaration: GeneratorDeclaration
    resolved: ResolvedGenerator
    plugin: Any

    @property
    def editable(self) -> bool:
        return self.resolved.editable

    @property
    def name(self) -> str:
        """What the plugin is called in a message: its entry-point name and its distribution."""
        distribution = f"{self.resolved.distribution} {self.resolved.version}"
        return f"{self.declaration.plugin} ({printable(distribution)})"


def require_allowed(declarations: Sequence[GeneratorDeclaration], allowed: Sequence[str]) -> None:
    """Refuse (``FLX-3119``) a declared plugin the command line did not name.

    There is no environment variable and no bundle key for the permission: a bundle is untrusted
    input, and the person who runs the command names each plugin they mean to run.
    """
    named = set(allowed)
    missing = sorted({item.plugin for item in declarations if item.plugin not in named})
    if not missing:
        return
    declared = ", ".join(sorted({item.plugin for item in declarations}))
    flags = " ".join(f"--allow-generator {name}" for name in missing)
    raise FluxError(
        FLX_3119,
        f"the bundle declares the generator plugin(s) {declared}, and this command allows "
        f"{', '.join(sorted(named)) or 'none'}; only the person running fluxsim decides whether "
        f"a plugin runs, after reviewing it, by adding {flags} to their own command; an agent "
        f"never adds it on its own judgement: it stops and asks that person, naming the plugin",
    )


def load_declared(
    declarations: Sequence[GeneratorDeclaration],
    allowed: Sequence[str],
    *,
    forbidden_roots: Sequence[Path],
) -> Dict[str, LoadedGenerator]:
    """Every declared plugin, loaded, by the ``exposeId`` it generates; ``{}`` when none is declared.

    The permission is checked for every plugin first, then every plugin is resolved (nothing is
    imported), and only then is the first one imported, so a refusal of the third plugin never
    follows the code of the first.
    """
    if not declarations:
        return {}
    require_allowed(declarations, allowed)
    resolved: List[ResolvedGenerator] = [
        registry.resolve(
            item.plugin, item.distribution, item.version, forbidden_roots=forbidden_roots
        )
        for item in declarations
    ]
    loaded: Dict[str, LoadedGenerator] = {}
    for item, found in zip(declarations, resolved, strict=True):
        factory = registry.load(found, expose_id=item.expose_id)
        try:
            plugin = factory(copy.deepcopy(dict(item.options)))
        except FluxError:
            raise
        except Exception as error:  # noqa: BLE001 (a plugin's own failure is its refusal, by name)
            raise FluxError(
                FLX_3124,
                f"{registry.plugin_label(found, item.expose_id)}: calling the factory failed "
                f"({registry.failure_text(error)})",
            ) from error
        loaded[item.expose_id] = LoadedGenerator(declaration=item, resolved=found, plugin=plugin)
    return loaded


def declared_statement(declaration: GeneratorDeclaration) -> Dict[str, Any]:
    """What the bundle says the plugin states: the declaration without its pin and its options."""
    document: Dict[str, Any] = {
        "grade": declaration.grade.value,
        "reproducibility": declaration.reproducibility,
    }
    if declaration.privacy is not None:
        document["privacy"] = declaration.privacy.to_document()
    if declaration.model_digest is not None:
        document["model"] = {"digest": declaration.model_digest}
    return document


def _flatten(document: Mapping[str, Any]) -> Dict[str, Any]:
    """*document* with its nested mappings as dotted names: ``privacy.epsilon``."""
    flat: Dict[str, Any] = {}
    for key, value in document.items():
        if isinstance(value, Mapping):
            for inner, item in value.items():
                flat[f"{key}.{inner}"] = item
        else:
            flat[str(key)] = value
    return flat


def _shown(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _SHOWN else text[:_SHOWN] + "..."


def check_statement(loaded: LoadedGenerator, request: Request) -> None:
    """Refuse (``FLX-3120``) a plugin whose statement differs from its declaration, naming the parts.

    The plugin's ``statement(request)`` must return a mapping that holds the declaration's grade,
    its privacy account, its model digest and its reproducibility, and no more: a part the bundle
    does not declare is a part the plugin may not state.
    """
    declared = _flatten(declared_statement(loaded.declaration))
    try:
        stated = loaded.plugin.statement(request)
    except FluxError:
        raise
    except Exception as error:  # noqa: BLE001 (a plugin's own failure is its refusal, by name)
        raise FluxError(
            FLX_3120,
            f"{loaded.name} could not state what it rests on ({type(error).__name__})",
        ) from error
    if not isinstance(stated, Mapping):
        raise FluxError(
            FLX_3120,
            f"{loaded.name} stated a {type(stated).__name__}, not a mapping of its grade, "
            "its privacy account, its model digest and its reproducibility",
        )
    found = _flatten(stated)
    differing: List[str] = []
    for name in sorted(set(declared) | set(found)):
        if name not in found:
            differing.append(f"{name} (declared {_shown(declared[name])}, not stated)")
        elif name not in declared:
            differing.append(f"{name} (stated {_shown(found[name])}, not declared)")
        elif declared[name] != found[name] or type(declared[name]) is not type(found[name]):
            differing.append(
                f"{name} (declared {_shown(declared[name])}, stated {_shown(found[name])})"
            )
    if differing:
        raise FluxError(
            FLX_3120,
            f"{loaded.name} for {loaded.declaration.expose_id!r} states something other than "
            f"the bundle declares: {'; '.join(differing)}",
        )


def forbidden_roots(
    bundle_directory: Path, state_directory: Optional[Path], current: Optional[Path] = None
) -> List[Path]:
    """The directories a plugin's code may not lie under: the bundle, the state directory, the cwd."""
    roots = [Path(bundle_directory)]
    roots.append(Path(state_directory) if state_directory is not None else Path(".flux"))
    roots.append(Path.cwd() if current is None else current)
    return [root.resolve() for root in roots]
