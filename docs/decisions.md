# Decision log and open questions

## Decided

| # | Decision | Rationale |
|---|---|---|
| 1 | Name: **Datadouble** | Describes the job (a realistic stand-in for real data); free on PyPI, npm and GitHub |
| 2 | Pluggable adapters for dump/load, subset, anonymize, verify, storage, replicate | Breadth of engines is a core goal |
| 3 | Minimum systems: PostgreSQL, MySQL, SQL Server, Oracle, Redis, MongoDB, Salesforce | Required coverage |
| 4 | Subsetting is planned up front and pushed down into extract | Sources can be terabytes |
| 5 | All reads in a run share one consistent snapshot where the engine allows | Integrity of filtered tables |
| 6 | JSON in / JSON out is primary; CLI is a thin layer over it | Interfaces cannot drift |
| 7 | Stages write artifacts with manifests; runs are resumable and chainable | Requirement |
| 8 | Native Jailer `.jm` support, plus a converter that fails on schema mismatch | Design in Jailer, run repeatedly in Datadouble |
| 9 | Excludes by schema/table glob | Requirement |
| 10 | `strict: off \| tables \| columns`, default `columns`; removed/changed objects always fail | Unknown tables and columns are the usual PII leak |
| 11 | Containers and sample data for every engine; Salesforce via recorded interactions plus an in-memory fake | Local development requirement |
| 12 | One repository, each adapter a separately published package | Adapter interface will change heavily before 1.0 |
| 13 | Repository private during planning | Publish when ready |

## Open

| Question | Notes |
|---|---|
| Implementation language | Python favored: mature drivers for every target and a strong anonymization ecosystem. Go would give single-binary distribution. |
| Licence | Apache-2.0 for adoption, or AGPL to discourage closed hosted forks |
| First release scope | Proposed: PostgreSQL, Faker-style anonymization, local and S3 storage, Docker-volume replication; MySQL next |
| Spec format details | YAML and JSON both, or JSON Schema-defined YAML only |
| Golden image formats | Logical dump, physical data directory, or both per engine |
| Trademark and domain checks | Before announcing the name publicly |
