# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which generator plugins are installed, which one a bundle means, and loading it.

A third-party generator registers under an entry-point group, without touching
the engine::

    [project.entry-points."flux_engine.generators"]
    learned = "my_package.plugin:factory"

Four things are kept apart, in the order the engine needs them:

* :func:`available` lists every entry point of the group without importing
  any of them (``fluxsim plugins`` calls it).
* :func:`resolve` turns a declared ``(name, distribution, version)`` into
  exactly one installed entry point, and refuses before anything is imported
  unless the distribution is installed where its code is.
* :func:`load` imports the entry point and then checks that what loaded is the
  code :func:`resolve` checked.
* Nothing here reads a bundle, a state directory or a module path: the only
  source is the group's entry points, and a name is never a module path.

Why the checks: ``importlib.metadata`` finds a ``.dist-info`` directory
anywhere on ``sys.path``, so one beside a bundle advertises an entry point,
and ``python -m`` puts the current directory first on ``sys.path``, so a
package there shadows an installed one. An installed distribution has its
metadata and its top-level module under one installation directory, and its
module listed in its ``RECORD``. An editable install keeps its code in a
source tree, so it has no ``RECORD`` to check; its top-level module must
lie under the source directory its ``direct_url.json`` records, and it is
returned marked editable.

Every name, version and path a refusal quotes comes from an environment, a
bundle or a distribution, so each goes through :func:`printable` first.

Standard library only.
"""

from __future__ import annotations

import json
import re
import site
import sys
import sysconfig
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from importlib import metadata, util
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from flux_engine.core.credentials import MASK, dsn_secret_values
from flux_engine.core.errors import FluxError
from flux_engine.generators.errors import FLX_3114, FLX_3115, FLX_3116, FLX_3117, FLX_3118, FLX_3124
from flux_engine.seam.coverage import printable
from flux_engine.sinks._secrets import scrub

__all__ = [
    "ENTRY_POINT_GROUP",
    "GeneratorInfo",
    "ResolvedGenerator",
    "available",
    "installation_directories",
    "load",
    "normalize_distribution",
    "resolve",
]

#: The entry-point group third-party generator plugins register under.
ENTRY_POINT_GROUP = "flux_engine.generators"

#: What an entry-point name may be: never a module path, a file or a reference.
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")

_SEPARATORS = re.compile(r"[-_.]+")


@dataclass(frozen=True)
class GeneratorInfo:
    """One generator entry point ``fluxsim plugins`` lists, found without importing it."""

    name: str
    value: str
    distribution: str
    version: str
    editable: bool
    outside: bool

    @property
    def source(self) -> str:
        return f"{self.distribution} {self.version}".strip() or "entry point"

    def as_document(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "distribution": self.distribution,
            "version": self.version,
            "editable": self.editable,
            "outside": self.outside,
        }


@dataclass(frozen=True)
class ResolvedGenerator:
    """An entry point that passed :func:`resolve`; :func:`load` imports it."""

    name: str
    value: str
    distribution: str
    version: str
    editable: bool
    #: The installation directory that holds the distribution's metadata.
    install_root: Path
    #: The top-level package directory, or the module file of a single-file
    #: module, that was checked.
    package_root: Optional[Path]
    #: Every file the distribution's ``RECORD`` lists, resolved. Empty for an
    #: editable install, whose files are not checked against it.
    record: FrozenSet[Path] = frozenset()
    entry_point: Any = field(default=None, compare=False, repr=False)


def normalize_distribution(name: str) -> str:
    """A distribution name as PEP 503 normalizes it: lower case, runs of ``-_.`` as ``-``."""
    return _SEPARATORS.sub("-", name).lower()


def installation_directories() -> Tuple[Path, ...]:
    """This interpreter's installation directories: ``purelib``, ``platlib``, the user site.

    The user site counts only when it is enabled. Kept in one function so that
    a test can replace it.
    """
    paths = sysconfig.get_paths()
    found: List[str] = [paths["purelib"], paths["platlib"]]
    if site.ENABLE_USER_SITE:
        user_site = site.getusersitepackages()
        if user_site:
            found.append(user_site)
    directories: List[Path] = []
    for entry in found:
        directory = Path(entry).resolve()
        if directory not in directories:
            directories.append(directory)
    return tuple(directories)


def _entry_points() -> Sequence[Any]:
    return tuple(metadata.entry_points(group=ENTRY_POINT_GROUP))


def _distribution_name(dist: Any) -> str:
    if dist is None:
        return ""
    return str(dist.metadata["Name"] or "")


def _distribution_version(dist: Any) -> str:
    if dist is None:
        return ""
    return str(dist.version or "")


def _metadata_directory(dist: Any) -> Optional[Path]:
    """Where the distribution's metadata directory is, or ``None`` when it is not a directory.

    ``importlib.metadata`` offers no public name for it; its file-system
    distribution keeps it as ``_path``.
    """
    path = getattr(dist, "_path", None)
    if path is None:
        return None
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return None


def _is_editable(dist: Any) -> bool:
    """``dir_info.editable`` of the distribution's ``direct_url.json``, true or not."""
    if dist is None:
        return False
    try:
        text = dist.read_text("direct_url.json")
        document = json.loads(text) if text else None
    except (OSError, ValueError):
        return False
    if not isinstance(document, dict):
        return False
    dir_info = document.get("dir_info")
    return isinstance(dir_info, dict) and dir_info.get("editable") is True


