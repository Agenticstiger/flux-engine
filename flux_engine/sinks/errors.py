# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error codes raised by the output sinks.

KERNEL-SPEC §13 gives 3000 to "seam/conformance", and a sink is the last
thing in the emit seam, so its codes sit in that band, in the ``FLX-35nn``
block after the generator's ``FLX-34nn``.

This module imports nothing beyond the error catalog, so ``fluxsim
explain`` and the documentation generator can load every sink code without
any optional extra installed.
"""

from __future__ import annotations

from flux_engine.core.errors import ErrorCode, register

__all__ = [
    "FLX_3501",
    "FLX_3502",
    "FLX_3503",
    "FLX_3504",
    "FLX_3505",
    "FLX_3506",
    "FLX_3507",
    "FLX_3508",
    "FLX_3509",
    "FLX_3510",
    "FLX_3511",
]

FLX_3501: ErrorCode = register(
    "FLX-3501",
    "a sink needs an optional extra that is not installed",
    "The engine's core installs without any columnar or database library, so the sinks "
    "that need one live behind optional extras: the Arrow, Parquet and Arrow IPC sinks "
    "need pyarrow, from flux-sim-engine[arrow], and the database sink needs "
    "adbc-driver-manager, from flux-sim-engine[adbc], plus the ADBC driver for the "
    "database. An object store target for the Parquet and Arrow IPC sinks needs obstore, "
    "the Iceberg sink needs pyiceberg and the Delta sink needs deltalake, all from "
    "flux-sim-engine[publish]. Install the extra the message names, for example "
    "pip install 'flux-sim-engine[arrow]', and run the command again. Nothing was "
    "written: the check runs before a sink opens.",
)

FLX_3502: ErrorCode = register(
    "FLX-3502",
    "a value has no exact representation in the sink's format",
    "A sink writes every value exactly or not at all. An INT outside the signed 64-bit "
    "range has no Arrow integer type to hold it, and a DECIMAL whose precision exceeds "
    "76 digits has no Arrow decimal type, so the sink refuses the stream rather than "
    "round, wrap or truncate a value the run computed. The message names the column, "
    "the row index and the value's class, never the value. Narrow the column's declared "
    "type in the FLUID contract, or write this stream with a sink that keeps values as "
    "text, such as the run directory's own NDJSON file.",
)

FLX_3503: ErrorCode = register(
    "FLX-3503",
    "the data read back is not the stream it is read as",
    "Reading rows back from a file or a table needs to know which stream they belong to: "
    "each column's logical type and the stream's header, which the sinks record in the "
    "Arrow metadata they write. Data without that metadata, with columns renamed or "
    "re-ordered, or with a column stored in a type that cannot hold its declared logical "
    "type (a BOOL kept as text, a DECIMAL at another scale) cannot be turned back into "
    "the rendered rows the outputDigest covers. Pass the header and the logical types "
    "explicitly, or read the data the sink wrote rather than a copy that changed it.",
)

FLX_3504: ErrorCode = register(
    "FLX-3504",
    "a sink was given a target or an option it cannot use",
    "A sink writes only where it is told to and only in ways it can name, so it checks "
    "its target and its options before it opens: a file sink needs a directory that is "
    "not a file, a stream whose expose id is a safe single path segment, and options "
    "of the right type and range; every sink needs to be opened on the stream it was "
    "set up for. A target written as a URL must be an object store URL (s3://, gs://, "
    "az:// or abfs://); file://, memory://, ftp:// and any other scheme are refused, and "
    "a local directory is written as a plain path. An ADBC uri that holds a secret inline "
    "is refused as well, in any form: a password in user information (user:PW@host, with "
    "or without a scheme), a pair named like a credential (sig, token, password, "
    "access_token, X-Amz-Signature) in a query split on ?, & or ;, in a fragment, in a "
    "libpq key=value string (the value may be quoted) or in an ODBC Key=Value; string "
    "(PWD, Password). Put the uri in an environment variable and pass "
    "--sink-opt uri-env=NAME. The object store tuning options (request-timeout, "
    "connect-timeout, max-retries, retry-timeout) must be whole numbers in their range, and "
    "are refused for a local directory. The message names the target or the "
    "option and says what it expects. Nothing was written, and no run was started.",
)

FLX_3505: ErrorCode = register(
    "FLX-3505",
    "a sink could not finish writing a stream",
    "The destination refused a write part-way: a disk filled up, a permission was "
    "missing, a file could not be renamed into place, an object store could not be set up "
    "or refused an upload, or a read-back check found data that does not digest to the "
    "stream's outputDigest. A file sink writing to a local directory stages every file "
    "under a hidden name and renames it into place only when the whole stream is "
    "written, and it removes what it staged when it fails, so no partial stream is left "
    "for a reader to mistake for a whole one. On an object store the manifest is "
    "uploaded last and a stream is whole only when its manifest object is there; when an "
    "upload fails, the data objects this delivery had written are deleted, best effort "
    "and by their exact keys, and the message says whether that worked and which keys "
    "remain if not. Nothing is deleted when the manifest of an earlier, complete delivery "
    "of the stream is already in place, because the new objects have the same names and "
    "bytes. An endpoint that does not answer is retried for up to 3 minutes, obstore's "
    "default, before the failure; to give up sooner pass --sink-opt retry-timeout=SECONDS "
    "(and max-retries=N, request-timeout=SECONDS or connect-timeout=SECONDS, for the "
    "parquet and arrow-ipc sinks). When a delivery to several streams fails part-way, the "
    "message names the stream that failed and says how many streams were delivered; "
    "their receipts are kept in sink-receipts.json and the later streams were not "
    "attempted. An object store failure that comes of the credentials or the endpoint ends with the settings to "
    "check: the AWS_* variables and AWS_ALLOW_HTTP for s3://, GOOGLE_* for gs://, "
    "AZURE_* for az:// and abfs://. The message names variables, never their values. "
    "An object store target with no credential in the environment is refused before the "
    "run, with nothing contacted, because the store would otherwise ask the cloud's "
    "instance metadata service (169.254.169.254 and its siblings) for one; to use the "
    "credentials of the machine or workload the run is on (an instance role, a pod or "
    "managed identity), pass --sink-opt instance-identity=true. "
    "Whatever the target, the run directory and its outputDigest are unaffected and the "
    "run is kept: after the cause is fixed, publish it again with fluxsim apply "
    "BUNDLE --run-id RUN_ID --sink TARGET (the command prints it, with --state-dir when "
    "one was given), which writes the same files under the same names.",
)

FLX_3506: ErrorCode = register(
    "FLX-3506",
    "the database sink will not touch a table it does not manage",
    "The database sink only ever changes tables it created itself. Every table it "
    "creates carries four columns of its own, _flux_synthetic, _flux_run_id, "
    "_flux_output_digest and _flux_row, after the stream's declared columns, and it "
    "refuses an existing table that lacks them, so a production table that happens to "
    "share a name is never appended to, emptied or dropped. It also refuses, in append "
    "mode, a table whose columns no longer match the stream's (the contract changed), "
    "and, in create mode, any table that already exists. Choose another table prefix or "
    "schema, or, for a table the sink created under an older contract, use mode=replace.",
)

FLX_3507: ErrorCode = register(
    "FLX-3507",
    "there is no sink by that name",
    "A sink is named as NAME:TARGET, for example parquet:out/ or adbc:duckdb, and the name "
    "must be one this environment has: a sink the engine ships (parquet, arrow-ipc, adbc, "
    "iceberg, delta) or one a package registers under the flux_engine.sinks entry-point "
    "group. The message lists the names it found; fluxsim plugins lists them too, with the "
    "extra each one needs. Check the spelling, or install the package that provides the sink.",
)

FLX_3508: ErrorCode = register(
    "FLX-3508",
    "a sink option is named like a credential",
    "A sink never takes a credential as an option: a command line is visible to every user "
    "of the machine and is kept in shell history, and a record of the run would carry it. "
    "An option whose name holds the word password, secret, token, credential, sas, "
    "signature, passphrase, authorization, bearer or a key (access-key, account-key, "
    "api-key, authkey, secretaccesskey and the like) is refused, however "
    "it is spelled (access-key, access_key, accesskey, AccessKey), before "
    "the sink is opened, for every sink, built-in or not. The message names the option and "
    "never its value. Credentials come from the environment or from the platform's own "
    "credential chain, so remove the option and set the variable the destination's "
    "library reads, or sign in the way the platform does. Nothing was written.",
)

FLX_3509: ErrorCode = register(
    "FLX-3509",
    "a sink target carries user information, a query or a fragment",
    "A sink target that is a URL names a place and nothing else. User information "
    "(s3://user:pass@bucket/), a query string (?sig=...) and a fragment can each hold a "
    "credential, and a target is written into the receipt and the logs, so the target is "
    "refused before the sink is opened. The message shows the target with them removed. "
    "Pass the place alone and give the credentials through the environment or the "
    "platform's own credential chain. Nothing was written.",
)

FLX_3510: ErrorCode = register(
    "FLX-3510",
    "an object store target does not name a usable bucket and prefix",
    "The Parquet, Arrow IPC and Delta sinks take an object store URL (s3://, gs://, az:// or "
    "abfs://) as their target: the host is the bucket or container and the path is the "
    "prefix the files or tables go under, as in parquet:s3://bucket/simulations/run-a. Azure Data Lake "
    "Storage Gen2 also has its standard form, abfs://CONTAINER@ACCOUNT.dfs.core.windows.net/"
    "PREFIX (az:// takes it too), where the part before the @ is the container and the account "
    "is the first label of a host that ends in .dfs.core.windows.net; a host with no "
    "container before it is refused. The path is split on / and refused when it is empty, or when a segment is empty, . or .., "
    "or holds a backslash or a percent-encoded /, \\ or . (%2F, %5C, %2E, in either case), "
    "so that no spelling of a target can write outside the prefix it shows. The Iceberg "
    "sink's catalogue name and each level of its namespace are held to the same rules. A "
    "trailing / is allowed and dropped. Every object's key is the prefix, a /, and the "
    "relative name a local target would use, and is checked to start with the prefix and a / "
    "before anything is written. Name the bucket and a plain prefix; nothing was written.",
)

FLX_3511: ErrorCode = register(
    "FLX-3511",
    "a catalogue configuration file sits in the current directory",
    "The Iceberg sink loads its catalogue through pyiceberg, which reads its configuration "
    "from a .pyiceberg.yaml file in PYICEBERG_HOME, in the home directory and in the current "
    "directory, and a catalogue's configuration can name the endpoint that receives the "
    "storage requests the sink signs with the person's own credentials. So the sink refuses "
    "to start when a .pyiceberg.yaml file is in the current directory and the current "
    "directory is neither the home directory nor PYICEBERG_HOME: a bundle directory cannot "
    "point a catalogue at another endpoint by carrying such a file. Run the command from "
    "another directory, or keep the catalogue configuration in the home directory or in the "
    "directory PYICEBERG_HOME names. Nothing was written.",
)
