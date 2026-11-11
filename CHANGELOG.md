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
- **`datadouble-postgres`**, the PostgreSQL engine adapter: one exported
  snapshot for keys, rows and schema; working databases as throwaway local
  clusters; physical (data directory) and logical (`pg_dump`) images.
- **Stores:** a filesystem store in the core package, and **`datadouble-s3`**
  for S3 and S3-compatible services.
- **`datadouble-conformance`**, the suite every engine adapter must pass.