def _editable_source(dist: Any) -> Optional[Path]:
    """The source directory an editable distribution's ``direct_url.json`` records, or ``None``.

    The ``url`` must use the ``file:`` scheme (RFC 8089) and name this host: a path
    is read from it, percent-decoded, and resolved.
    """
    try:
        text = dist.read_text("direct_url.json")
        document = json.loads(text) if text else None
    except (OSError, ValueError):
        return None
    url = document.get("url") if isinstance(document, dict) else None
    if not isinstance(url, str):
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "file" or parts.netloc not in ("", "localhost") or not parts.path:
        return None
    try:
        return Path(urllib.request.url2pathname(parts.path)).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _install_root(directory: Optional[Path]) -> Optional[Path]:
    if directory is None:
        return None
    for root in installation_directories():
        if _under(directory, root):
            return root
    return None


def available() -> Tuple[GeneratorInfo, ...]:
    """Every generator entry point of this environment, sorted by name, none imported."""
    found: List[GeneratorInfo] = []
    for entry_point in _entry_points():
        dist = getattr(entry_point, "dist", None)
        found.append(
            GeneratorInfo(
                name=entry_point.name,
                value=entry_point.value,
                distribution=_distribution_name(dist),
                version=_distribution_version(dist),
                editable=_is_editable(dist),
                outside=_install_root(_metadata_directory(dist)) is None,
            )
        )
    return tuple(sorted(found, key=lambda info: (info.name, info.distribution, info.version)))


def _label(dist: Any) -> str:
    name = printable(_distribution_name(dist)) or "an unnamed distribution"
    version = printable(_distribution_version(dist))
    return f"{name} {version}" if version else name


def _known_names() -> str:
    names = sorted({point.name for point in _entry_points()})
    return ", ".join(printable(name) for name in names) if names else "none"


def _record_of(dist: Any) -> FrozenSet[Path]:
    files = dist.files
    if not files:
        return frozenset()
    listed = set()
    for file in files:
        try:
            listed.add(Path(dist.locate_file(file)).resolve())
        except (OSError, RuntimeError):
            continue
    return frozenset(listed)


def _origin_of(top_level: str) -> Optional[Path]:
    """The file the top-level module name would import from, found without importing it."""
    try:
        spec = util.find_spec(top_level)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.has_location or not spec.origin:
        return None
    try:
        return Path(spec.origin).resolve()
    except (OSError, RuntimeError):
        return None


def _package_root(origin: Path) -> Path:
    return origin.parent if origin.name.startswith("__init__.") else origin


