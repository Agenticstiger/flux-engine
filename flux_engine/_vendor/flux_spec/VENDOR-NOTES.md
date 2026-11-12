# Vendor notes — `flux_engine/_vendor/flux_spec/`

## Provenance

Copied verbatim (byte-for-byte, then edited as listed below) from
`Agenticstiger/flux`. The pinned commit below is the provenance; it is
reproducible from a clone of that repository anywhere.

**Upstream commit pinned:** `0181eeb21f35355c1f88bb8f06c8335493d94611`

Retrieved via `git show <commit>:<path>` rather than the working tree,
because the FLUX checkout had uncommitted, in-flight changes at copy time
(a `feat/flux-spec-package` branch already mid-refactor into a `flux_spec/`
package with `scripts/*.py` becoming compatibility shims — exactly the
public-distribution work planned for a later increment). Pinning to the
last commit keeps this vendor copy reproducible and independent of that
unmerged, unstable work.

Files copied, each verified byte-identical to the pinned blob before any
edit was applied:

| Vendored path | Upstream path (at the pinned commit) |
|---|---|
| `validate.py` | `scripts/validate.py` |
| `enforce.py` | `scripts/enforce.py` |
| `bundle.py` | `scripts/bundle.py` |
| `schema/*.json` (11 files) | `schema/*.json` |
| `vendor/fluid/*.json` (3 files) | `vendor/fluid/*.json` |
| `vectors/enforcement-vectors.json` | `tests/enforcement-vectors.json` |

`tests/test_regression.py` was read for context (not vendored — flux_engine
has its own test suite; `test_spec_shim.py` exercises this vendored copy
directly instead of re-running the upstream regression file).

## Lines changed (minimal edits, path resolution only)

No logic, control flow, or behavior was changed. Every edit is a
`Path(__file__)`-relative constant, changed because this copy lives
directly inside the vendored package directory instead of two levels
below a repository root under `scripts/`.

### `validate.py`

```diff
-REPO = Path(__file__).resolve().parent.parent
+REPO = Path(__file__).resolve().parent
 FLUX_SCHEMA_PATH = REPO / "schema" / "flux-schema-latest.json"
 FLUID_VENDOR_DIR = REPO / "vendor" / "fluid"
```

`FLUX_SCHEMA_PATH` and `FLUID_VENDOR_DIR` are unchanged — they already
resolve correctly once `REPO` points at this directory (which now itself
contains `schema/` and `vendor/fluid/`).

Left intentionally unfixed (inert in flux_engine's usage): `main()`'s
no-argument branch does `(REPO / "examples").iterdir()` to discover bundle
directories for the standalone `python3 scripts/validate.py` CLI. This
vendored copy has no `examples/` directory and `flux_engine.spec.validate`
never calls `main()` — only `Bundle`, `canonical_digest`,
`load_yaml_docs`, and `semver_satisfies` are imported by the shim
(`flux_engine/spec/validate.py`). If a future increment wants the
standalone CLI to work from this vendored copy, that branch needs its own
fix at that time.

### `enforce.py`

```diff
-REPO = Path(__file__).resolve().parent.parent
-DEFAULT_VECTORS = REPO / "tests" / "enforcement-vectors.json"
+REPO = Path(__file__).resolve().parent
+DEFAULT_VECTORS = REPO / "vectors" / "enforcement-vectors.json"
```

The vectors file is vendored under `vectors/` (not `tests/`) per the task
spec, so the directory segment changed along with the `.parent` count.

### `bundle.py`

```diff
-REPO = Path(__file__).resolve().parent.parent
-sys.path.insert(0, str(REPO / "scripts"))
-from validate import Bundle, load_yaml_docs  # noqa: E402
+REPO = Path(__file__).resolve().parent
+from .validate import Bundle, load_yaml_docs  # noqa: E402
```

`validate.py` is now a sibling module inside the same Python package
(`flux_engine._vendor.flux_spec`) rather than a same-directory *script*
next to `bundle.py` on `sys.path`, so the `sys.path.insert` + bare
top-level `import validate` becomes an ordinary package-relative import.
`import sys` was dropped from the top-of-file import block because the
removed `sys.path.insert` call was its only use in the module.

`SCHEMA_PATH`, `MANIFEST_SCHEMA_PATH`, and the `-o/--out` argparse default
(`REPO / "dist"`) are unchanged — all already resolve correctly under the
one-level-shallower `REPO`.

## License

Apache License, Version 2.0. See `NOTICE` in this directory.

## Provenance check (2026-09-06)

All vendored JSON files are byte-identical to `origin/main` (`35d979f`) of Agenticstiger/flux; the three Python modules are identical to `origin/main` except for the path edits listed above.

## Re-sync (2026-11-12): FLUX 0.5.1

The JSON data files were re-synced from Agenticstiger/flux at `9be1bd0` (Agenticstiger/flux#20), the
commit that adds FLUX 0.5.1 (FLUID 0.7.6 at the seam). The three Python modules
are unchanged: they still differ from upstream only by the path edits above.

- New: `schema/flux-schema-0.5.1.json`, `schema/flux-ui-hints-0.5.1.json` and
  `vendor/fluid/fluid-schema-0.7.6.json`.
- Moved: `schema/flux-schema-latest.json` now aliases 0.5.1.
- Caught up with upstream changes made since `35d979f`:
  `schema/flux-schema-0.5.0.json` (the reworded `agentPolicy` description) and
  `vendor/fluid/fluid-schema-0.7.3.json` to `0.7.5.json`, which FLUX re-vendored
  from FLUID (0.7.5 gains the pgvector binding).
- Unchanged: the enforcement and manifest schemas and
  `vectors/enforcement-vectors.json`.

Every vendored JSON file is byte-identical to its upstream copy at that commit.
The copy now holds 13 schema files and 4 FLUID schemas; the provenance table at
the top lists the 11 and 3 of the original vendoring.
