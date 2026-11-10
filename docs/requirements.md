# Requirements

## Functional

1. **Breadth of engines.** Minimum supported systems: PostgreSQL, MySQL, Microsoft
   SQL Server, Oracle, Redis, MongoDB and Salesforce. Breadth is a core goal, not
   a stretch goal.
2. **Pluggable adapters** for each concern:
   - Dump/load (native dump tools, ETL exports, CSV, APIs)
   - Subsetting
   - Anonymization (Faker-style generators, hashing, nulling, format-preserving)
   - Verification (referential integrity, row counts, PII scanning)
   - Storage, for both inputs and golden-image outputs (local disk, object storage,
     artifact repositories, network shares)
   - Replication, turning a golden image into many copies (Docker volumes,
     Kubernetes persistent volumes and CSI snapshots, copy-on-write filesystems,
     plain restore)
3. **Subsetting during extract.** Sources may be hundreds of gigabytes or
   terabytes. Filtering must be pushed down to the source while producing a
   relationally consistent result with minimal data transferred.
   See [subsetting](subsetting.md).
4. **Native Jailer support.** Jailer extraction models (`.jm` plus data model
   folder) can be used directly as the subset definition, and converted to the
   native format. Conversion fails on schema mismatch. See [Jailer](jailer.md).
5. **Excludes** by schema and table name with wildcard support, e.g.
   `exclude: ["dba_maint_*", "test_*"]`.
6. **Strict mode**, on by default at its strictest level, failing when the planned
   extract contains tables (and optionally columns) unknown to the subset model.
   See [excludes and strict mode](excludes-and-strict-mode.md).
7. **Resumable, chainable stages.** Several stages run in one command; a failed
   run resumes from the last completed stage.

## Interfaces

8. **Programmatic interface:** JSON request in, JSON response out.
9. **CLI:** conventional `datadouble <command> --switch ... args`, a thin layer over
   the JSON interface. Every CLI invocation can print the equivalent JSON request.

## Development

10. **Local development with containers** for every engine, each with sample data.
    Salesforce, which cannot run in a container, is covered by recorded API
    interactions and an in-memory fake. See [local development](local-development.md).

## Non-functional

- Credentials come from the environment, Vault or files; they never appear in
  configuration files, artifacts, manifests or logs.
- Any run's output is explainable from its manifest: source versions, subset
  rules, anonymization strategy hashes, verification results.
- Fail loudly. No silent merges of configuration, no "warning and carry on" for
  anything that can put real personal data into a copy.