def resolve(
    name: str,
    distribution: str,
    version: str,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> ResolvedGenerator:
    """The one installed entry point called *name*, from *distribution* at *version*.

    Refuses (``FLX-3114`` to ``FLX-3117``) before anything is imported when
    there is no entry point of that name, when two or more have it, when its
    distribution or version is not the declared one, or when the distribution
    is not installed where its code is. *forbidden_roots* are the bundle
    directory, the state directory and the current directory: a non-editable
    plugin's top-level module may lie under none of them, and an editable
    plugin's must lie under the source directory its ``direct_url.json``
    records and under none of them either (``FLX-3117``), so a bundle directory inside the
    source tree cannot shadow the module. A root that holds the whole installation directory (a
    virtual environment inside the project), or, for an editable plugin, the whole source
    directory (a command run from the plugin's own source root), is not one that can shadow it,
    and is skipped for that directory.
    """
    if not _NAME.fullmatch(name):
        raise FluxError(
            FLX_3114,
            f"{name!r} is not an entry-point name (a letter, then letters, digits, "
            f"underscores, dots or hyphens); this environment has {_known_names()}",
        )
    matches = [point for point in _entry_points() if point.name == name]
    if not matches:
        raise FluxError(
            FLX_3114,
            f"there is no generator entry point called {name!r}; this environment has "
            f"{_known_names()}",
        )
    if len(matches) > 1:
        labels = ", ".join(sorted(_label(getattr(point, "dist", None)) for point in matches))
        raise FluxError(
            FLX_3115,
            f"{len(matches)} generator entry points are called {name!r}, from {labels}; "
            "uninstall all but the one the bundle pins",
        )
    entry_point = matches[0]
    dist = getattr(entry_point, "dist", None)
    found = _label(dist)
    if (
        dist is None
        or normalize_distribution(_distribution_name(dist)) != normalize_distribution(distribution)
        or _distribution_version(dist) != version
    ):
        raise FluxError(
            FLX_3116,
            f"the entry point {name!r} comes from {found}; the bundle pins "
            f"{printable(distribution)} {printable(version)}",
        )

    directory = _metadata_directory(dist)
    root = _install_root(directory)
    if root is None:
        raise FluxError(
            FLX_3117,
            f"{found} keeps its metadata at "
            f"{printable(str(directory)) if directory else 'no directory'}, "
            "which is under no installation directory ("
            f"{', '.join(printable(str(path)) for path in installation_directories()) or 'none'})",
        )
    editable = _is_editable(dist)
    top_level = entry_point.module.split(".")[0]
    origin = _origin_of(top_level) if top_level else None
    if editable:
        source = _editable_source(dist)
        if source is None:
            raise FluxError(
                FLX_3117,
                f"{found} is an editable install whose direct_url.json records no file: "
                "URL of a source directory, so there is nothing to check its code against",
            )
        if origin is None or not _under(origin, source):
            raise FluxError(
                FLX_3117,
                f"{found}: the top-level module {top_level!r} would import from "
                f"{printable(str(origin)) if origin is not None else 'no file'}, which is not "
                f"under {printable(str(source))}, the source directory it was installed from",
            )
        for forbidden in forbidden_roots:
            barred = Path(forbidden).resolve()
            if _under(source, barred):
                continue
            if _under(origin, barred):
                raise FluxError(
                    FLX_3117,
                    f"{found}: {printable(str(origin))} lies under {printable(str(barred))}, "
                    "a directory of this run that does not hold the source directory the "
                    "distribution was installed from, so it may be a module that python found "
                    "there first",
                )
        return ResolvedGenerator(
            name=name,
            value=entry_point.value,
            distribution=_distribution_name(dist),
            version=_distribution_version(dist),
            editable=True,
            install_root=root,
            package_root=_package_root(origin),
            entry_point=entry_point,
        )

    if origin is None:
        raise FluxError(
            FLX_3117,
            f"{found}: the top-level module {top_level!r} of {entry_point.value!r} "
            "has no single file to check",
        )
    if not _under(origin, root):
        raise FluxError(
            FLX_3117,
            f"{found}: the top-level module {top_level!r} would import from "
            f"{printable(str(origin))}, which is not under {printable(str(root))}, "
            "where its metadata is",
        )
    record = _record_of(dist)
    if origin not in record:
        raise FluxError(
            FLX_3117,
            f"{found}: {printable(str(origin))} is not listed in its RECORD",
        )
    for forbidden in forbidden_roots:
        barred = Path(forbidden).resolve()
        if _under(root, barred):
            continue
        if _under(origin, barred):
            raise FluxError(
                FLX_3117,
                f"{found}: {printable(str(origin))} lies under {printable(str(barred))}, "
                "a directory of this run, "
                "so it may be a module that python found there first",
            )
    return ResolvedGenerator(
        name=name,
        value=entry_point.value,
        distribution=_distribution_name(dist),
        version=_distribution_version(dist),
        editable=False,
        install_root=root,
        package_root=_package_root(origin),
        record=record,
        entry_point=entry_point,
    )


def plugin_label(resolved: ResolvedGenerator, expose_id: Optional[str]) -> str:
    """The plugin as a refusal names it: the expose it generates, its reference and its pin."""
    where = f"expose {printable(expose_id)}, " if expose_id is not None else ""
    return (
        f"{where}plugin {printable(resolved.name)} "
        f"({printable(resolved.distribution)} {printable(resolved.version)})"
    )


def _module_file(loaded: Any) -> Optional[Path]:
    module: Optional[ModuleType]
    if isinstance(loaded, ModuleType):
        module = loaded
    else:
        module = sys.modules.get(str(getattr(loaded, "__module__", "")))
    file = getattr(module, "__file__", None)
    if not isinstance(file, str):
        return None
    try:
        return Path(file).resolve()
    except (OSError, RuntimeError):
        return None


_FIRST_LINE = 200


def plugin_text(text: str) -> str:
    """*text*, which a plugin wrote, with every credential this process can name removed.

    A plugin's message is free text: it may hold the URL of the service it called, a connection
    string, or the value of a credential it read from the environment. The URL is written as its
    label, each secret a connection string in it holds (the password of ``user:PW@host``, the value
    of ``password=``, ``token=`` or ``sig=``) and each value of a credential-named environment
    variable is replaced by ``***``. Call it on the whole text, before any line is picked or any
    length is cut: a cut can leave four characters of a secret that no longer match it.
    """
    known = [(secret, MASK) for secret in dict.fromkeys(dsn_secret_values(text))]
    return scrub(text, known)


def failure_text(error: BaseException) -> str:
    """The type and first line of *error*, as it is written into a diagnostic.

    The message is scrubbed (:func:`plugin_text`) whole, then its first line is taken and cut.
    """
    lines = plugin_text(str(error)).strip().splitlines()
    first = lines[0] if lines else ""
    if len(first) > _FIRST_LINE:
        first = first[:_FIRST_LINE] + "..."
    name = plugin_text(type(error).__name__)
    return printable(f"{name}: {first}" if first else name)


def _missing_module(resolved: ResolvedGenerator, error: ModuleNotFoundError) -> str:
    """Name the module a ``ModuleNotFoundError`` lacks, and whether it is the plugin's own."""
    missing = error.name
    if not missing:
        return ""
    module = resolved.value.split(":", 1)[0].strip()
    own = module == missing or module.startswith(missing + ".")
    return (
        f"; the module {printable(plugin_text(missing))} is missing, and it is "
        f"{'the plugin' + chr(39) + 's own module' if own else 'a module it imports'}"
    )


def load(resolved: ResolvedGenerator, *, expose_id: Optional[str] = None) -> Any:
    """Import the entry point of *resolved*, then check that the checked code is what loaded.

    The loaded object's module file must lie inside the top-level package that
    :func:`resolve` checked and, unless the distribution is editable, be listed
    in its ``RECORD``. Anything else is ``FLX-3118``. An exception raised while
    importing is ``FLX-3124``, naming *expose_id* when it is given.
    """
    found = f"{printable(resolved.distribution)} {printable(resolved.version)}"
    try:
        loaded = resolved.entry_point.load()
    except Exception as error:  # noqa: BLE001 (a plugin's own failure is its refusal, by name)
        extra = _missing_module(resolved, error) if isinstance(error, ModuleNotFoundError) else ""
        raise FluxError(
            FLX_3124,
            f"{plugin_label(resolved, expose_id)}: importing the module "
            f"{printable(resolved.value)} failed ({failure_text(error)}){extra}",
        ) from error
    file = _module_file(loaded)
    expected = resolved.package_root
    if file is None:
        raise FluxError(
            FLX_3118,
            f"{found}: the object the entry point {resolved.name!r} loaded has no module file",
        )
    if expected is not None and not _under(file, expected):
        raise FluxError(
            FLX_3118,
            f"{found}: the entry point {resolved.name!r} loaded code from "
            f"{printable(str(file))}, not from {printable(str(expected))}, which was checked",
        )
    if not resolved.editable and file not in resolved.record:
        raise FluxError(
            FLX_3118,
            f"{found}: {printable(str(file))} is not listed in its RECORD",
        )
    return loaded
