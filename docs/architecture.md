# Architecture

## Pipeline

```
plan → extract → load → transform → package → verify → store → promote → fetch → replicate → publish
```

plus three operations on existing images and copies: `release`, `prune` and
`activate`.

| Stage | Input | Output |
|---|---|---|
| plan | Subset spec (native or Jailer `.jm`), live catalog, catalog lock | Execution plan: per-table extraction queries, resolved excludes, strict-mode result, size estimate |
| extract | Plan | Raw extract artifacts, all read from one consistent snapshot |
| load | Extract artifacts | Populated working database (scratch space, never shared storage); `before_load` hooks run first |
| transform | Working database | Anonymized working database; `after_transform` hooks run last |
| package | Anonymized working database | Golden image: logical dump and/or physical data directory, plus manifest |
| verify | Golden image, restored to scratch space | Verification report; a failing report stops the pipeline |
| store | Verified golden image | Image and sidecar manifest written to one or more storage backends |
| promote | Stored golden image | The image copied to the export store and a channel advanced; refused unless verification passed |
| fetch | Image in the export store | The image unpacked into non-production staging and a staging channel advanced; skipped if nothing newer |
| replicate | Staged image | Copies, as volumes or running instances, in pool or on-demand mode |
| publish | Copy descriptions | Copies registered where consumers find them |

| Operation | Effect |
|---|---|
| release | Destroys copies by ID, owner or expiry; recycles pool copies of old generations |
| prune | Applies retention to images in stores and staging; never removes a channel's target |
| activate | Points a channel at a chosen generation (rollback) |

Not every run uses every stage. Stages group into jobs (build, promote, fetch,
provision, release, prune, activate) that run in different places: everything up
to `store` runs inside production, and only a verified golden image leaves it.
See [CI/CD integration](ci-cd.md).

## Adapters

Each concern is an adapter type with a narrow interface. The core never contains
engine-specific logic; engine quirks stay inside the adapter.

| Adapter type | Responsibility | Examples |
|---|---|---|
| Engine (dump/load) | Introspect the catalog, open a consistent snapshot, extract, load, package | postgres, mysql, mssql, oracle, mongodb, redis, salesforce |
| Subset | Turn a subset spec into per-table extraction queries | FK-graph planner, Jailer `.jm` front end |
| Anonymize | Rewrite values column by column | faker, hash, null, format-preserving |
| Verify | Check a restored golden image | fk-integrity, row-counts, pii-scan |
| Storage | Read and write immutable artifacts | local, s3, gcs, artifact repository, nfs |
| Replicate | Turn a staged image into copies (volumes or instances), and destroy them | docker-volume, nfs-copy, csi-snapshot, zfs, plain-restore |
| Publish | Register copies where consumers find them | k8s-pv, k8s-secret, ci-outputs, vault, dotenv |
| CI/CD | Package, generate and bridge jobs for a CI/CD system; no pipeline logic | github-actions, gitlab, jenkins, argocd |

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
  ci/
    github-actions/  # container actions and reusable workflows
    gitlab/          # CI/CD Catalog components
    jenkins/         # shared library
    kubernetes/      # container image, Job templates, Helm chart (Argo CD)
  .claude-plugin/  # Claude Code marketplace and plugin manifests
  skills/          # agent skills, one directory per skill
  conformance/     # shared test suites (engine, CI/CD, agent skills)
  dev/             # container profiles and sample data per engine and CI system
  docs/
```

`pip install datadouble[postgres,mongodb]` is a convenience over installing the
adapter packages directly. Adapters can move to their own repositories after the
adapter interface stabilizes, without users noticing.

## Credentials

Configuration refers to secrets indirectly (environment variables, Vault paths,
files). Resolved secrets are never written to manifests, artifacts or logs.
