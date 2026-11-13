# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The `datadouble` package and command.** `datadouble version` reports the
  installed version, `--json` prints the JSON response instead of a summary,
  `--dry-run` prints the JSON request without running it, and `datadouble api`
  reads a JSON request from standard input. Every later command follows the
  same pattern, because the command line only builds JSON requests.
- **A container image, `ghcr.io/datadouble/datadouble`,** signed with cosign
  keyless signing and published with SBOM and build provenance attestations.
  It carries Datadouble, its adapters, the `docker` command, `kubectl` and
  cosign, and no engine software.
- **A documentation site** at <https://datadouble.tightlinesoftware.com/>,
  generated from the design documents.
- **Python support:** 3.13 or later, tested on 3.13, 3.14 and 3.15. The
  container image runs Python 3.15.
- **Specs** in YAML or JSON, validated against one JSON Schema
  (`datadouble schema`), with secrets referred to by environment variable or
  file, never written into the spec.
- **`datadouble lock`** writes `catalog.lock`, and **strict mode** compares the
  live catalog with it at plan time: `columns` (the default) fails on new
  tables and columns, `tables` on new tables, `off` only warns. Removed or
  changed objects always fail.
- **Excludes and structure-only tables** by shell-style patterns, resolved once
  and recorded.
- **`datadouble plan`**: the subset planner, with starting filters (`seeds`),
  followed child edges, virtual references (optionally conditional), per-edge
  fallbacks (`follow: all`, `null`, `stub`, `drop`), image groups with
  `requires:`, and both strategies: nested queries pushed to the source, or
  key collection with the closure computed by Datadouble. Every problem in a
  spec is reported at once.
- **`datadouble run`** for the build stages (`plan`, `extract`, `load`,
  `transform`, `package`, `verify`, `store`), with a manifest per stage and
  `--resume GENERATION` to continue a stopped run from its last completed stage.
- **Anonymization:** deterministic faker values, keyed hashes, null,
  format-preserving replacement and engine-native expressions, applied in one
  native update per table; `before_load` and `after_transform` hooks.
- **Verification** of every image format after a scratch restore: row counts,
  references, a PII scan and the spec's assertions.
- **Golden images** packed per format with their build manifest, stored with an
  image summary and the verification report.
- **Engine software in the vendor's images.** Working databases, restores and
  tools such as `pg_dump` run in containers from the engine vendor's official
  image of the source's version, or the image in `source.image`, started by a
  container runtime named in the spec's `runtime:`: `docker` for any
  Docker-compatible daemon, or `kubernetes` for pods sharing the work
  directory's PersistentVolumeClaim. Working servers accept only a password
  generated for each start. Failures are reported as `container_failed`.
- **`datadouble-postgres`**, the PostgreSQL engine adapter: one exported
  snapshot for keys, rows and schema; working databases as throwaway clusters
  in the official PostgreSQL image; physical (data directory) and logical
  (`pg_dump`) images.
- **`datadouble-mongodb`**, the MongoDB engine adapter: collections and dotted
  field paths as tables and columns, from sampling and `$jsonSchema`
  validators; extract in one snapshot session with the keys strategy; seeds,
  conditions and assertions in MongoDB's own query and aggregation language;
  physical (data directory) and logical (`mongodump` archive) images.
- **`datadouble-mysql`**, the MySQL engine adapter: one consistent-snapshot
  transaction for keys and rows, the schema from `mysqldump`, working
  databases as throwaway servers in the official MySQL image, and physical
  (data directory) and logical (`mysqldump`) images.
- **`datadouble-mssql`**, the SQL Server engine adapter: keys, schema and rows
  read in one snapshot-isolation transaction, the schema scripted from the
  catalog views, working databases as throwaway instances in Microsoft's SQL
  Server image, and physical (data and log files) and logical (schema and
  rows) images. Working databases need `source.server.accept_eula: true` and
  Datadouble running as uid and gid 10001.
- **`source.server`:** settings for an engine's working servers, checked by
  its adapter.
- **`datadouble-oracle`**, the Oracle engine adapter: one or more schemas
  scripted with `DBMS_METADATA` and read in one read-only transaction,
  working databases as pluggable databases in throwaway Oracle Database Free
  containers, and physical (an unplugged pluggable database) and logical
  (schema and rows) images. Datadouble runs as 54321:54321 for Oracle.
- **Engine users:** Datadouble runs as the `uid:gid` an engine's copies are
  served as (999:999 for the official PostgreSQL, MySQL and MongoDB images,
  10001:10001 for SQL Server, 54321:54321 for Oracle), so physical images belong to their server on
  every replicate adapter; builds, `fetch` and `replicate` refuse to run as
  anyone else and say which user to run as.
- **Jailer subsets:** a spec's `subset.jailer` names a Jailer extraction model
  and data model, read and translated on every run; `datadouble import-jailer`
  writes the translation as native settings. On Sakila, Jailer and Datadouble
  choose the same rows.
- **Stores:** a filesystem store in the core package, and **`datadouble-s3`**
  for S3 and S3-compatible services.
- **`datadouble-conformance`**, the suite every engine adapter must pass.
- **Image signing** with cosign in the `store` stage, by key reference or
  keyless, and signature checks in `promote` and `fetch`.
- **`datadouble promote`**: copies verified, signed images built with the
  required strict-mode level into the export stores and advances a channel,
  refusing any image that fails a check.
- **`datadouble fetch`** and **`datadouble activate`**: staging in
  non-production, skipping generations already staged, keeping the newest
  for rollback.
- **`datadouble replicate`**, **`claim`**, **`release`** and **`copies`**:
  pool copies recycled one for one so the pool never empties, and on-demand
  copies that belong to an owner and expire. Replicate adapters: `filesystem`
  (core), **`datadouble-docker`** (`docker-volume`: volumes, or instances
  running the engine's server with a generated password),
  **`datadouble-btrfs`** (`btrfs`) and **`datadouble-kubernetes`** (`k8s-pv`).
- **`datadouble announce`**: `dotenv` and `ci-outputs` (core), `k8s-secret`
  (`datadouble-kubernetes`) and **`datadouble-vault`** (`vault`).
- **`datadouble prune`** for the build's stores, the export stores and
  staging, never removing a channel's target, with `--dry-run`.
