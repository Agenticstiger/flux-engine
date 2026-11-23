# Third-party licenses

Generated 2026-11-23T13:42:27.222180Z by `scripts/third_party_licenses.py` (documented list derived from pyproject.toml (pip-licenses not installed)).
Every distribution `pyproject.toml` declares appears below; the list is
derived from those declarations rather than maintained by hand, and
`tests/test_third_party_licenses.py` fails when one has no entry.

| Package | License | Used for |
|---|---|---|
| numpy | BSD-3-Clause | core: seed-tree bit source |
| PyYAML | MIT | core: FLUX/FLUID document parsing |
| jsonschema | MIT | core: FLUX/FLUID schema validation |
| rfc8785 | MIT | core: JSON Canonicalization Scheme |
| cloudevents | Apache-2.0 | core: event envelope |
| cryptography | Apache-2.0 OR BSD-3-Clause | core: ed25519 signature over the fidelity record |
| flux-spec | Apache-2.0 | core: FLUX validator, RFC-04 enforcement gate, RFC-09 bundler |
| flux-stats | Apache-2.0 | core: the statistic registry, record reader, fixed-point grid and FLX catalog, released from this repository (packages/flux-stats) |
| pyarrow | Apache-2.0 | [arrow]: Arrow batches, Parquet and Arrow IPC files for the output sinks |
| adbc-driver-manager | Apache-2.0 | [adbc]: loads the ADBC driver the database sink writes through |
| cma | BSD-3-Clause | [calibrate]: CMA-ES optimiser for calibration |
| pytest | MIT | [dev] |
| pytest-cov | MIT | [dev] |
| hypothesis | MPL-2.0 | [dev]: property-based testing |
| mypy | MIT | [dev] |
| ruff | MIT | [dev] |
| black | MIT | [dev] |
| import-linter | BSD-2-Clause | [dev]: architecture contracts |
| pytest-xdist | MIT | [dev]: parallel test execution |
| pytest-timeout | MIT | [dev]: per-test hang backstop |
| pytest-split | MIT | [dev]: splits the fast test subset into balanced CI shards |
| pytest-socket | MIT | [dev]: blocks sockets in the tests, the offline guarantee's test-time check |
| sybil | MIT | [dev]: runs the examples in the documentation as tests |
| reuse | Apache-2.0 AND CC0-1.0 AND CC-BY-SA-4.0 AND GPL-3.0-or-later | [dev]: licence-header check, run as a tool and not distributed |
| obstore | MIT | [publish]: uploads the Parquet and Arrow IPC files to S3, GCS and Azure object stores |
| adbc-driver-sqlite | Apache-2.0 | [dev]: the database sink's tests, against SQLite |
| duckdb | MIT | [sql]: runs a consumer query over generated rows; also the database sink's tests |
| sqlglot | MIT | [sql]: reads and checks the SQL of a consumer query |
| openlineage-python | Apache-2.0 | [lineage]: RunEvent client |
| pyiceberg | Apache-2.0 | [publish]: writes one Iceberg table per stream through a catalogue |
| deltalake | Apache-2.0 | [publish]: writes one Delta Lake table per stream |
