# ADR 0005: depend on flux_spec, vendored fallback until it ships

## Status

Accepted (W0). **Amended 2026-09-07:** the precondition this ADR was
written around has occurred. `flux_spec` 0.5.0 is published from the
public `Agenticstiger/flux` repository, and `flux-spec` is now a declared
core dependency in `pyproject.toml` -- a PEP 508 direct reference pinned
to a commit until the PyPI release lands. See "Consequences" below, whose
second bullet describes the state now in force.

**Amended again when the PyPI release landed:** the dependency is the version
range `flux-spec>=0.5,<0.6` instead of a commit pin. A release has the
advisory and yanked-release story a commit pin lacks, so the scheduled
pin-drift alert that stood in for it was retired.

## Context

The FLUX offline reference validator, the RFC-04 enforcement gate, and the
RFC-09 evidence bundler already exist, tested, in the FLUX specification
repository (`Agenticstiger/flux`) as `scripts/validate.py`, `enforce.py`,
and `bundle.py`. The approved plan's owner decisions commit to publishing
these as a public, Apache-2.0 `flux_spec` PyPI distribution in a later
increment (W1) -- until then, flux_engine cannot depend on a distribution
that does not exist yet.

(Written at W0. That increment has since landed: `flux_spec` 0.5.0 is
published and depended on -- see Status above. The paragraph is kept as
the context the decision was taken in, not as a description of today.)

## Decision

`flux_engine.spec` is a shim: it imports the public `flux_spec`
distribution when it is installed, and falls back to a vendored,
byte-for-byte copy of the three modules and their data files under
`flux_engine/_vendor/flux_spec/` otherwise (see that directory's own
`VENDOR-NOTES.md` for the exact upstream commit and the minimal
path-resolution edits applied). `flux_engine.spec.SOURCE` reports which
backend is active, and `fluxsim describe --self` surfaces it.

## Consequences

- flux_engine's enforcement-gate conformance (`fluxsim gate conform`) and
  future document validation both produce the identical verdict the
  public validator/gate would produce, by construction, whichever backend
  is active -- there is one enforcement decision function, imported, not
  reimplemented.
- The `flux_spec` distribution has shipped and is a declared core
  dependency, so the vendored copy is now a pure fallback (kept for an
  environment that has to run without it, and for an older flux_engine
  pinned from before the distribution existed)
  rather than the primary path. No code outside `flux_engine.spec` had to
  change, since every consumer already imports through the shim.
- Selection is not left to whatever happens to be installed. The shim
  refuses a `flux_spec` outside the supported range
  (`flux_engine.spec.SUPPORTED_SPEC_VERSION_RANGE`) rather than using it:
  this distribution supplies the enforcement decision, and silently
  substituting a different one for the pinned one is the failure mode the
  shim exists to prevent. `flux_engine.spec.SOURCE` and `SOURCE_VERSION`
  report which backend answered, and `fluxsim describe --self` surfaces both.
- Code and data come from one backend by construction. Data files are
  resolved by asking the active backend for the constant it already
  computed (`validate.FLUX_SCHEMA_PATH`, `enforce.DEFAULT_VECTORS`),
  never by rebuilding a directory layout from `__file__`. The probes that
  did the latter looked for `flux_spec/vectors/` and `flux_spec/schema/`;
  the published distribution keeps its data under `flux_spec/data/`, so
  with the distribution installed the engine ran its code and read the
  vendored schema and vectors -- silently, and reporting the wrong
  provenance.
