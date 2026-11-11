# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``EVIDENCE.md`` and the ``claims[]`` that back it.

Five fixed sections, always in this order, always all five present --
"fixed" so a reader who has seen one evidence pack knows exactly where to
look in the next one:

1. **What This Bundle Is** -- identity: bundle name, kernel version, the
   run digest, when it was generated.
2. **Conformance** -- did the bundle obey the FLUX schema and its
   cross-document rules.
3. **Reproducibility** -- did the same bundle and seed produce the same
   bytes.
4. **Fidelity** -- what the fidelity check (SEAM-FIDELITY-SPEC) found, if
   anything was checked.
5. **What Was Not Proven** -- **mandatory**, generated from a fixed
   template (never hand-edited, the same discipline SEAM-FIDELITY-SPEC §6
   applies to a fidelity record's own ``doesNotCertify`` list): in v0.1
   this always states that no validity claim is being made and names
   ``temperature.campaignResponse`` as an authored dial -- a value the
   bundle's author declared, not one this engine measured or fitted.

:func:`build_claims` assembles the ``claims[]`` a customer-facing record
carries alongside the prose: one :class:`~flux_engine.api.claims.Claim`
each for conformance and reproducibility (basis
:attr:`~flux_engine.api.claims.ClaimBasis.NONE` -- neither is a claim
"measured against the world"), the fidelity claims a caller already built
elsewhere, passed through unchanged, and a validity claim **only** when a
*promoted* :class:`~flux_engine.api.accuracy.AccuracyVerdict` is supplied
(see :func:`flux_engine.accuracy.policy.promote`) -- never fabricated to
fill the fourth slot.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from flux_engine._vocabulary import BANNED_WORDS
from flux_engine._vocabulary import find_banned_words as _find_banned_words
from flux_engine.api.accuracy import AccuracyVerdict
from flux_engine.api.claims import Claim, ClaimBasis, ClaimType
from flux_engine.api.digests import DigestRef
from flux_engine.api.verdict import Reproducibility, Validity
from flux_engine.core.errors import FluxError, register

__all__ = [
    "DEFAULT_AUTHORED_DIALS",
    "EVIDENCE_SECTIONS",
    "FLX_8030",
    "build_claims",
    "find_banned_words",
    "render_evidence_report",
    "require_no_banned_words",
]

FLX_8030 = register(
    "FLX-8030",
    "generated evidence prose contains a reserved word",
    "EVIDENCE.md is rendered from a fixed template, but a caller-supplied "
    "value (a bundle name, a conformance summary) can still introduce a "
    "word this project reserves (the list is in flux_engine/_vocabulary.py). "
    "The report is refused rather than written with the word inside it.",
)

#: The five sections of ``EVIDENCE.md``, in the fixed order they always
#: appear. A reader who has seen one evidence pack already knows where to
#: look in the next one.
EVIDENCE_SECTIONS: Tuple[str, ...] = (
    "What This Bundle Is",
    "Conformance",
    "Reproducibility",
    "Fidelity",
    "What Was Not Proven",
)

#: v0.1 names exactly one authored dial in the mandatory "What Was Not
#: Proven" section -- the one the design names. A later
#: increment with a real calibration loop grows this list; nothing here
#: shrinks it silently.
DEFAULT_AUTHORED_DIALS: Tuple[str, ...] = ("temperature.campaignResponse",)

#: The reserved-word list. Re-exported from
#: :mod:`flux_engine._vocabulary`, which is the single definition shared
#: with ``scripts/register_lint.py``: the list used to be hand-copied into
#: both gates plus three test modules, with nothing asserting they were
#: equal, so a word added to one left the others blind to it.
_BANNED_WORDS: Tuple[str, ...] = BANNED_WORDS


def find_banned_words(text: str) -> Tuple[str, ...]:
    """Return every reserved word found in *text*, lower-cased, sorted, deduplicated.

    Thin wrapper over :func:`flux_engine._vocabulary.find_banned_words`,
    kept because this module is where the runtime refusal lives: unlike
    ``README.md`` or ``docs/``, ``EVIDENCE.md`` has no file on disk for a
    static scan to find until a bundle produces one, so the check has to
    run in-process, on the rendered string, before it is written.
    Deliberately does *not* mask dotted module paths -- generated
    customer-facing prose has no business naming an internal module.
    """
    return _find_banned_words(text)


def require_no_banned_words(text: str, *, label: str = "EVIDENCE.md") -> None:
    """Raise ``FLX-8030`` when *text* contains a reserved word."""
    hits = find_banned_words(text)
    if hits:
        raise FluxError(FLX_8030, f"{label} contains reserved vocabulary: {', '.join(hits)}")


def _render_not_proven_section(
    validity: Validity,
    authored_dials: Sequence[str],
) -> str:
    """Render the mandatory fifth section from its fixed template.

    Never hand-edited per call site: the *content* is fixed except for the
    dial list, which is data (the set of dials this bundle declares rather
    than fits), not prose a caller chooses.
    """
    dial_list = ", ".join(f"`{dial}`" for dial in authored_dials) if authored_dials else "none"
    lines = [
        "This bundle asserts no privacy or re-identification claim about any "
        "source data, no fairness claim about any source system, and no claim "
        "about any system trained or tested using its output.",
    ]
    if validity is Validity.NOT_ASSERTED:
        lines.append(
            # "validity: not asserted" is the exact half of the headline
            # `fluxsim verify` prints, so a reader can reproduce this line
            # with a command rather than reading the source. The symbol it
            # comes from is flux_engine.api.verdict.Validity.NOT_ASSERTED;
            # that name belongs in this comment, not in a document handed
            # to someone with no access to the source tree.
            "No validity claim is made (validity: not asserted): this run's "
            "predicted outcomes have not been measured against the world. "
            f"In particular, the following are authored dials -- values this "
            f"bundle's author declared, not values this engine fitted or measured: "
            f"{dial_list}."
        )
    elif validity is Validity.SUPPORTED:
        lines.append(
            "A validity claim is made for the outcome named in claims[] below, on "
            "the basis recorded there. Every other predicted outcome this bundle "
            "declares -- authored dials among them -- remains unmeasured against "
            f"the world: {dial_list}."
        )
    else:
        lines.append(
            "A validity claim was evaluated and did not hold for the outcome "
            f"named in claims[] below. Authored dials remain unmeasured against "
            f"the world regardless: {dial_list}."
        )
    return "\n\n".join(lines)


def render_evidence_report(
    *,
    bundle_name: str,
    kernel_version: str,
    run_digest: DigestRef,
    generated_at: str,
    conformance_summary: str,
    reproducibility: Reproducibility,
    fidelity_summary: str = "No fidelity check was run for this bundle.",
    validity: Validity = Validity.NOT_ASSERTED,
    authored_dials: Sequence[str] = DEFAULT_AUTHORED_DIALS,
) -> str:
    """Render ``EVIDENCE.md``'s five fixed sections for one bundle.

    Raises ``FLX-8030`` if the rendered text contains a reserved
    word -- most likely introduced through a caller-supplied
    string such as *bundle_name* or *conformance_summary*.
    """
    sections = [
        f"# Evidence: {bundle_name}",
        "## What This Bundle Is\n\n"
        f"- Bundle: `{bundle_name}`\n"
        f"- Kernel version: `{kernel_version}`\n"
        f"- Run digest: `{run_digest.value}`\n"
        f"- Generated at: {generated_at}",
        f"## Conformance\n\n{conformance_summary}",
        f"## Reproducibility\n\nreproducibility: {reproducibility.value}",
        f"## Fidelity\n\n{fidelity_summary}",
        f"## What Was Not Proven\n\n{_render_not_proven_section(validity, authored_dials)}",
    ]
    report = "\n\n".join(sections) + "\n"
    require_no_banned_words(report)
    return report


def build_claims(
    *,
    conformance_statement: str,
    conformance_evidence: Sequence[DigestRef],
    reproducibility: Reproducibility,
    reproducibility_evidence: Sequence[DigestRef] = (),
    verifier: str = "flux-engine",
    fidelity_claims: Sequence[Claim] = (),
    promoted_verdict: Optional[AccuracyVerdict] = None,
) -> Tuple[Claim, ...]:
    """Assemble the ``claims[]`` an evidence pack carries alongside ``EVIDENCE.md``.

    A validity claim is appended only when *promoted_verdict* is given --
    see :func:`flux_engine.accuracy.policy.promote`, which decides whether
    a verdict is allowed to reach here at all. Nothing here promotes a
    verdict itself; this function only renders one that already was.
    """
    claims = [
        Claim(
            claim_type=ClaimType.CONFORMANCE,
            statement=conformance_statement,
            basis=ClaimBasis.NONE,
            evidence=tuple(conformance_evidence),
            verifier=verifier,
        ),
        Claim(
            claim_type=ClaimType.REPRODUCIBILITY,
            statement=f"reproducibility: {reproducibility.value}",
            basis=ClaimBasis.NONE,
            evidence=tuple(reproducibility_evidence),
            verifier=verifier,
        ),
    ]
    claims.extend(fidelity_claims)
    if promoted_verdict is not None:
        claims.append(
            Claim(
                claim_type=ClaimType.VALIDITY,
                statement=(
                    f"metric {promoted_verdict.metric_id} outcome "
                    f"{promoted_verdict.outcome.value}"
                ),
                basis=promoted_verdict.basis,
                evidence=promoted_verdict.evidence,
                verifier=verifier,
            )
        )
    return tuple(claims)
