# Third-party licenses

Generated 2026-11-17T05:02:46.806840Z by `scripts/third_party_licenses.py` (documented list derived from pyproject.toml (pip-licenses not installed)).
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
| pytest | MIT | [dev] |
| pytest-cov | MIT | [dev] |
| hypothesis | MPL-2.0 | [dev]: property-based testing |
| mypy | MIT | [dev] |
| ruff | MIT | [dev] |
| black | MIT | [dev] |
| import-linter | BSD-2-Clause | [dev]: architecture contracts |
| pytest-xdist | MIT | [dev]: parallel test execution |
| pytest-timeout | MIT | [dev]: per-test hang backstop |
| sybil | MIT | [dev]: runs the examples in the documentation as tests |
| reuse | Apache-2.0 AND CC0-1.0 AND CC-BY-SA-4.0 AND GPL-3.0-or-later | [dev]: licence-header check, run as a tool and not distributed |
| adbc-driver-sqlite | Apache-2.0 | [dev]: the database sink's tests, against SQLite |
| duckdb | MIT | [dev]: the database sink's tests, against DuckDB |
| openlineage-python | Apache-2.0 | [lineage]: RunEvent client |
