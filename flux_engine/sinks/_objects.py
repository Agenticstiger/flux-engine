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

**An ADLS Gen2 target** is also written ``abfs://CONTAINER@ACCOUNT.dfs.core.windows.net/PREFIX``
(``az://`` takes the same form): the part before the ``@`` is the container, the account is
the first label of the host, which must end in ``.dfs.core.windows.net``, and the path is the
prefix. The form is checked as strictly as a bucket name, so the ``@`` carries no credential.

**Credentials and endpoints are never passed on.** The store is built by
``obstore.store.from_url("<scheme>://<bucket>")`` with no credential or endpoint, so obstore
reads them from the person's environment. No credential sink option reaches obstore. The
only things the engine sets on the client are the timeouts and the retry budget a person
asks for with ``request-timeout``, ``connect-timeout``, ``max-retries`` and
``retry-timeout`` (:class:`ObjectTuning`); with none of them obstore's own defaults apply
(a 30 second request timeout, 10 retries over at most 3 minutes).

**A failed upload cleans up after itself.** The manifest is uploaded last and commits the
stream. When an upload fails, the data objects this delivery had written are deleted, best
effort and by their exact keys, unless the manifest of an earlier delivery of the same
stream is already in place (it names the same objects with the same bytes, so they are kept).
The error says what was deleted and which keys remain.

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
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

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
    "TUNING_OPTIONS",
    "ObjectLocation",
    "ObjectTuning",
    "Upload",
    "adls_gen2_target",
    "explicit_credentials",
    "instance_identity_option",
    "is_object_target",
    "parse_object_target",
    "open_store",
    "refuse_unsupported_scheme",
    "require_explicit_credentials",
    "require_obstore",
    "require_plain_segment",
    "tuning_option",
    "upload_files",
]

#: The URL schemes an object store target may use.
OBJECT_SCHEMES: Tuple[str, ...] = ("s3", "gs", "az", "abfs")

#: The ``--sink-opt`` that lets an object store use the instance's or the workload's own
#: credentials (an EC2 role, a pod identity, a managed identity, a GCE service account).
#: Not named like a credential, which the registry would refuse.
INSTANCE_IDENTITY_OPTION = "instance-identity"

#: The ``--sink-opt`` keys that tune the object store client, as the command line spells them.
TUNING_OPTIONS: Tuple[str, ...] = (
    "connect-timeout",
    "max-retries",
    "request-timeout",
    "retry-timeout",
)

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
#: The host of an ADLS Gen2 account: the account name, then ``.dfs.core.windows.net``.
_ADLS_HOST = re.compile(r"^([a-z0-9]{3,24})\.dfs\.core\.windows\.net$")
#: The schemes that take the ``CONTAINER@ACCOUNT.dfs.core.windows.net`` form.
_ADLS_SCHEMES = ("abfs", "az")
#: A bucket or container name: letters, digits, ``.``, ``_`` and ``-``.
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: A percent-encoded ``/``, ``\`` or ``.``, in either case.
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c|2e)", re.IGNORECASE)


@dataclass(frozen=True)
class ObjectTuning:
    """The client settings a person may change: whole seconds, and a retry count.

    ``None`` leaves obstore's own default (a 30 second request timeout, no connect timeout
    of its own, 10 retries within 3 minutes). ``retry_timeout`` bounds the whole time spent
    retrying one request, so it is what shortens the wait for an endpoint that never answers.
    """

    request_timeout: Optional[int] = None
    connect_timeout: Optional[int] = None
    max_retries: Optional[int] = None
    retry_timeout: Optional[int] = None

    def client_options(self) -> Dict[str, Any]:
        """obstore's ``client_options`` for these settings: only the ones that were set."""
        options: Dict[str, Any] = {}
        if self.request_timeout is not None:
            options["timeout"] = timedelta(seconds=self.request_timeout)
        if self.connect_timeout is not None:
            options["connect_timeout"] = timedelta(seconds=self.connect_timeout)
        return options

    def retry_config(self) -> Dict[str, Any]:
        """obstore's ``retry_config`` for these settings: only the ones that were set."""
        config: Dict[str, Any] = {}
        if self.max_retries is not None:
            config["max_retries"] = self.max_retries
        if self.retry_timeout is not None:
            config["retry_timeout"] = timedelta(seconds=self.retry_timeout)
        return config


