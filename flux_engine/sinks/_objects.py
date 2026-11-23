# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Object store targets for the file sinks: where an object lands, and the upload.

``parquet:s3://bucket/some/prefix`` and ``arrow-ipc:gs://bucket/some/prefix`` write the
files a local target writes, under the same relative names, as objects. The URL's
host is the bucket (or container) and its path is the prefix.

**The prefix is split on ``/`` and every segment is checked** before anything is
written: an empty segment, ``.``, ``..``, a backslash and a percent-encoded ``/``,
``\\`` or ``.`` (``%2F``, ``%5C``, ``%2E``, in either case) are refused
(``FLX-3510``), so no spelling of a target can name a key outside the prefix it
shows. A trailing ``/`` is dropped. An object's key is the prefix, a ``/`` and the
relative name a local target would use, and is checked to start with the prefix and
a ``/`` before it is used.

**Credentials and endpoints are never passed on.** The store is built by
``obstore.store.from_url("<scheme>://<bucket>")`` with no credential, configuration
or client option, so obstore reads them from the person's environment. No credential
sink option reaches obstore.

**No metadata probe by default.** With no explicit credential in the environment
(:func:`explicit_credentials`), the store would fall back to the cloud's instance
metadata service (``169.254.169.254`` and its siblings), a network call the person
never asked for. Such a target is refused before the run (``FLX-3505``) unless the
sink option ``instance-identity=true`` says the credentials are the instance's or
the workload's own (:data:`INSTANCE_IDENTITY_OPTION`).

