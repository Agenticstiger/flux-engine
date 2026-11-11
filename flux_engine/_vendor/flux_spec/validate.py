#!/usr/bin/env python3
"""FLUX offline reference validator.

Validates FLUX document bundles: every document against the FLUX JSON Schema,
then the cross-document guarantees the schema alone cannot express:

  * every *Ref resolves to a document of the expected kind (no dangling links)
  * document ids are unique within a bundle (across .flux.yml AND .fluid.yml)
  * World.lifecycleMix and Experiment variant weights sum to 1
  * Experiment variant names are unique
  * JourneyTaxonomy transitions and Blueprint edges stay within their node sets
  * Journey.personaReactions keys resolve to Personas; nextState is a state of
    the referenced taxonomy
  * Simulation.timeBounds.start < end
  * skills bindings respect the document's agentPolicy (model allow-list,
    per-skill token budget)
  * every .fluid.yml in the bundle validates against the vendored FLUID schema
    for its declared fluidVersion (referenced or not)
  * the FLUID seam: Simulation.emits[].productRef resolves to a .fluid.yml in
    the bundle, the pinned fluidVersion matches the document's declaration,
    and exposeId is a member of its exposes[]
  * consent strictness at the seam: an emitted contract's expose
    policy.agentPolicy must not allow a use case denied by any ConsentProfile
    gating the Simulation's campaigns, and (when both enumerate allow-lists)
    must not allow more than the profile allows

Cross-document checks run only on documents that passed schema validation, so
malformed input yields error messages, never tracebacks. Unquoted YAML
timestamps are loaded as strings, matching the schema's RFC 3339 grammar.

Usage:
    python3 scripts/validate.py [bundle_dir ...]

With no arguments, validates every directory under examples/.
Runs fully offline: no network, no running engine. Exit code 0 = green.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import rfc8785
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover
    sys.exit("validate.py requires: pip install jsonschema pyyaml rfc8785")

# VENDOR EDIT (see VENDOR-NOTES.md): this module now lives directly inside
# the vendored package directory (flux_engine/_vendor/flux_spec/), not two
# levels below a repo root under scripts/, so REPO drops one ``.parent``.
REPO = Path(__file__).resolve().parent
FLUX_SCHEMA_PATH = REPO / "schema" / "flux-schema-latest.json"
FLUID_VENDOR_DIR = REPO / "vendor" / "fluid"

# *Ref field -> kind it must resolve to (None = any kind)
REF_KINDS = {
    "catalogRef": "Catalog",
    "treatmentRef": "Treatment",
    "segmentRef": "Segment",
    "channelRef": "Channel",
    "consentRef": "ConsentProfile",
    "worldRef": "World",
    "signalRef": "Signal",
    "taxonomyRef": "JourneyTaxonomy",
    "mapToPersona": "Persona",
    "personaRefs": "Persona",
    "segmentRefs": "Segment",
    "signalRefs": "Signal",
    "campaignRefs": "Campaign",
    "experimentRefs": "Experiment",
    "moduleRefs": "Module",
    "modules": "Module",
    "resources": None,
    "nodes": None,
}

# free-form payload objects the ref-walker must not descend into: keys inside
# them are user data, not references. "extensions" (RFC-02) is envelope-level
# and namespace-owned — its contents are never core refs.
FREE_FORM_KEYS = {"payloadTemplate", "config", "demographics", "attributes", "annotations", "labels", "extensions"}

SUM_TOLERANCE = 1e-9


def canonical_digest(doc) -> str:
    """Content digest for RFC-03 lockfiles and RFC-06 seam provenance.

    sha256 over the RFC 8785 (JCS) canonicalisation. JCS — not merely sorted
    keys — because a digest that two implementations compute differently is
    not a digest: JCS also fixes number spelling (1000.0 and 1000 serialise
    identically) and emits raw UTF-8 rather than backslash-u escapes, which
    `json.dumps` does not.
    """
    import hashlib
    return hashlib.sha256(rfc8785.dumps(doc)).hexdigest()


def semver_satisfies(version: str, rng: str) -> bool:
    """Minimal semver-range check for RFC-03: exact 'x.y.z', caret '^x.y[.z]'
    (same major, >= floor), tilde '~x.y[.z]' (same major.minor, >= floor)."""
    def parse(v):
        parts = v.split(".")
        if len(parts) < 3:
            parts += ["0"] * (3 - len(parts))
        return tuple(int(p) for p in parts[:3])

    try:
        got = parse(version)
        if rng.startswith("^"):
            floor = parse(rng[1:])
            return got[0] == floor[0] and got >= floor
        if rng.startswith("~"):
            floor = parse(rng[1:])
            return got[:2] == floor[:2] and got >= floor
        return got == parse(rng)
    except ValueError:
        return False


class _StringTimestampLoader(yaml.SafeLoader):
    """SafeLoader that keeps unquoted timestamps as plain strings.

    PyYAML would otherwise coerce `start: 2026-07-01T00:00:00Z` to a
    datetime object, which the schema (correctly) rejects as "not a string"
    with a confusing Python-typed error message.
    """


_StringTimestampLoader.add_constructor(
    "tag:yaml.org,2002:timestamp",
    lambda loader, node: loader.construct_scalar(node),
)


def load_yaml_docs(path: Path):
    """Load YAML documents; returns (docs, error_message_or_None)."""
    try:
        with path.open() as fh:
            docs = list(yaml.load_all(fh, Loader=_StringTimestampLoader))
    except (yaml.YAMLError, OSError) as exc:
        return [], f"unreadable YAML: {str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__}"
    return [d for d in docs if d is not None], None


def walk_refs(node, field_path, out):
    """Collect (field_name, ref_value, path) for every *Ref-ish field."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FREE_FORM_KEYS:
                continue
            here = f"{field_path}.{key}" if field_path else key
            if key in REF_KINDS:
                if isinstance(value, str):
                    out.append((key, value, here))
                elif isinstance(value, list):
                    out.extend((key, v, f"{here}[{i}]") for i, v in enumerate(value) if isinstance(v, str))
            walk_refs(value, here, out)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            walk_refs(item, f"{field_path}[{i}]", out)


