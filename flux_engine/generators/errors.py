# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error codes raised when a generator plugin is looked up, allowed, loaded and run.

KERNEL-SPEC §13 gives 3000 to "seam/conformance", and the seam's own codes
fill ``FLX-3101`` to ``FLX-3113``, so a generator plugin's refusals take the
next numbers of that family.

This module imports nothing beyond the error catalog, so ``fluxsim explain``
and the documentation generator can load every code without any plugin
installed.
"""

from __future__ import annotations

from flux_engine.core.errors import ErrorCode, register

__all__ = [
    "FLX_3114",
    "FLX_3115",
    "FLX_3116",
    "FLX_3117",
    "FLX_3118",
    "FLX_3119",
    "FLX_3120",
    "FLX_3121",
    "FLX_3122",
    "FLX_3123",
    "FLX_3124",
]

FLX_3114: ErrorCode = register(
    "FLX-3114",
    "no generator plugin entry point has that name",
    "A bundle names a generator plugin by an entry-point name: a letter, then letters, "
    "digits, underscores, dots or hyphens, and never a module path, a file or an object "
    "reference. The engine reads only the entry points the group flux_engine.generators "
    "lists in this interpreter, and it found none of that name. The message says what "
    "this environment has. Install the distribution that provides the plugin, in the "
    "interpreter that runs the engine, or correct the name in the bundle. Nothing was "
    "imported.",
)

FLX_3115: ErrorCode = register(
    "FLX-3115",
    "two generator plugin entry points have one name",
    "A plugin is named by its entry point, so exactly one entry point of that name must "
    "exist. When two installed distributions register the same name, the engine does "
    "not take the first on sys.path, because which one comes first depends on the "
    "environment and on the current directory. The message names every distribution and "
    "version that registers the name. Uninstall all but the distribution the bundle "
    "pins. Nothing was imported.",
)

FLX_3116: ErrorCode = register(
    "FLX-3116",
    "a generator plugin comes from another distribution or version than the bundle pins",
    "A bundle pins the distribution and the exact version that must provide a plugin. "
    "The distribution is compared by its normalized name (lower case, with every run of "
    "hyphens, underscores and dots as one hyphen), and the version as written. The "
    "message names the distribution and version the entry point comes from and the ones "
    "the bundle declares. Install the pinned version, or change the pin in the bundle "
    "and review the change. Nothing was imported.",
)

FLX_3117: ErrorCode = register(
    "FLX-3117",
    "a generator plugin is not installed where its code is",
    "A plugin loads only from an installation directory of this interpreter: the "
    "purelib and platlib directories of its sysconfig, and the user site when it is "
    "enabled. The distribution's metadata directory must lie under one of them. Unless "
    "the distribution is an editable install, the origin of the entry point's top-level "
    "module must lie under the same directory, be listed in the distribution's RECORD, "
    "and lie under none of the bundle directory, the state directory and the current "
    "directory, so that a .dist-info folder beside a bundle, or a module that python "
    "finds first in the current directory, cannot stand in for an installed plugin. The "
    "message names the paths found. Install the plugin with a package installer into "
    "this interpreter, and run the command from another directory. Nothing was imported.",
)

FLX_3118: ErrorCode = register(
    "FLX-3118",
    "the code a generator plugin loaded is not the code that was checked",
    "The engine checks where a plugin's top-level module is before it imports anything. "
    "After the import it checks again that the loaded object's module file lies inside "
    "that checked package and, for an installed distribution, is listed in its RECORD. "
    "A module swapped between the check and the import (a changed sys.path, or a "
    "module already in sys.modules) fails this check, and so does an entry point whose "
    "loaded object has no module file. The message names the file found and the package "
    "expected. Reinstall the plugin and run the command again. A plugin whose import or "
    "factory raises is FLX-3124.",
)

FLX_3119: ErrorCode = register(
    "FLX-3119",
    "a declared generator plugin was not allowed on the command line",
    "A bundle is untrusted input, and any installed distribution can register an entry "
    "point, so a bundle's declaration alone never loads a plugin. A command that would "
    "import one (plan, run, apply when it generates rows, and replay) needs "
    "--allow-generator NAME for each plugin the bundle names, and there is no "
    "environment variable and no bundle key for it. The message names the plugins the "
    "bundle declares and the ones the command allowed. A plugin is trusted code that runs "
    "with the rights of the person who runs it, so only the person running fluxsim decides "
    "whether a plugin runs, after reviewing it, and they do so by adding "
    "--allow-generator NAME themselves. An agent never adds --allow-generator on its own "
    "judgement: it stops and asks that person, naming the plugin. FLUX does not sandbox "
    "a plugin's network or file access, so allow only plugins you trust, and run a bundle "
    "you did not write in a container with no network. Nothing was imported.",
)

FLX_3120: ErrorCode = register(
    "FLX-3120",
    "a generator plugin states something other than the bundle declares",
    "The bundle declares each plugin's grade, its privacy account (the unit of "
    "privacy, the loss and the accounting, for P4), its model digest and its "
    "reproducibility, and the declaration is bound into inputDigest. The plugin's own "
    "statement is read after it loads and before any row is drawn, and it must repeat "
    "the declaration exactly, so a plugin cannot claim more than the bundle was "
    "reviewed for. The message names each part that differs. Correct the bundle or the "
    "plugin, and review the change.",
)

FLX_3121: ErrorCode = register(
    "FLX-3121",
    "a generator plugin returned values its expose does not accept",
    "The engine asks a plugin for the columns it generates, for rows start to stop, and "
    "checks what comes back before any of it is written: one list per requested column, "
    "each as long as the range, and every value against its column: its type, its "
    "nullability, its declared range, its length and its levels, and the contract's "
    "checks, which the engine holds on every row it covers. It refuses the first failure, "
    "naming the plugin, the column and the row, and repairs nothing. The message never "
    "quotes a value. A plugin that raises is refused the same way, by the name of the "
    "error. Correct the plugin, or the declaration the plugin is held to.",
)

FLX_3122: ErrorCode = register(
    "FLX-3122",
    "a plan that declares a generator plugin must stage its rows",
    "A plugin's values are not drawn from the engine's kernel, and many models give the "
    "same bytes only in the environment that ran them, so the run of a bundle that "
    "declares generators is generated once, at plan, and its stored bytes are what "
    "apply commits. A plan made with --no-stage keeps only plan.json and leaves apply to "
    "generate again, which a plugin's run never does, so it is refused before any plugin "
    "code runs. Plan without --no-stage, or use fluxsim run, which holds its execution "
    "in memory and writes it once.",
)

FLX_3123: ErrorCode = register(
    "FLX-3123",
    "a generator plugin's run is committed from its stage, never generated again",
    "apply --run-id commits the rows that plan staged for a bundle that declares "
    "generators, after checking them against their record, and it never asks a plugin "
    "for them a second time. A run that is already committed is kept as it stands: apply, "
    "run and the Python facade neither ask the plugin again nor write its directory, and "
    "apply --run-id RUN_ID --sink delivers its files without the plugin. It refuses when "
    "the run has no stage made for this plan (none was made, or it was made for another "
    "--simulation-id or --zone than this call names): run fluxsim plan again without "
    "--no-stage, and apply with the --simulation-id and --zone it was planned with. It "
    "refuses --strict-coverage, on apply and on run alike, because it asks for the rows to "
    "be generated again and a kept run records no coverage to check: apply or run "
    "without it, or plan with it. It also refuses --shard, because a share would ask the "
    "plugin for its rows, and the stage holds the whole run: apply the run whole. Whether "
    "a run's plugin bytes are stored is decided by the bundle's declaration, never by "
    "plan.json, which sits in a state directory that can be "
    "written by someone else: a plan.json whose generators record is missing or differs from "
    "the declaration (the plugin, its distribution and version, the grade, the privacy "
    "account or the model digest) is refused by apply, replay and ship, and the message "
    "quotes no value; plan the run again. replay verifies by generating again, so it is "
    "refused, and says that it verified nothing and that the stored bytes are the run of "
    "record, for a plugin the bundle declares none, for an editable install, and for one "
    "declared environment when the environment it measures is not the recorded one; a "
    "replay never exits 0 from the stored run.",
)

FLX_3124: ErrorCode = register(
    "FLX-3124",
    "a generator plugin could not be loaded",
    "The engine imports the plugin's module and calls its factory only after the "
    "bundle, the command line and the installation checks have passed. Any exception "
    "raised by either step, such as a syntax error or an error at import, an entry "
    "point that names nothing, a module that is missing or a factory that raises, "
    "ends the command with this code. The message names the expose, the plugin the "
    "bundle declares, the phase (importing the module, or calling the factory), and "
    "the type and first line of the exception. A missing module is named, and the "
    "message says whether it is the plugin's own module or one it imports. The "
    "message never quotes the plugin's options and prints no traceback. Install the "
    "plugin's dependencies, or correct the plugin, and run the command again.",
)