Importing this module needs nothing optional: obstore is imported only when an
upload is about to happen, through :func:`flux_engine.sinks._optional.require`.
"""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Iterable, Mapping, Optional, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.core.target_label import target_label
from flux_engine.seam.coverage import printable
from flux_engine.sinks._optional import require
from flux_engine.sinks._options import bool_option
from flux_engine.sinks._secrets import scrub
from flux_engine.sinks.errors import FLX_3504, FLX_3505, FLX_3510

__all__ = [
    "INSTANCE_IDENTITY_OPTION",
    "OBJECT_SCHEMES",
    "ObjectLocation",
    "Upload",
    "explicit_credentials",
    "instance_identity_option",
    "is_object_target",
    "parse_object_target",
    "open_store",
    "refuse_unsupported_scheme",
    "require_explicit_credentials",
    "require_obstore",
    "require_plain_segment",
    "upload_files",
]

#: The URL schemes an object store target may use.
OBJECT_SCHEMES: Tuple[str, ...] = ("s3", "gs", "az", "abfs")

#: The ``--sink-opt`` that lets an object store use the instance's or the workload's own
#: credentials (an EC2 role, a pod identity, a managed identity, a GCE service account).
#: Not named like a credential, which the registry would refuse.
INSTANCE_IDENTITY_OPTION = "instance-identity"

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
#: A bucket or container name: letters, digits, ``.``, ``_`` and ``-``.
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: A percent-encoded ``/``, ``\`` or ``.``, in either case.
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c|2e)", re.IGNORECASE)


@dataclass(frozen=True)
class ObjectLocation:
    """A checked object store target: a scheme, a bucket and a normalized prefix."""

    scheme: str
    bucket: str
    prefix: str
    #: Whether the person opted in to the instance's or the workload's own credentials.
    instance_identity: bool = False

    @property
    def store_url(self) -> str:
        """What the store is built from: the scheme and the bucket, never the prefix."""
        return f"{self.scheme}://{self.bucket}"

    @property
    def url(self) -> str:
        """The target as the receipt records it: its label, so the key prefix stays whole."""
        return target_label(self.scheme, f"{self.store_url}/{self.prefix}")

    def key(self, relative: str) -> str:
        """The key of the object a local target would write at *relative*.

        ``FLX-3510`` unless the key starts with the prefix and a ``/``.
        """
        name = PurePosixPath(relative)
        if (
            name.as_posix() != relative
            or name.is_absolute()
            or not name.parts
            or ".." in name.parts
            or "\\" in relative
        ):
            raise FluxError(
                FLX_3510, f"{relative!r} is not a relative name below {printable(self.url)}"
            )
        return require_key_in_prefix(f"{self.prefix}/{relative}", self.prefix)


def require_key_in_prefix(key: str, prefix: str) -> str:
    """Return *key* when it starts with *prefix* and a ``/``; ``FLX-3510`` otherwise."""
    if not key.startswith(f"{prefix}/"):
        raise FluxError(FLX_3510, f"the object key {key!r} is not below the prefix {prefix!r}")
    return key


def is_object_target(target: str) -> bool:
    """Whether *target* is a URL of one of :data:`OBJECT_SCHEMES`."""
    match = _SCHEME.match(target)
    return match is not None and match.group(1).lower() in OBJECT_SCHEMES


def refuse_unsupported_scheme(sink: str, target: str) -> None:
    """``FLX-3504`` when *target* is a ``scheme://`` URL that is not an object store URL.

    A local directory is written as a plain path, so any other URL (``file://``,
    ``memory://``, ``ftp://``) is refused before anything is generated or created.
    """
    if _SCHEME.match(target) and not is_object_target(target):
        raise FluxError(
            FLX_3504,
            f"{target_label(sink, target)!r} is not a target the {sink} sink can write: a URL must "
            f"start with one of {', '.join(f'{scheme}://' for scheme in OBJECT_SCHEMES)}; "
            "to write a local directory, give it as a plain path, with no scheme",
        )


def parse_object_target(target: str, *, instance_identity: bool = False) -> ObjectLocation:
    """Check *target* and split it into scheme, bucket and prefix; ``FLX-3510`` if unusable."""
    match = _SCHEME.match(target)
    if match is None or match.group(1).lower() not in OBJECT_SCHEMES:
        raise FluxError(
            FLX_3510, f"{target_label('object store', target)!r} is not an object store URL"
        )
    scheme = match.group(1).lower()
    bucket, _, path = target[match.end() :].partition("/")
    shown = target_label(scheme, target)
    if _BUCKET.fullmatch(bucket) is None:
        raise FluxError(
            FLX_3510, f"{shown!r} names no usable bucket or container: {scheme}://BUCKET/PREFIX"
        )
    if path.endswith("/"):
        path = path[:-1]
    if "\\" in path:
        raise FluxError(FLX_3510, f"{shown!r} holds a backslash in its prefix")
    if "@" in path:
        # Read as user information, what comes before it is a credential a person put in the wrong
        # place: refuse it rather than write it into a key and a receipt.
        raise FluxError(
            FLX_3510,
            f"{shown!r} holds an at sign in its prefix, which reads as user information; "
            "credentials come from the environment, and a prefix is a name without one",
        )
    if _ENCODED_SEPARATOR.search(path):
        raise FluxError(FLX_3510, f"{shown!r} holds a percent-encoded separator in its prefix")
    segments = path.split("/")
    if segments == [""]:
        raise FluxError(FLX_3510, f"{shown!r} names no prefix: {scheme}://BUCKET/PREFIX")
    for segment in segments:
        if segment in ("", ".", ".."):
            what = "an empty segment" if not segment else f"the segment {segment!r}"
            raise FluxError(FLX_3510, f"{shown!r} holds {what} in its prefix")
    return ObjectLocation(
        scheme=scheme,
        bucket=bucket,
        prefix="/".join(segments),
        instance_identity=instance_identity,
    )


def require_plain_segment(segment: str, *, what: str, shown: str) -> str:
    """Return *segment* when it is one plain name; ``FLX-3510`` otherwise.

    The rules are an object prefix's, one segment at a time: not empty, not ``.`` or
    ``..``, no backslash and no percent-encoded ``/``, ``\\`` or ``.``. *what* names the
    segment ("the catalogue name") and *shown* the target it came from, already
    free of credentials.
    """
    if segment in ("", ".", ".."):
        problem = "is empty" if not segment else f"is the segment {segment!r}"
    elif "\\" in segment:
        problem = "holds a backslash"
    elif _ENCODED_SEPARATOR.search(segment):
        problem = "holds a percent-encoded separator"
    else:
        return segment
    raise FluxError(FLX_3510, f"{shown!r}: {what} {problem}")


def require_obstore() -> ModuleType:
    """obstore, or ``FLX-3501`` naming ``flux-sim-engine[publish]``."""
    return require("obstore", extra="publish", feature="an object store target")


@dataclass(frozen=True)
class Upload:
    """One file to upload: where it is, and the key it lands at."""

    path: Path
    key: str


#: The settings each scheme's store reads from the environment, as one remedy line.
_REMEDY = {
    "s3": (
        "set the AWS_* settings the store reads (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, "
        "AWS_REGION, and AWS_ENDPOINT_URL for another endpoint), and AWS_ALLOW_HTTP=true "
        "for an http:// endpoint"
    ),
    "gs": (
        "set the GOOGLE_* settings the store reads (GOOGLE_SERVICE_ACCOUNT, "
        "GOOGLE_SERVICE_ACCOUNT_KEY or GOOGLE_APPLICATION_CREDENTIALS)"
    ),
    "az": (
        "set the AZURE_* settings the store reads (AZURE_STORAGE_ACCOUNT_NAME and "
        "AZURE_STORAGE_ACCOUNT_KEY, AZURE_STORAGE_SAS_KEY or AZURE_STORAGE_TOKEN)"
    ),
}
_REMEDY["abfs"] = _REMEDY["az"]

#: A store failure that comes of the credentials or the endpoint, not of the data.
_SETTINGS_FAILURE = re.compile(
    r"credential|token|metadata|endpoint|HTTP error|builder error|must be specified|"
    r"unauthenticated|permission|forbidden|access ?denied|invalid ?(?:access|signature)|"
    r"\b40[13]\b|error sending request|dns|connect",
    re.IGNORECASE,
)


def instance_identity_option(options: Mapping[str, str]) -> bool:
    """The ``instance-identity`` option as true or false; false when absent."""
    return bool_option(options, INSTANCE_IDENTITY_OPTION, False)


def _set(environ: Mapping[str, str], *names: str) -> bool:
    """Whether any of the variables *names* is set to a value."""
    return any(environ.get(name, "").strip() for name in names)


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _on(environ: Mapping[str, str], *names: str) -> bool:
    """Whether any of the variables *names* is set to a true value."""
    return bool(_TRUE_VALUES & {environ.get(name, "").strip().lower() for name in names})


def _aws_explicit(env: Mapping[str, str], library: str) -> bool:
    return bool(
        (_set(env, "AWS_ACCESS_KEY_ID") and _set(env, "AWS_SECRET_ACCESS_KEY"))
        or (_set(env, "AWS_WEB_IDENTITY_TOKEN_FILE") and _set(env, "AWS_ROLE_ARN"))
        or _on(env, "AWS_SKIP_SIGNATURE")
        or (library == "deltalake" and _set(env, "AWS_PROFILE"))
    )


def _gcs_explicit(env: Mapping[str, str]) -> bool:
    return (
        _set(
            env,
            "GOOGLE_SERVICE_ACCOUNT",
            "GOOGLE_SERVICE_ACCOUNT_PATH",
            "SERVICE_ACCOUNT",
            "GOOGLE_SERVICE_ACCOUNT_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_BEARER_TOKEN",
        )
        or _on(env, "GOOGLE_SKIP_SIGNATURE")
        or _gcloud_default_credentials(env).is_file()
    )


def _azure_explicit(env: Mapping[str, str]) -> bool:
    if env.get("AZURE_CREDENTIAL_TYPE", "").strip().lower() == "managed_identity":
        return False
    client = _set(env, "AZURE_STORAGE_CLIENT_ID", "AZURE_CLIENT_ID")
    tenant = _set(
        env,
        "AZURE_STORAGE_TENANT_ID",
        "AZURE_STORAGE_AUTHORITY_ID",
        "AZURE_TENANT_ID",
        "AZURE_AUTHORITY_ID",
    )
    secret = _set(env, "AZURE_STORAGE_CLIENT_SECRET", "AZURE_CLIENT_SECRET")
    return (
        _set(
            env,
            "AZURE_STORAGE_ACCOUNT_KEY",
            "AZURE_STORAGE_ACCESS_KEY",
            "AZURE_STORAGE_MASTER_KEY",
            "AZURE_STORAGE_SAS_KEY",
            "AZURE_STORAGE_SAS_TOKEN",
            "AZURE_STORAGE_TOKEN",
        )
        or (client and secret and tenant)
        or (client and tenant and _set(env, "AZURE_FEDERATED_TOKEN_FILE"))
        or _on(env, "AZURE_USE_AZURE_CLI", "AZURE_SKIP_SIGNATURE", "AZURE_STORAGE_USE_EMULATOR")
    )


def explicit_credentials(
    scheme: str, environ: Optional[Mapping[str, str]] = None, *, library: str = "obstore"
) -> bool:
    """Whether the environment holds a credential the store reads without asking a metadata service.

    Only a credential the library reads counts, so a setting it ignores cannot stand in for
    one it needs. *library* is ``obstore`` (the file sinks) or ``deltalake`` (which also reads
    ``AWS_PROFILE``). Nothing is read from disk but the Google well-known application
    credentials file, and nothing is sent anywhere.

    * ``s3``: ``AWS_ACCESS_KEY_ID`` with ``AWS_SECRET_ACCESS_KEY``, or
      ``AWS_WEB_IDENTITY_TOKEN_FILE`` with ``AWS_ROLE_ARN``, or ``AWS_SKIP_SIGNATURE``.
    * ``gs``: a service account (``GOOGLE_SERVICE_ACCOUNT``, ``GOOGLE_SERVICE_ACCOUNT_PATH``,
      ``GOOGLE_SERVICE_ACCOUNT_KEY``), ``GOOGLE_APPLICATION_CREDENTIALS``,
      ``GOOGLE_BEARER_TOKEN``, ``GOOGLE_SKIP_SIGNATURE``, or the gcloud application default
      credentials file.
    * ``az`` and ``abfs``: an account key, a SAS, ``AZURE_STORAGE_TOKEN``, a client id with a
      secret and a tenant, a client id with a tenant and ``AZURE_FEDERATED_TOKEN_FILE``,
      ``AZURE_USE_AZURE_CLI``, ``AZURE_SKIP_SIGNATURE`` or ``AZURE_STORAGE_USE_EMULATOR``,
      unless ``AZURE_CREDENTIAL_TYPE`` forces ``managed_identity``.
    """
    env = os.environ if environ is None else environ
    if scheme == "s3":
        return _aws_explicit(env, library)
    if scheme == "gs":
        return _gcs_explicit(env)
    if scheme in ("az", "abfs"):
        return _azure_explicit(env)
    return True


def _gcloud_default_credentials(env: Mapping[str, str]) -> Path:
    """Where the Google object store reads the application default credentials from."""
    home = env.get("HOME", "").strip() or env.get("APPDATA", "").strip()
    return Path(home) / ".config" / "gcloud" / "application_default_credentials.json"


#: The opt-in's remedy, named the same for every scheme.
_INSTANCE_REMEDY = (
    "or, to use the credentials of the machine or workload this runs on (an instance role, "
    f"a pod or managed identity), pass --sink-opt {INSTANCE_IDENTITY_OPTION}=true, which lets "
    "the store ask the cloud's metadata service for them"
)


def require_explicit_credentials(location: ObjectLocation, *, library: str = "obstore") -> None:
    """``FLX-3505`` when *location* has no explicit credential and no opt-in.

    Called before the store is built and before the run, so the instance metadata
    address is never contacted for a person who did not ask for it. With
    ``instance-identity=true`` nothing is checked and the store behaves as it always did.
    """
    if location.instance_identity or explicit_credentials(location.scheme, library=library):
        return
    raise FluxError(
        FLX_3505,
        f"the object store {printable(location.url)} has no credentials set, and without them "
        "the store would ask the cloud's instance metadata service (an address such as "
        "169.254.169.254) for some; nothing was contacted. To fix the store's settings, "
        f"{_REMEDY[location.scheme]}; {_INSTANCE_REMEDY}",
    )


def _failure(what: str, exc: BaseException, location: ObjectLocation) -> FluxError:
    """``FLX-3505`` for a store failure: the first line scrubbed, and a remedy when settings are to blame.

    The remedy names variables, never their values.
    """
    known = [location.store_url, f"{location.store_url}/{location.prefix}"]
    detail = (scrub(str(exc), known, sink=location.scheme).strip().splitlines() or [""])[0][:300]
    kind = type(exc).__name__
    text = f"{what}: {kind}{': ' + printable(detail) if detail else ''}"
    if kind in ("UnauthenticatedError", "PermissionDeniedError") or _SETTINGS_FAILURE.search(
        f"{kind} {detail}"
    ):
        text += f". To fix the store's settings, {_REMEDY[location.scheme]}"
    return FluxError(FLX_3505, text)


def open_store(location: ObjectLocation) -> Any:
    """The obstore store for *location*, built from the scheme and bucket alone.

    Building reads the environment and makes no request, so a setting that is missing or
    malformed (an Azure target with no account) fails here, before any row is generated.
    A credential that is wrong only shows on the first request. ``FLX-3505`` on failure.
    """
    require_obstore()
    require_explicit_credentials(location)
    try:
        return importlib.import_module("obstore.store").from_url(location.store_url)
    except Exception as exc:  # noqa: BLE001
        raise _failure(
            f"the object store for {printable(location.url)} could not be set up", exc, location
        ) from None


def upload_files(location: ObjectLocation, uploads: Iterable[Upload]) -> str:
    """Upload each file in order; return the writer, ``obstore <version>``.

    The store is built from the scheme and the bucket alone, so obstore takes its
    credentials and endpoint from the environment. A failure is the engine's own
    ``FLX-3505``, its message scrubbed and the writer's exception not chained.
    """
    obstore = require_obstore()
    batch = tuple(uploads)
    for item in batch:
        require_key_in_prefix(item.key, location.prefix)
    store = open_store(location)
    try:
        for item in batch:
            obstore.put(store, item.key, item.path)
    except Exception as exc:  # noqa: BLE001
        raise _failure(f"uploading to {printable(location.url)} failed", exc, location) from None
    return f"obstore {obstore.__version__}"
