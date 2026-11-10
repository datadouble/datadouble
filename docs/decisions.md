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
| 14 | Licence: MIT | Maximum ease of adoption |
| 15 | CI/CD adapters, Tier 1: GitHub Actions, GitLab CI, Jenkins, Argo CD; each tested end to end | Datadouble mostly runs inside other teams' pipelines |
| 16 | Lifecycle split into jobs: build, promote, fetch, provision, release, prune, activate; stages `promote`, `fetch`, `publish`; operations `release`, `prune`, `activate` | Jobs run in different environments, on different schedules, with different credentials |
| 17 | Everything up to `verify` runs where `extract` runs; `promote` into an export store is the only production crossing and refuses unverified images; non-production pulls from the export store only | Raw extracts and working databases contain real personal data |
| 18 | CI/CD adapters contain no pipeline logic and never put data artifacts in CI artifact storage | Same reason as the thin CLI; CI storage is often outside production |
| 19 | `build` refuses to generate or run on vendor-hosted runners | Prevents production data being pulled onto a vendor's machines |
| 20 | `verify` runs after `package`, against a scratch restore of the image | Verifies the artifact that ships, including that it restores |
| 21 | Run outcomes: success, warning, failure; anything that could leak personal data is a failure; no continue-on-error switch | Operators need "fix this soon" without weakening fail-loudly |
| 22 | Images identified by spec, engine, target engine version and generation; addressed through channels; sidecar manifests readable without download | Cheap change detection, rollback, and test channels that can't replace live images |
| 23 | `replicate` supports pool mode (N unclaimed copies, recycled by generation) and on-demand mode; copies are volumes or instances | Large images copy slower than people will wait; small ones suit per-request copies |
| 24 | Agent skills (Agent Skills format) for each job, the spec, and a whole pipeline; shipped in this repository as a Claude Code plugin and plain skill directories | Agents increasingly do pipeline setup; skills make it repeatable |
| 25 | Skills contain no logic: all files come from `datadouble` commands, and skills never weaken safety settings or handle secrets | Same reason as the thin CLI; an agent must not trade safety for a passing run |

## Open

| Question | Notes |
|---|---|
| Implementation language | Python favored: mature drivers for every target and a strong anonymization ecosystem. Go would give single-binary distribution. |
| First release scope | Proposed: PostgreSQL, Faker-style anonymization, local and S3 storage, Docker-volume replication; MySQL next |
| Spec format details | YAML and JSON both, or JSON Schema-defined YAML only |
| Golden image formats | Logical dump, physical data directory, or both per engine |
| Trademark and domain checks | Before announcing the name publicly |
| Golden image signing | Sign manifests in `build` so non-production can check provenance before `provision` (e.g. Sigstore/cosign) |
| Jenkins packaging | Shared library only, or a plugin too |
| Non-database assets | File trees (uploads, media) that rows refer to are often needed alongside the database, and may hold personal data. A `files` engine adapter, and how to anonymize or filter its contents |
| Several images from one spec | One combined image, one per database, or both from one build |
| CI/CD in first release | Proposed: GitHub Actions and the Kubernetes base first, the rest of Tier 1 next |