#: No tuning: obstore's own defaults.
NO_TUNING = ObjectTuning()

#: The longest timeout, in seconds, and the most retries an option may ask for.
_MAX_SECONDS = 3600
_MAX_RETRIES = 100


def _whole_number(
    options: Mapping[str, str], key: str, low: int, high: int, what: str
) -> Optional[int]:
    raw = options.get(key)
    if raw is None:
        return None
    text = raw.strip()
    if not text.isascii() or not text.isdigit() or not low <= int(text) <= high:
        # The option's name and its range, never its value: a plain option is not scrubbed.
        raise FluxError(FLX_3504, f"{key} must be {what} from {low} to {high}")
    return int(text)


def tuning_option(options: Mapping[str, str]) -> ObjectTuning:
    """The :data:`TUNING_OPTIONS` of *options*; ``FLX-3504`` naming the option that is out of range."""
    return ObjectTuning(
        request_timeout=_whole_number(options, "request-timeout", 1, _MAX_SECONDS, "seconds"),
        connect_timeout=_whole_number(options, "connect-timeout", 1, _MAX_SECONDS, "seconds"),
        max_retries=_whole_number(options, "max-retries", 0, _MAX_RETRIES, "a number of retries"),
        retry_timeout=_whole_number(options, "retry-timeout", 1, _MAX_SECONDS, "seconds"),
    )


@dataclass(frozen=True)
class ObjectLocation:
    """A checked object store target: a scheme, a bucket and a normalized prefix."""

    scheme: str
    bucket: str
    prefix: str
    #: Whether the person opted in to the instance's or the workload's own credentials.
    instance_identity: bool = False
    #: The storage account, for the ADLS Gen2 form ``CONTAINER@ACCOUNT.dfs.core.windows.net``.
    account: Optional[str] = None
    #: The timeouts and retry budget the person asked for; empty leaves obstore's defaults.
    tuning: ObjectTuning = ObjectTuning()

    @property
    def store_url(self) -> str:
        """What the store is built from: the scheme and the bucket, never the prefix."""
        if self.account is not None:
            return f"{self.scheme}://{self.bucket}@{self.account}.dfs.core.windows.net"
        return f"{self.scheme}://{self.bucket}"

    @property
    def url(self) -> str:
        """The target as the receipt records it: its label, so the key prefix stays whole.

        The ADLS form is labelled like any other URL: the container before the ``@`` is user
        information to the label and is written ``…``.
        """
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


def adls_gen2_target(target: str) -> bool:
    """Whether *target* is the ADLS Gen2 form ``abfs://CONTAINER@ACCOUNT.dfs.core.windows.net[/...]``.

    Only that exact shape: a container name, an account host and a path with no query or
    fragment. Its ``@`` is not user information, so the credential check lets it through.
    """
    match = _SCHEME.match(target)
    if match is None or match.group(1).lower() not in _ADLS_SCHEMES:
        return False
    rest = target[match.end() :]
    if "?" in rest or "#" in rest:
        return False
    container, at, host = rest.partition("/")[0].partition("@")
    return (
        bool(at) and _BUCKET.fullmatch(container) is not None and _ADLS_HOST.match(host) is not None
    )


def _prefix_segments(path: str, shown: str, scheme: str) -> List[str]:
    """The segments of an object prefix, or ``FLX-3510`` when one is unusable."""
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
    return segments


