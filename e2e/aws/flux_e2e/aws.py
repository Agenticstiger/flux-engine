# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""S3, the Glue catalog and Athena, for the owner's side only.

Every resource a run makes carries the prefix ``fluid-lane-flux-e2e-`` (Glue
spells it ``fluid_lane_flux_e2e_``), and :func:`sweep` deletes everything under
that prefix in the region, whichever run made it. Nothing here runs unless
``FLUX_E2E_AWS_LIVE=1`` is set.
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PREFIX = "fluid-lane-flux-e2e-"
GLUE_PREFIX = PREFIX.replace("-", "_")
LIVE_FLAG = "FLUX_E2E_AWS_LIVE"
#: Athena refuses a query that would scan more than this; the whole source is far smaller.
BYTES_SCANNED_CUTOFF = 2 * 1024**3


class NotLive(RuntimeError):
    """A step that touches AWS was asked for without the live flag."""


def require_live() -> None:
    if os.environ.get(LIVE_FLAG) != "1":
        raise NotLive(
            "this step talks to AWS (it creates or deletes resources and runs Athena "
            f"queries); set {LIVE_FLAG}=1 to allow it"
        )


@dataclass(frozen=True)
class Names:
    """The names of one run's resources."""

    run_id: str

    @property
    def bucket(self) -> str:
        return f"{PREFIX}{self.run_id}"

    @property
    def workgroup(self) -> str:
        return f"{PREFIX}{self.run_id}"

    def database(self, side: str) -> str:
        return f"{GLUE_PREFIX}{self.run_id.replace('-', '_')}_{side}"

    def prefix(self, side: str, table: str) -> str:
        return f"s3://{self.bucket}/{side}/{table}/"


def check_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,30}[a-z0-9]", run_id):
        raise ValueError("a run id is 4 to 32 lowercase letters, digits and hyphens")
    return run_id


def session(profile: Optional[str], region: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name=region)


