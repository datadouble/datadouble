# Architecture

## Pipeline

```
plan → extract → load → transform → verify → package → store → replicate
```

| Stage | Input | Output |
|---|---|---|
| plan | Subset spec (native or Jailer `.jm`), live catalog, catalog lock | Execution plan: per-table extraction queries, resolved excludes, strict-mode result, size estimate |
| extract | Plan | Raw extract artifacts, all read from one consistent snapshot |
| load | Extract artifacts | Populated working database (scratch space, never shared storage) |
| transform | Working database | Anonymized working database |
| verify | Working database | Verification report; a failing report stops the pipeline |
| package | Verified working database | Golden image: logical dump and/or physical data directory, plus manifest |
| store | Golden image | Image published to one or more storage backends |
| replicate | Stored golden image | N disposable copies |

Not every run uses every stage. A job that only builds golden images stops after
`store`; a job that provisions a copy per pull request runs only `replicate`.

## Adapters

Each concern is an adapter type with a narrow interface. The core never contains
engine-specific logic; engine quirks stay inside the adapter.

| Adapter type | Responsibility | Examples |
|---|---|---|
| Engine (dump/load) | Introspect the catalog, open a consistent snapshot, extract, load, package | postgres, mysql, mssql, oracle, mongodb, redis, salesforce |
| Subset | Turn a subset spec into per-table extraction queries | FK-graph planner, Jailer `.jm` front end |
| Anonymize | Rewrite values column by column | faker, hash, null, format-preserving |
| Verify | Check the working database | fk-integrity, row-counts, pii-scan |
| Storage | Read and write immutable artifacts | local, s3, gcs, artifact repository, nfs |
| Replicate | Turn a golden image into copies | docker-volume, k8s-pv, csi-snapshot, zfs, plain-restore |

Adapters declare their capabilities (supported formats, tool versions, whether
they can share a snapshot across parallel workers) and the planner adapts to them.
Where a mature tool already exists, an adapter wraps it rather than reimplementing
it: native dump tools, and for Salesforce possibly SFDMU.

Adapters are discovered through standard plugin entry points, so third-party
adapters can be installed from anywhere.

## Artifacts, manifests and resumability

Every stage writes an artifact plus a manifest recording:

- Hashes of the stage's inputs and configuration
- Source server versions and tool versions
- Subset rules, resolved excludes, strict-mode level
- Anonymization strategy hashes
- Verification results
- Timings, row counts and sizes

Chaining is a single command:

```
datadouble run plan,extract,load,transform,verify,package,store --spec prod.yml
```

On a re-run, a stage whose input hashes match an existing artifact is skipped and
its artifact reused. That gives resume-after-failure and cheap partial re-runs
(for example, changing only the anonymization strategy re-runs from `transform`).

Working space and storage are separate concerns. Live database files are only
ever written to scratch space suited to them (local disk or strongly consistent
volumes). Storage adapters only handle finished, immutable artifacts.

## Interfaces

**JSON is primary.** Every operation is a JSON request with a JSON response:

```json
{"command": "run", "stages": ["plan", "extract"], "spec": "prod.yml", "strict": "columns"}
```

**The CLI is a thin layer** that builds those requests:

```
datadouble run plan,extract --spec prod.yml --strict=columns
datadouble run plan,extract --spec prod.yml --dry-run --json   # print the request only
datadouble plan --spec prod.yml --estimate --json               # machine-readable plan
```

Because the CLI only builds requests, the two interfaces cannot drift apart.

## Repository layout

One repository, with each adapter published as its own package so users install
only the drivers they need:

```
datadouble/
  core/            # planner, manifest, JSON API, CLI → package "datadouble"
  adapters/
    postgres/      # → "datadouble-postgres", own dependencies and version
    mysql/
    mssql/
    oracle/
    mongodb/
    redis/
    salesforce/
  conformance/     # shared test suite every adapter must pass
  dev/             # container profiles and sample data per engine
  docs/
```

`pip install datadouble[postgres,mongodb]` is a convenience over installing the
adapter packages directly. Adapters can move to their own repositories after the
adapter interface stabilizes, without users noticing.

## Credentials

Configuration refers to secrets indirectly (environment variables, Vault paths,
files). Resolved secrets are never written to manifests, artifacts or logs.
