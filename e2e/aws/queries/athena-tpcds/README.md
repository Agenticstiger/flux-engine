# Athena-dialect TPC-DS-shaped queries

These 98 files are copied unchanged from
[awslabs/aws-athena-query-federation](https://github.com/awslabs/aws-athena-query-federation),
directory `athena-tpcds/src/main/resources/queries`, at commit
`458a4d4f6ac86d38debd248e6fec5fc2b9a3c293`. That repository is licensed under the
Apache License 2.0 (`LICENSES/Apache-2.0.txt` in this repository), and its NOTICE
reads:

> Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.

The upstream set has no file for queries 36, 58, 70, 72 and 86, and spells 14, 23,
24 and 39 as their `a` and `b` variants. Each file names its tables with a
`[ TABLE_SUFFIX ]` placeholder; the harness removes it when it reads a file and runs
the query against one Glue database at a time. A run uses the queries that read only
the tables in its scope.

The source these queries run on is a TPC-DS-shaped dataset generated with DuckDB's
tpcds extension. No number the harness reports is a TPC-DS benchmark result.