def create(boto: Any, names: Names) -> None:
    """The bucket (private, encrypted), the two Glue databases and the workgroup."""
    require_live()
    s3 = boto.client("s3")
    region = boto.region_name
    kwargs: Dict[str, Any] = {"Bucket": names.bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    _tolerate_existing(lambda: s3.create_bucket(**kwargs), "BucketAlreadyOwnedByYou")
    s3.put_public_access_block(
        Bucket=names.bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_tagging(
        Bucket=names.bucket, Tagging={"TagSet": [{"Key": "purpose", "Value": "flux-e2e"}]}
    )
    glue = boto.client("glue")
    for side in ("source", "synthetic"):
        _tolerate_existing(
            lambda side=side: glue.create_database(DatabaseInput={"Name": names.database(side)}),
            "AlreadyExistsException",
        )
    _tolerate_existing(lambda: _create_workgroup(boto, names), "InvalidRequestException")


def _tolerate_existing(call: Callable[[], Any], code: str) -> None:
    """Run *call*; an error with *code* means the resource is there already."""
    from botocore.exceptions import ClientError

    try:
        call()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != code:
            raise
        if code == "InvalidRequestException" and "already" not in str(error).lower():
            raise


def _create_workgroup(boto: Any, names: Names) -> None:
    boto.client("athena").create_work_group(
        Name=names.workgroup,
        Configuration={
            "ResultConfiguration": {"OutputLocation": f"s3://{names.bucket}/athena-results/"},
            "EnforceWorkGroupConfiguration": True,
            "PublishCloudWatchMetricsEnabled": False,
            "BytesScannedCutoffPerQuery": BYTES_SCANNED_CUTOFF,
        },
        Description="flux e2e accuracy run",
        Tags=[{"Key": "purpose", "Value": "flux-e2e"}],
    )


def publish(
    boto: Any, names: Names, side: str, table: str, files: Sequence[Path]
) -> Dict[str, str]:
    """Upload a table's Parquet files and catalogue them; return the Glue column types."""
    import awswrangler as wr

    require_live()
    s3 = boto.client("s3")
    for index, path in enumerate(sorted(files)):
        key = f"{side}/{table}/part-{index:05d}.parquet"
        s3.upload_file(str(path), names.bucket, key)
    columns, _, _ = wr.s3.store_parquet_metadata(
        path=names.prefix(side, table),
        database=names.database(side),
        table=table,
        dataset=True,
        boto3_session=boto,
    )
    return dict(columns)


@dataclass
class QueryResult:
    rows: List[Tuple[Any, ...]]
    columns: List[str]
    types: List[str]
    bytes_scanned: int
    error: Optional[str] = None


def query(boto: Any, names: Names, side: str, sql: str) -> QueryResult:
    """Run one statement in the run's workgroup against one side's database."""
    import awswrangler as wr

    require_live()
    try:
        frame = wr.athena.read_sql_query(
            sql,
            database=names.database(side),
            ctas_approach=False,
            workgroup=names.workgroup,
            boto3_session=boto,
            keep_files=False,
        )
    except Exception as exc:  # an Athena failure is a result to report, not a crash
        return QueryResult([], [], [], 0, error=f"{type(exc).__name__}: {str(exc)[:500]}")
    statistics = (getattr(frame, "query_metadata", None) or {}).get("Statistics", {})
    columns = [str(column) for column in frame.columns]
    types = [str(dtype) for dtype in frame.dtypes]
    rows = [
        tuple(None if _is_missing(value) else value for value in row)
        for row in frame.itertuples(index=False)
    ]
    return QueryResult(rows, columns, types, int(statistics.get("DataScannedInBytes", 0)))


def _is_missing(value: Any) -> bool:
    try:
        import pandas as pd

        return bool(pd.isna(value)) if not isinstance(value, (list, tuple, dict)) else False
    except (TypeError, ValueError):
        return False


def run_all(
    new_session: Callable[[], Any],
    names: Names,
    jobs: Iterable[Tuple[str, str, str]],
    *,
    workers: int = 8,
) -> Dict[Tuple[str, str], QueryResult]:
    """Run (key, side, sql) jobs in parallel; results keyed by (key, side).

    boto3 sessions are not safe to share between threads, so each worker makes
    its own with *new_session*.
    """
    local = threading.local()

    def one(side: str, sql: str) -> QueryResult:
        if not hasattr(local, "boto"):
            local.boto = new_session()
        return query(local.boto, names, side, sql)

    work = list(jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {(key, side): pool.submit(one, side, sql) for key, side, sql in work}
        return {key: future.result() for key, future in futures.items()}


def sweep(boto: Any, report: Callable[[str], None] = print) -> Dict[str, Any]:
    """Delete every resource under the prefix in the session's region; count what is left.

    The prefix is the harness's, not one run's: one run at a time per region. A
    failure to delete one resource is recorded and the sweep goes on to the next.
    """
    require_live()
    s3 = boto.resource("s3")
    glue = boto.client("glue")
    athena = boto.client("athena")
    deleted = {"buckets": 0, "glueDatabases": 0, "workgroups": 0}
    errors: List[str] = []

    def attempt(kind: str, name: str, call: Callable[[], Any]) -> None:
        try:
            call()
        except Exception as error:  # recorded, and the next resource is still swept
            errors.append(f"{kind} {name}: {type(error).__name__}: {str(error)[:200]}")
            return
        deleted[kind] += 1
        report(f"deleted {kind[:-1]} {name}")

    def empty_and_delete(bucket: str) -> None:
        handle = s3.Bucket(bucket)
        handle.object_versions.delete()
        handle.objects.all().delete()
        handle.delete()

    for bucket in _buckets(boto, errors):
        attempt("buckets", bucket, lambda bucket=bucket: empty_and_delete(bucket))
    for database in _databases(glue):
        attempt(
            "glueDatabases", database, lambda database=database: glue.delete_database(Name=database)
        )
    for workgroup in _workgroups(athena):
        attempt(
            "workgroups",
            workgroup,
            lambda workgroup=workgroup: athena.delete_work_group(
                WorkGroup=workgroup, RecursiveDeleteOption=True
            ),
        )
    remaining = {
        "buckets": _buckets(boto, errors),
        "glueDatabases": _databases(glue),
        "workgroups": _workgroups(athena),
    }
    return {
        "region": boto.region_name,
        "deleted": deleted,
        "remaining": remaining,
        "errors": errors,
    }


def left_over(outcome: Mapping[str, Any]) -> bool:
    """Whether a sweep left a resource behind or failed to delete one."""
    return bool(outcome.get("errors")) or any(outcome.get("remaining", {}).values())


def _buckets(boto: Any, errors: List[str]) -> List[str]:
    # Buckets are global: only those in this region are swept here.
    s3 = boto.client("s3")
    names = [
        bucket["Name"]
        for bucket in s3.list_buckets()["Buckets"]
        if bucket["Name"].startswith(PREFIX)
    ]
    mine = []
    for name in names:
        try:
            location = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
        except Exception as error:  # gone since the listing, or not readable
            if "NoSuchBucket" not in str(error):
                errors.append(f"buckets {name}: {type(error).__name__}: {str(error)[:200]}")
            continue
        if (location or "us-east-1") == boto.region_name:
            mine.append(name)
    return mine


def _databases(glue: Any) -> List[str]:
    found: List[str] = []
    for page in glue.get_paginator("get_databases").paginate():
        found += [
            entry["Name"] for entry in page["DatabaseList"] if entry["Name"].startswith(GLUE_PREFIX)
        ]
    return found


def _workgroups(athena: Any) -> List[str]:
    found: List[str] = []
    token: Optional[str] = None
    while True:
        page = athena.list_work_groups(**({"NextToken": token} if token else {}))
        found += [entry["Name"] for entry in page["WorkGroups"] if entry["Name"].startswith(PREFIX)]
        token = page.get("NextToken")
        if not token:
            return found
