# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The profile a Simulation pins, checked against the profile a command is given.

A Simulation pins a profile document (docs/spec/PROFILE-SPEC.md) by its
digest, under the namespace's top-level ``profile`` key
(:func:`flux_engine.seam.resolve.profile_reference`). The document itself
is not a bundle file: a bundle directory holds the files ADR 0006 lists and
no others, so the profile is handed to the command that needs it, as a
committed slice is. The pin is in the Simulation, so ``inputDigest`` covers
it, and a run is bound to the profile's exact content through it, as an
emit is bound to its contract through ``provenance.contractDigest``.

The digest is ``jcs-document``: sha256 over the RFC 8785 canonical form of
the parsed document (KERNEL-SPEC §2), so re-indenting a profile or
reordering its keys keeps its digest, and changing any value moves it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.seam.overlay import Overlay, overlaid_profile
from flux_stats.profile import Profile, load_profile_text

__all__ = [
    "FLX_3109",
    "FLX_3110",
    "PinnedProfile",
    "load_profile",
    "require_profile",
]

FLX_3109 = register(
    "FLX-3109",
    "the profile the Simulation pins and the profile supplied do not pair",
    "A Simulation that pins a profile under extensions['com.agentics.flux/1']"
    ".profile needs that profile handed to plan with --profile, and a run "
    "planned from it needs it handed to ship, and to verify for its scorecard; "
    "a profile handed to a Simulation or a run that pins none would be read by "
    "nothing. "
    "The message says which side is missing. Pass the profile file, or pin "
    "its digest in the Simulation, or drop the one that is not wanted. "
    "Nothing was written.",
)

FLX_3110 = register(
    "FLX-3110",
    "the supplied profile is not the one the Simulation pins",
    "The digest of the profile handed to plan, verify or ship (jcs-document: sha256 over its "
    "RFC 8785 canonical form) differs from the digest the Simulation pins. "
    "Either the profile changed after it was pinned, or it is a different "
    "profile. Pin the new digest in the Simulation if the change is "
    "intended, or pass the profile that was pinned. Nothing was written.",
)


@dataclass(frozen=True)
class PinnedProfile:
    """A profile read from disk, with the digest its pin is checked against.

    ``document`` is the parsed JSON the digest was taken over, for a reader that
    addresses the profile by JSON Pointer: a parameter register's citations
    (:func:`flux_engine.seam.parameters.check_profile_citations`), and the privacy
    grade (ADR 0020), which never reads an overlay. ``profile`` is the profile a run is
    scored against: the pinned profile as read, or, after :meth:`with_overlay`, the profile
    the Simulation's overlay makes of it (ADR 0030 section 7). ``digest`` is always the pinned
    profile's, which the Simulation's pin names.
    """

    profile: Profile
    digest: DigestRef
    path: Path
    document: Mapping[str, Any] = field(default_factory=dict)
    #: The overlay ``profile`` was made with, or ``None`` when it is the pinned profile as read.
    overlay: Optional[Overlay] = None

    def with_overlay(self, overlay: Overlay, *, code: ErrorCode) -> "PinnedProfile":
        """This profile scored with *overlay*: ``profile`` is ``MergePatch(profile, patch)``.

        The pinned ``document`` and ``digest`` are kept: the pin and the grade read the pinned
        profile, and a number cited as measured is checked against it.
        """
        scored, _ = overlaid_profile(self.document, overlay, code=code)
        return replace(self, profile=scored, overlay=overlay)


def load_profile(path: Path) -> PinnedProfile:
    """Read and check the profile at *path*, and digest it.

    A file that cannot be read is ``FLX-3109``; a document that is not a
    profile is ``FLX-5036``, from the profile reader.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) else "it is not UTF-8"
        raise FluxError(FLX_3109, f"the profile at {path} could not be read: {reason}") from exc
    profile, document = load_profile_text(text)
    digest = digest_of_document(document, subject=f"profile:{Path(path).name}")
    return PinnedProfile(profile=profile, digest=digest, path=Path(path), document=document)


def require_profile(
    pinned: Optional[str], supplied: Optional[Path], *, where: str
) -> Optional[PinnedProfile]:
    """Pair a Simulation's pin with the profile a command was given, or refuse.

    No pin and no profile is the common case and returns ``None``. A pin
    with no profile, or a profile with no pin, is ``FLX-3109``; a profile
    whose digest is not the pinned one is ``FLX-3110``.
    """
    if pinned is None and supplied is None:
        return None
    if pinned is None:
        raise FluxError(
            FLX_3109,
            f"{where}: a profile was supplied, but the Simulation pins none under "
            "extensions['com.agentics.flux/1'].profile",
        )
    if supplied is None:
        raise FluxError(
            FLX_3109,
            f"{where}: the Simulation pins profile {pinned}; pass that profile with --profile",
        )
    loaded = load_profile(supplied)
    if loaded.digest.value != pinned:
        raise FluxError(
            FLX_3110,
            f"{where}: the Simulation pins profile {pinned}, and {Path(supplied).name} is "
            f"{loaded.digest.value}",
        )
    return loaded
