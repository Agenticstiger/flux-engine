# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The starter project ``fluxsim init`` writes: a small FLUX bundle and its FLUID contract.

The bundle lives in ``flux_engine/starter/bundle/`` as package data. It is read
through :mod:`importlib.resources`, so it works from a wheel, an editable
install and a zip import alike, and it is copied byte for byte: a bundle's
digests are taken over its bytes, so a newline translated on the way out would
give every user a different ``inputDigest`` for the same project.

Only specification documents belong in that directory. A bundle directory
holds ``*.flux.yml``, ``*.fluid.yml``, ``flux.lock`` and the evidence
manifest, and nothing else (``FLX-3108``). That is why the starter carries no
README, no ``.gitignore`` and no Python file: the next steps are printed by
the command instead (:mod:`flux_engine.cli._init`).

The one rule when editing a starter file: a pinned test
(``tests/starter/test_starter_bundle.py``) holds the ``runDigest`` the bundle
produces, so a change here moves it on purpose, with the new value written
into that test in the same commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Dict, List, Tuple

__all__ = ["PURPOSES", "ScaffoldError", "StarterFile", "scaffold", "starter_files"]

_PACKAGE = "flux_engine.starter"
_BUNDLE_DIR = "bundle"

#: One line per starter file, said in the order a reader meets them. The test
#: suite holds this equal to the files actually shipped, so a file added to the
#: bundle without a line here (or the reverse) fails a test rather than printing
#: a blank description to the first person who runs ``fluxsim init``.
PURPOSES: Dict[str, str] = {
    "simulation.flux.yml": "the run: seed, time window, and the contract it emits rows for",
    "world.flux.yml": "the population: size, lifecycle mix, sovereignty",
    "persona.flux.yml": "a kind of customer: traits and their distributions",
    "signal.flux.yml": "an event the world produces: a payment is aborted",
    "detector.flux.yml": "a pattern over signals; each firing is one row",
    "journey.flux.yml": "what the business does about it, and how customers react",
    "taxonomy.flux.yml": "the lifecycle states a journey may move a customer between",
    "treatment.flux.yml": "the action a journey takes: a one-tap payment link",
    "module.flux.yml": "bundles the signal, detector and journey into one swappable unit",
    "payment-recovery.fluid.yml": "the FLUID contract the rows are emitted for",
}

#: How many of the names found in a refused directory the message lists.
_LISTED_ENTRIES = 3


class ScaffoldError(Exception):
    """The directory cannot be scaffolded: it is not usable, or a write failed."""


@dataclass(frozen=True)
class StarterFile:
    """One file of the starter bundle: its name, what it is for, its exact bytes."""

    name: str
    purpose: str
    data: bytes


def starter_files() -> Tuple[StarterFile, ...]:
    """Every file of the starter bundle, in the order of :data:`PURPOSES`.

    That is the order a reader meets them in. A file with no entry there sorts
    after the rest, by name.
    """
    root = resources.files(_PACKAGE).joinpath(_BUNDLE_DIR)
    rank = {name: position for position, name in enumerate(PURPOSES)}
    entries = sorted(
        (entry for entry in root.iterdir() if entry.is_file()),
        key=lambda e: (rank.get(e.name, len(rank)), e.name),
    )
    return tuple(
        StarterFile(name=e.name, purpose=PURPOSES.get(e.name, ""), data=e.read_bytes())
        for e in entries
    )


def _check_usable(target: Path) -> List[Path]:
    """Refuse an unusable *target*; return the directories that do not exist yet.

    The returned list runs from *target* upward to the first existing parent, so
    that a failed scaffold can remove exactly the directories it created.
    """
    if target.is_symlink() and not target.exists():
        raise ScaffoldError(f"{target} is a symbolic link to nothing")
    if target.exists():
        if not target.is_dir():
            raise ScaffoldError(f"{target} exists and is not a directory")
        held = sorted(entry.name for entry in target.iterdir())
        if held:
            shown = ", ".join(held[:_LISTED_ENTRIES]) + (
                ", ..." if len(held) > _LISTED_ENTRIES else ""
            )
            raise ScaffoldError(
                f"{target} is not empty (it holds {shown}), and `fluxsim init` never overwrites "
                "anything; give it a directory that does not exist yet, or an empty one"
            )
        return []
    missing: List[Path] = []
    here = target
    while not here.exists() and here != here.parent:
        missing.append(here)
        here = here.parent
    return missing


def scaffold(directory: Path) -> Tuple[str, ...]:
    """Write the starter bundle into *directory* and return the names written.

    *directory* may be missing (it is created, parents included) or an empty
    directory. A directory that holds anything, a path that is a file, and a
    link to nothing are refused with a :class:`ScaffoldError` before a byte is
    written. Every file is created exclusively (``O_EXCL``), so nothing that
    appears in the directory between the check and the write can be overwritten
    or written through, a symbolic link included. If a write fails part way,
    every file this call created, the partly written one included, is removed,
    and so is each directory it created.
    """
    target = Path(directory)
    try:
        to_create = _check_usable(target)
    except OSError as error:
        raise ScaffoldError(f"cannot use {target}: {error.strerror or error}") from error

    written: List[Path] = []
    try:
        if to_create:
            target.mkdir(parents=True)
        for entry in starter_files():
            path = target / entry.name
            handle = path.open("xb")
            # From here the file is ours, even if the write below fails halfway.
            written.append(path)
            with handle:
                handle.write(entry.data)
    except OSError as error:
        for path in written:
            path.unlink(missing_ok=True)
        for created in to_create:
            try:
                created.rmdir()
            except OSError:
                # Not there (mkdir failed before reaching it) or not empty (someone
                # else wrote into it meanwhile): leave it, and try the next one up.
                continue
        raise ScaffoldError(
            f"could not write into {target}: {error.strerror or error}; "
            "what was written so far was removed"
        ) from error
    return tuple(path.name for path in written)