def parse_object_target(
    target: str, *, instance_identity: bool = False, tuning: ObjectTuning = NO_TUNING
) -> ObjectLocation:
    """Check *target* and split it into scheme, bucket and prefix; ``FLX-3510`` if unusable."""
    match = _SCHEME.match(target)
    if match is None or match.group(1).lower() not in OBJECT_SCHEMES:
        raise FluxError(
            FLX_3510, f"{target_label('object store', target)!r} is not an object store URL"
        )
    scheme = match.group(1).lower()
    bucket, _, path = target[match.end() :].partition("/")
    shown = target_label(scheme, target)
    account: Optional[str] = None
    if adls_gen2_target(target):
        bucket, _, host = bucket.partition("@")
        account = _ADLS_HOST.match(host).group(1)  # type: ignore[union-attr]
    elif "@" in bucket or bucket.endswith(".dfs.core.windows.net"):
        raise FluxError(
            FLX_3510,
            f"{shown!r} names no usable container: for ADLS Gen2 write "
            f"{scheme}://CONTAINER@ACCOUNT.dfs.core.windows.net/PREFIX",
        )
    if _BUCKET.fullmatch(bucket) is None:
        raise FluxError(
            FLX_3510, f"{shown!r} names no usable bucket or container: {scheme}://BUCKET/PREFIX"
        )
    segments = _prefix_segments(path, shown, scheme)
    return ObjectLocation(
        scheme=scheme,
        bucket=bucket,
        prefix="/".join(segments),
        instance_identity=instance_identity,
        account=account,
        tuning=tuning,
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


#: A failure that comes of waiting on an endpoint that is slow or does not answer.
_SLOW_FAILURE = re.compile(r"timed? ?out|timeout|retries|deadline", re.IGNORECASE)

_WAIT_HINT = (
    "An endpoint that does not answer is retried for up to 3 minutes before this error; to give "
    "up sooner pass --sink-opt retry-timeout=SECONDS, and tune --sink-opt max-retries=N, "
    "request-timeout=SECONDS and connect-timeout=SECONDS"
)


def _failure(what: str, exc: BaseException, location: ObjectLocation) -> FluxError:
    """``FLX-3505`` for a store failure: the first line scrubbed, and a remedy when settings are to blame.

    The remedy names variables, never their values.
    """
    known = [location.store_url, f"{location.store_url}/{location.prefix}"]
    detail = (scrub(str(exc), known, sink=location.scheme).strip().splitlines() or [""])[0][:300]
    kind = type(exc).__name__
    text = f"{what}: {kind}{': ' + printable(detail) if detail else ''}"
    settings = kind in ("UnauthenticatedError", "PermissionDeniedError") or bool(
        _SETTINGS_FAILURE.search(f"{kind} {detail}")
    )
    if settings:
        text += f". To fix the store's settings, {_REMEDY[location.scheme]}"
    if settings or _SLOW_FAILURE.search(f"{kind} {detail}"):
        text += f". {_WAIT_HINT}"
    return FluxError(FLX_3505, text)


def open_store(location: ObjectLocation) -> Any:
    """The obstore store for *location*, built from the scheme and bucket alone.

    Building reads the environment and makes no request, so a setting that is missing or
    malformed (an Azure target with no account) fails here, before any row is generated.
    A credential that is wrong only shows on the first request. ``FLX-3505`` on failure.
    The client's timeouts and retry budget are set only when the person asked for them.
    """
    require_obstore()
    require_explicit_credentials(location)
    options: Dict[str, Any] = {}
    client = location.tuning.client_options()
    retry = location.tuning.retry_config()
    if client:
        options["client_options"] = client
    if retry:
        options["retry_config"] = retry
    try:
        return importlib.import_module("obstore.store").from_url(location.store_url, **options)
    except Exception as exc:  # noqa: BLE001
        raise _failure(
            f"the object store for {printable(location.url)} could not be set up", exc, location
        ) from None


#: How many keys a message lists before it counts the rest.
_LISTED_KEYS = 5


def _present(obstore: ModuleType, store: Any, key: str) -> bool:
    """Whether the object *key* exists; a failure other than "not found" is the caller's."""
    # obstore raises the builtin FileNotFoundError for a missing object.
    not_found = (FileNotFoundError, importlib.import_module("obstore.exceptions").NotFoundError)
    try:
        obstore.head(store, key)
    except not_found:
        return False
    return True


def _known_present(obstore: ModuleType, store: Any, key: str) -> Optional[bool]:
    """Whether *key* exists, or ``None`` when that cannot be told.

    S3 answers a HEAD of a missing key with a permission error when the credential has no
    ``ListBucket``, so any failure other than "not found" is "unknown", never a refusal.
    """
    try:
        return _present(obstore, store, key)
    except Exception:  # noqa: BLE001
        return None


def _cleanup(
    obstore: ModuleType,
    store: Any,
    written: List[str],
    earlier: Optional[bool],
    manifest_key: str = "",
) -> str:
    """Delete the objects this delivery wrote, best effort; say what became of them.

    Only the exact keys that were written are touched. The first failed delete ends the
    cleanup, because an endpoint that refuses one request is not asked for the rest, and the
    keys not yet deleted are named. An earlier complete delivery of the stream is never undone,
    and neither is one whose manifest is found in place after the failure (a manifest put that
    committed and then raised): without a manifest check the data objects are kept.
    *earlier* is ``None`` when the manifest could not be checked before the upload.
    """
    if not written:
        return ""
    kept = _kept_written(obstore, store, len(written), earlier, manifest_key)
    if kept is not None:
        return kept
    return _delete_written(obstore, store, written)


def _kept_written(
    obstore: ModuleType, store: Any, count: int, earlier: Optional[bool], manifest_key: str
) -> Optional[str]:
    """Why the written objects are kept, or ``None`` when they are to be deleted."""
    plural = "" if count == 1 else "s"
    unchecked = (
        f"; the {count} data object{plural} this delivery had written "
        f"{'was' if count == 1 else 'were'} kept because the state of the manifest could not be checked"
    )
    if earlier is None:
        return unchecked
    if not earlier and manifest_key:
        found = _known_present(obstore, store, manifest_key)
        if found is None:
            return unchecked
        if found:
            return (
                f"; the manifest of the stream is in place, so the {count} data "
                f"object{plural} this delivery had written {'was' if count == 1 else 'were'} kept"
            )
    if earlier:
        return (
            f"; the manifest of an earlier delivery of this stream is in place, so the {count} "
            f"object{plural} this delivery rewrote with the same bytes were kept"
        )
    return None


def _delete_written(obstore: ModuleType, store: Any, written: List[str]) -> str:
    """Delete the written keys in order; say what became of them."""
    count = len(written)
    plural = "" if count == 1 else "s"
    left = list(written)
    try:
        while left:
            obstore.delete(store, left[0])
            left.pop(0)
    except Exception:  # noqa: BLE001
        shown = ", ".join(left[:_LISTED_KEYS]) + (
            f" and {len(left) - _LISTED_KEYS} more" if len(left) > _LISTED_KEYS else ""
        )
        done = count - len(left)
        return (
            f"; the cleanup of the data objects this delivery had written stopped after "
            f"{done} of {count}: {len(left)} could not be deleted and remain without a manifest "
            f"({shown}), so delete them or publish the stream again"
        )
    verb = "was" if count == 1 else "were"
    return (
        f"; the {count} data object{plural} this delivery had written {verb} deleted, "
        "so none is left without a manifest"
    )


def upload_files(location: ObjectLocation, uploads: Iterable[Upload]) -> str:
    """Upload each file in order; return the writer, ``obstore <version>``.

    The last upload is the manifest, which commits the stream. The store is built from the
    scheme and the bucket alone, so obstore takes its credentials and endpoint from the
    environment. A failure is the engine's own ``FLX-3505``, its message scrubbed and the
    writer's exception not chained; the data objects this delivery had written before it are
    deleted, best effort (:func:`_cleanup`), and the message says what became of them.
    """
    obstore = require_obstore()
    batch = tuple(uploads)
    for item in batch:
        require_key_in_prefix(item.key, location.prefix)
    store = open_store(location)
    written: List[str] = []
    manifest_key = batch[-1].key if batch else ""
    earlier: Optional[bool] = _known_present(obstore, store, manifest_key) if batch else False
    try:
        for item in batch:
            obstore.put(store, item.key, item.path)
            written.append(item.key)
    except Exception as exc:  # noqa: BLE001
        failure = _failure(f"uploading to {printable(location.url)} failed", exc, location)
        raise FluxError(
            FLX_3505,
            failure.detail + _cleanup(obstore, store, written, earlier, manifest_key),
        ) from None
    return f"obstore {obstore.__version__}"