class Bundle:
    def __init__(self, directory: Path, flux_validator: Draft202012Validator):
        self.dir = directory
        self.flux_validator = flux_validator
        self.errors: list[str] = []
        self.flux_docs: dict[str, dict] = {}   # schema-VALID flux docs only
        self.fluid_docs: dict[str, tuple[Path, dict]] = {}
        self._fluid_validators: dict[str, Draft202012Validator | None] = {}

    def err(self, source, message):
        self.errors.append(f"{source}: {message}")

    # ---- loading ---------------------------------------------------------

    def load(self):
        for path in sorted(self.dir.glob("*.flux.yml")) + sorted(self.dir.glob("*.flux.yaml")):
            docs, load_error = load_yaml_docs(path)
            if load_error:
                self.err(path.name, load_error)
                continue
            for doc in docs:
                if not isinstance(doc, dict):
                    self.err(path.name, f"document is not a mapping (got {type(doc).__name__})")
                    continue
                schema_errors = sorted(self.flux_validator.iter_errors(doc), key=lambda e: e.json_path)
                for e in schema_errors:
                    self.err(path.name, f"schema: {e.json_path}: {e.message}")
                if schema_errors:
                    continue  # cross-checks assume schema-valid shapes
                doc_id = doc["id"]
                if doc_id in self.flux_docs:
                    self.err(path.name, f"duplicate document id '{doc_id}'")
                else:
                    self.flux_docs[doc_id] = doc
        for path in sorted(self.dir.glob("*.fluid.yml")) + sorted(self.dir.glob("*.fluid.yaml")):
            docs, load_error = load_yaml_docs(path)
            if load_error:
                self.err(path.name, load_error)
                continue
            for doc in docs:
                if not isinstance(doc, dict):
                    self.err(path.name, f"document is not a mapping (got {type(doc).__name__})")
                    continue
                doc_id = doc.get("id")
                if not isinstance(doc_id, str):
                    self.err(path.name, "FLUID document has no string 'id'")
                    continue
                if doc_id in self.fluid_docs:
                    self.err(path.name, f"duplicate FLUID document id '{doc_id}'")
                elif doc_id in self.flux_docs:
                    self.err(path.name, f"FLUID document id '{doc_id}' collides with a FLUX document id")
                else:
                    self.fluid_docs[doc_id] = (path, doc)

    def _fluid_validator(self, version):
        if version not in self._fluid_validators:
            schema_file = FLUID_VENDOR_DIR / f"fluid-schema-{version}.json"
            self._fluid_validators[version] = (
                Draft202012Validator(json.loads(schema_file.read_text()), format_checker=FormatChecker())
                if schema_file.exists()
                else None
            )
        return self._fluid_validators[version]

    # ---- cross-document checks -------------------------------------------

    def check_refs(self):
        for doc_id, doc in self.flux_docs.items():
            refs: list[tuple[str, str, str]] = []
            walk_refs(doc.get("spec", {}), "spec", refs)
            for field, value, path in refs:
                expected_kind = REF_KINDS[field]
                name = value.split("@", 1)[0]  # RFC-03: strip a semver range
                target = self.flux_docs.get(name)
                if target is None:
                    self.err(doc_id, f"dangling reference {path} -> '{name}' (no such document in bundle)")
                elif expected_kind and target.get("kind") != expected_kind:
                    self.err(doc_id, f"{path} -> '{name}' resolves to kind {target.get('kind')}, expected {expected_kind}")

    def check_sums_and_sets(self):
        for doc_id, doc in self.flux_docs.items():
            kind, spec = doc.get("kind"), doc.get("spec", {})
            if kind == "World":
                mix = spec.get("population", {}).get("lifecycleMix", {})
                if mix and abs(sum(mix.values()) - 1.0) > SUM_TOLERANCE:
                    self.err(doc_id, f"population.lifecycleMix sums to {sum(mix.values())}, expected 1.0")
            elif kind == "Experiment":
                variants = spec.get("variants", [])
                weights = [v.get("weight", 0) for v in variants]
                if variants and abs(sum(weights) - 1.0) > SUM_TOLERANCE:
                    self.err(doc_id, f"variant weights sum to {sum(weights)}, expected 1.0")
                names = [v.get("name") for v in variants]
                if len(names) != len(set(names)):
                    self.err(doc_id, "variant names are not unique")
            elif kind == "JourneyTaxonomy":
                states = set(spec.get("states", []))
                for i, t in enumerate(spec.get("transitions", [])):
                    for end in ("from", "to"):
                        if t.get(end) not in states:
                            self.err(doc_id, f"transitions[{i}].{end} '{t.get(end)}' is not a declared state")
            elif kind == "Blueprint":
                nodes = set(spec.get("topology", {}).get("nodes", []))
                for i, e in enumerate(spec.get("topology", {}).get("edges", [])):
                    for end in ("from", "to"):
                        if e.get(end) not in nodes:
                            self.err(doc_id, f"topology.edges[{i}].{end} '{e.get(end)}' is not a declared node")
            elif kind == "Journey":
                reactions = spec.get("personaReactions", {})
                for persona_id in reactions:
                    target = self.flux_docs.get(persona_id)
                    if target is None or target.get("kind") != "Persona":
                        self.err(doc_id, f"personaReactions key '{persona_id}' does not resolve to a Persona")
                tax_ref = spec.get("taxonomyRef")
                if tax_ref:
                    tax = self.flux_docs.get(tax_ref, {})
                    states = set(tax.get("spec", {}).get("states", []))
                    for persona_id, reaction in reactions.items():
                        nxt = reaction.get("nextState")
                        if nxt and states and nxt not in states:
                            self.err(doc_id, f"personaReactions['{persona_id}'].nextState '{nxt}' is not a state of {tax_ref}")
            elif kind == "Simulation":
                tb = spec.get("timeBounds")
                if tb and tb.get("start", "") >= tb.get("end", ""):
                    self.err(doc_id, f"timeBounds.start {tb.get('start')} must precede end {tb.get('end')}")

    def check_traits(self):
        """RFC-01: categorical levels sum to 1; bounded normal has min < max."""
        for doc_id, doc in self.flux_docs.items():
            if doc.get("kind") != "Persona":
                continue
            for name, trait in (doc.get("spec", {}).get("traits", {}) or {}).items():
                if not isinstance(trait, dict):
                    continue
                if trait.get("dist") == "categorical":
                    total = sum(v for v in trait.get("levels", {}).values() if isinstance(v, (int, float)))
                    if abs(total - 1.0) > SUM_TOLERANCE:
                        self.err(doc_id, f"traits.{name}.levels sums to {total}, expected 1.0")
                elif trait.get("dist") == "normal":
                    lo, hi = trait.get("min"), trait.get("max")
                    if lo is not None and hi is not None and lo >= hi:
                        self.err(doc_id, f"traits.{name}: min {lo} must be below max {hi}")

    def _versioned_refs(self):
        """Yield (doc_id, path, name, range) for every RFC-03 versioned ref."""
        for doc_id, doc in self.flux_docs.items():
            spec = doc.get("spec", {})
            for field in ("moduleRefs", "modules"):
                for i, value in enumerate(spec.get(field, []) or []):
                    if isinstance(value, str) and "@" in value:
                        name, rng = value.split("@", 1)
                        yield doc_id, f"spec.{field}[{i}]", name, rng

    def check_supply_chain(self):
        """RFC-03: every versioned ref resolves through flux.lock to an exact
        version and a content digest of the in-bundle document."""
        versioned = list(self._versioned_refs())
        lock_path = self.dir / "flux.lock"
        if not versioned:
            return
        if not lock_path.exists():
            self.err("flux.lock", "versioned refs are used but the bundle has no flux.lock")
            return
        lock_docs, load_error = load_yaml_docs(lock_path)
        lock = lock_docs[0] if lock_docs and isinstance(lock_docs[0], dict) else None
        if load_error or lock is None:
            self.err("flux.lock", load_error or "lockfile is not a mapping")
            return
        entries = lock.get("modules", {}) if isinstance(lock.get("modules"), dict) else {}
        for doc_id, path, name, rng in versioned:
            entry = entries.get(name)
            if not isinstance(entry, dict):
                self.err(doc_id, f"{path}: '{name}@{rng}' has no flux.lock entry")
                continue
            pinned = entry.get("version")
            if not isinstance(pinned, str) or not semver_satisfies(pinned, rng):
                self.err(doc_id, f"{path}: locked version {pinned} does not satisfy range '{rng}'")
            target = self.flux_docs.get(name)
            if target is None:
                continue  # dangling ref already reported by check_refs
            declared = target.get("version")
            if declared != pinned:
                self.err(doc_id, f"{path}: {name} declares version {declared} but flux.lock pins {pinned}")
            digest = entry.get("digest")
            actual = "sha256:" + canonical_digest(target)
            if digest != actual:
                self.err(doc_id, f"{path}: {name} content digest mismatch — flux.lock has {digest}, bundle has {actual}")

    def check_semantics(self):
        """RFC-07: every semanticRef resolves to a measure declared by a
        Module binding the ossie-model port."""
        measures = set()
        bound = False
        for doc in self.flux_docs.values():
            if doc.get("kind") != "Module":
                continue
            for bind in doc.get("spec", {}).get("binds", []) or []:
                if bind.get("port") == "ossie-model":
                    bound = True
                    for m in (bind.get("config", {}) or {}).get("measures", []) or []:
                        if isinstance(m, str):
                            measures.add(m)
                        elif isinstance(m, dict) and isinstance(m.get("name"), str):
                            measures.add(m["name"])
        for doc_id, doc in self.flux_docs.items():
            if doc.get("kind") != "Experiment":
                continue
            for i, metric in enumerate(doc.get("spec", {}).get("metrics", []) or []):
                sref = metric.get("semanticRef")
                if not sref:
                    continue
                measure = sref.split("/", 1)[1]
                if not bound:
                    self.err(doc_id, f"spec.metrics[{i}].semanticRef '{sref}' used but no Module binds the ossie-model port")
                elif measure not in measures:
                    self.err(doc_id, f"spec.metrics[{i}].semanticRef '{sref}': measure '{measure}' is not declared by the bound semantic model (has: {sorted(measures)})")

    def check_agent_policy(self):
        for doc_id, doc in self.flux_docs.items():
            policy, skills = doc.get("agentPolicy"), doc.get("skills")
            if not skills or not isinstance(policy, dict):
                continue  # schema already enforces skills => agentPolicy
            allowed = set(policy.get("allowedModels", []))
            budget = policy.get("tokenBudget", 0)
            for i, skill in enumerate(skills):
                model = skill.get("model")
                if model and model not in allowed:
                    self.err(doc_id, f"skills[{i}].model '{model}' is not in agentPolicy.allowedModels")
                sb = skill.get("tokenBudget")
                if sb and sb > budget:
                    self.err(doc_id, f"skills[{i}].tokenBudget {sb} exceeds agentPolicy.tokenBudget {budget}")

    def check_fluid_documents(self):
        """Every .fluid.yml in the bundle must be valid FLUID — referenced or not."""
        for doc_id, (path, doc) in self.fluid_docs.items():
            declared = doc.get("fluidVersion")
            validator = self._fluid_validator(declared) if isinstance(declared, str) else None
            if validator is None:
                self.err(path.name, f"declared fluidVersion '{declared}' has no vendored schema in vendor/fluid/")
                continue
            for e in sorted(validator.iter_errors(doc), key=lambda e: e.json_path)[:10]:
                self.err(path.name, f"fails FLUID {declared}: {e.json_path}: {e.message}")

    def _vendored_fluid_versions(self):
        return sorted(p.stem.replace("fluid-schema-", "") for p in FLUID_VENDOR_DIR.glob("fluid-schema-*.json"))

    def check_fluid_seam(self):
        for doc_id, doc in self.flux_docs.items():
            if doc.get("kind") != "Simulation":
                continue
            gating_profiles = self._gating_consent_profiles(doc)
            for i, emit in enumerate(doc.get("spec", {}).get("emits", [])):
                where = f"spec.emits[{i}]"
                product_ref, expose_id = emit.get("productRef"), emit.get("exposeId")
                fluid_version = emit.get("fluidVersion")
                entry = self.fluid_docs.get(product_ref)
                if entry is None:
                    self.err(doc_id, f"{where}.productRef '{product_ref}' does not resolve to a .fluid.yml document in the bundle")
                    continue
                fluid_path, fluid_doc = entry
                declared = fluid_doc.get("fluidVersion")
                if isinstance(fluid_version, str) and fluid_version[:1] in "^~":
                    # RFC-06: a semver range, resolved against the vendored set
                    rng = fluid_version
                    if not any(semver_satisfies(v, rng) for v in self._vendored_fluid_versions()):
                        self.err(doc_id, f"{where}: no vendored FLUID version satisfies range '{rng}' (vendored: {self._vendored_fluid_versions()})")
                    if not (isinstance(declared, str) and semver_satisfies(declared, rng)):
                        self.err(doc_id, f"{where}: {fluid_path.name} declares fluidVersion {declared}, which does not satisfy the pinned range '{rng}'")
                elif declared != fluid_version:
                    self.err(doc_id, f"{where}: pinned fluidVersion {fluid_version} but {fluid_path.name} declares {declared}")
                # RFC-06: provenance — the proven contract bytes are the shipped bytes
                provenance = emit.get("provenance") or {}
                contract_digest = provenance.get("contractDigest")
                if contract_digest:
                    actual = "sha256:" + canonical_digest(fluid_doc)
                    if contract_digest != actual:
                        self.err(doc_id, f"{where}: provenance.contractDigest mismatch — declared {contract_digest}, bundle contract is {actual}")
                # RFC-05: seam gate on the twin's own credibility
                min_credibility = emit.get("minCredibility")
                if min_credibility is not None:
                    self._check_seam_credibility(doc_id, where, doc, min_credibility)
                exposes = [e for e in fluid_doc.get("exposes", []) if isinstance(e, dict)]
                expose = next((e for e in exposes if e.get("exposeId") == expose_id), None)
                if expose is None:
                    self.err(doc_id, f"{where}.exposeId '{expose_id}' is not an expose of {product_ref} (has: {[e.get('exposeId') for e in exposes]})")
                    continue
                self._check_consent_strictness(doc_id, where, expose, gating_profiles)

    def _check_seam_credibility(self, doc_id, where, sim_doc, min_credibility):
        """RFC-05: a Playback scorecard calibrating this Simulation's world must
        report at least the required credibility."""
        world_ref = sim_doc.get("spec", {}).get("worldRef", "").split("@", 1)[0]
        scores = [
            p.get("spec", {}).get("scorecard", {}).get("credibility")
            for p in self.flux_docs.values()
            if p.get("kind") == "Playback"
            and p.get("spec", {}).get("worldRef") == world_ref
            and isinstance(p.get("spec", {}).get("scorecard"), dict)
        ]
        scores = [c for c in scores if isinstance(c, (int, float))]
        if not scores:
            self.err(doc_id, f"{where}: minCredibility {min_credibility} set but no Playback scorecard calibrates world '{world_ref}'")
        elif max(scores) < min_credibility:
            self.err(doc_id, f"{where}: twin credibility {max(scores)} is below the required minCredibility {min_credibility}")

    def _gating_consent_profiles(self, sim_doc):
        """ConsentProfiles gating this Simulation via campaignRefs -> Campaign.consentRef."""
        profiles = []
        for campaign_ref in sim_doc.get("spec", {}).get("campaignRefs", []) or []:
            campaign = self.flux_docs.get(campaign_ref)
            if not campaign or campaign.get("kind") != "Campaign":
                continue
            profile = self.flux_docs.get(campaign.get("spec", {}).get("consentRef", ""))
            if profile and profile.get("kind") == "ConsentProfile":
                profiles.append(profile)
        return profiles

    def _check_consent_strictness(self, doc_id, where, expose, profiles):
        """The emitted contract must be at least as strict as every gating ConsentProfile."""
        policy = expose.get("policy", {}).get("agentPolicy", {}) if isinstance(expose.get("policy"), dict) else {}
        contract_allowed = set(policy.get("allowedUseCases", []) or [])
        for profile in profiles:
            pid, pspec = profile.get("id"), profile.get("spec", {})
            denied = set(pspec.get("deniedUseCases", []) or [])
            violation = contract_allowed & denied
            if violation:
                self.err(doc_id, f"{where}: contract allows use cases denied by ConsentProfile {pid}: {sorted(violation)}")
            profile_allowed = set(pspec.get("allowedUseCases", []) or [])
            if profile_allowed and contract_allowed and not contract_allowed <= profile_allowed:
                extra = contract_allowed - profile_allowed
                self.err(doc_id, f"{where}: contract allows use cases outside ConsentProfile {pid}'s allow-list: {sorted(extra)}")

    def run(self) -> list[str]:
        if not self.dir.is_dir():
            self.err(str(self.dir), "bundle directory does not exist or is not a directory")
            return self.errors
        self.load()
        if not self.flux_docs and not self.errors:
            self.err(str(self.dir), "bundle contains no .flux.yml documents")
        self.check_refs()
        self.check_sums_and_sets()
        self.check_traits()
        self.check_supply_chain()
        self.check_semantics()
        self.check_agent_policy()
        self.check_fluid_documents()
        self.check_fluid_seam()
        return self.errors


def main(argv: list[str]) -> int:
    flux_schema = json.loads(FLUX_SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(flux_schema)
    flux_validator = Draft202012Validator(flux_schema, format_checker=FormatChecker())

    if argv:
        bundle_dirs = [Path(a) for a in argv]
    else:
        bundle_dirs = sorted(p for p in (REPO / "examples").iterdir() if p.is_dir())

    failed = False
    for directory in bundle_dirs:
        errors = Bundle(directory, flux_validator).run()
        status = "FAIL" if errors else "ok"
        print(f"[{status}] {directory.relative_to(REPO) if directory.is_relative_to(REPO) else directory}")
        for e in errors:
            print(f"       {e}")
        failed = failed or bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
