# Architecture

## Pipeline

```
plan → extract → load → transform → package → verify → store → promote → fetch → replicate → announce
```

plus three operations on existing images and copies: `release`, `prune` and
`activate`.

| Stage | Input | Output |
|---|---|---|
| plan | Subset spec (native or Jailer `.jm`), live catalog, catalog lock | Execution plan: per-table extraction queries, resolved excludes, strict-mode result, size estimate |
| extract | Plan | Raw extract artifacts, all read from one consistent snapshot |
| load | Extract artifacts | One working database per image group (scratch space, never shared storage); `before_load` hooks run first |
| transform | Working database | Anonymized working database; `after_transform` hooks run last |
| package | Anonymized working databases | One golden image per group and engine, in each requested format, plus manifests |
| verify | Golden image, restored to scratch space | Verification report; a failing report stops the pipeline |
| store | Verified golden image | Image and its image summary written to one or more storage backends |
| promote | Stored golden image | The image copied to the export store and a channel advanced; refused unless verification passed |
| fetch | Image in the export store | The image unpacked into non-production staging and a staging channel advanced; skipped if nothing newer |
| replicate | Staged image | Copies, as volumes or running instances, in pool or on-demand mode |
| announce | Copy descriptions | Consumers told where each clone can be accessed: a volume, or connection parameters |

| Operation | Effect |
|---|---|
| release | Destroys copies by ID, owner or expiry; recycles pool copies of old generations |
| prune | Applies retention to images in stores and staging; never removes a channel's target |
| activate | Points a channel at a chosen generation (rollback) |

Not every run uses every stage. Stages group into jobs (build, promote, fetch,
provision, release, prune, activate) that run in different places: everything up
to `store` runs inside production, and only a verified golden image leaves it.
See [CI/CD integration](ci-cd.md).

## Golden images

### Formats

A golden image comes in one or both of two formats:

- **physical:** the engine's own on-disk form, fastest to clone and start, but
  tied to the server version
- **logical:** a portable export, restorable into other server versions

```yaml
package:
  formats: [physical, logical]   # at least one
```

The CLI can override it for one run (`--format logical`), which sets the same
field in the JSON request. The field is part of the build's input hash and is
recorded in the manifest. When the spec leaves it out, each engine produces
every format it supports. Listing a format an engine can't produce fails the
plan and names the formats that engine supports; there is no silent fallback.

| Engine | physical | logical |
|---|---|---|
| PostgreSQL, MySQL, SQL Server, Oracle | Data directory | Native dump |
| MongoDB | WiredTiger data directory | `mongodump` |
| Redis | RDB file | Not offered |
| Elasticsearch, OpenSearch | Snapshot repository | NDJSON export |
| Salesforce | Not offered | Records and metadata |
| Filesystem | The file tree | Not offered |

### Several images from one build

A spec can split its output into named groups, each becoming its own image:

```yaml
images:
  all:     { include: ["*"] }                  # a combined image
  billing: { include: ["billing", "billing_audit"] }
  content: { include: ["cms.*"], requires: [billing] }
```

With no `images:` section there is one group containing everything.

1. **One extract.** The plan, the consistent snapshot and the extract are
   shared by every group, so the source is read once however many images come
   out.
2. **One working database per group.** `load` builds each group's working
   database from the shared extract; `transform`, `package` and `verify` then
   run per group.
3. **Images that agree.** Anonymization is seeded once per run, so a value gets
   the same replacement in every group, and all images from one build share a
   generation. Consumers that need several groups together take the same
   generation of each, or a group that already contains them; images are never
   merged after the fact.
4. **References between groups are checked at plan time.** A reference from
   one group into another fails the plan unless the edge has a fallback (`null`
   or `stub`) or the group declares `requires:` the other, in which case
   `replicate` provisions the required images alongside it.
5. **Independent life cycles.** Each image has its own manifest, identity and
   channels, so it can be promoted, fetched, rolled back and pruned on its own.

Groups match on whatever namespace an engine has: databases and schemas for
relational engines and MongoDB, database numbers or key prefixes for Redis,
index patterns for Elasticsearch and OpenSearch, object sets for Salesforce, and
top-level directories for the filesystem adapter. A spec with several sources
produces one image per engine per group, all sharing the generation.

## Adapters

Each concern is an adapter type with a narrow interface. The core never contains
engine-specific logic; engine quirks stay inside the adapter.

| Adapter type | Responsibility | Examples |
|---|---|---|
| Engine (dump/load) | Introspect the catalog, open a consistent snapshot, extract, load, package | postgres, mysql, mssql, oracle, mongodb, redis, salesforce |
| Subset | Turn a subset spec into per-table extraction queries | FK-graph planner, Jailer `.jm` front end |
| Anonymize | Rewrite values column by column (see [anonymization](anonymization.md)) | faker, hash, null, format-preserving, expression (e.g. SQL) |
| Verify | Check a restored golden image | fk-integrity, row-counts, pii-scan |
| Storage | Read and write immutable artifacts | local, s3, gcs, artifact repository, nfs |
| Replicate | Turn a staged image into copies (volumes or instances), and destroy them | filesystem, docker-volume, btrfs, k8s-pv; later nfs-copy, csi-snapshot, zfs |
| Announce | Let consumers know where clones can be accessed, as volumes or connection parameters; never writes data | k8s-secret, ci-outputs, vault, dotenv (a `k8s-pv` copy announces itself) |
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
datadouble run plan,extract,load,transform,package,verify,store --spec prod.yml
```

A run belongs to a generation, and keeps its stages' artifacts and manifests
under `<work>/<spec>/<generation>/`. Resuming a generation re-runs only the
stages whose input hashes changed and skips the rest, reusing their artifacts.
That gives resume-after-failure and cheap partial re-runs (for example, changing
only the anonymization strategy re-runs from `transform`):

```
datadouble run plan,extract,load,transform,package,verify,store --spec prod.yml --resume 0193a7c21b4e9f30
```

The generation also fixes the run's anchor time (`as_of`, when the spec gives
none) and its anonymization key, so a resumed run produces the same values.
The work directory holds real personal data from `extract` on: it is production
scratch space, like the working databases.

Working space and storage are separate concerns. Live database files are only
ever written to scratch space suited to them (local disk or strongly consistent
volumes). Working databases are throwaway servers the engine adapter starts there,
one per image group. Storage adapters only handle finished, immutable artifacts.

### Engine software runs in the vendor's images

Datadouble installs no engine software (decision 72). Working databases,
restores for verification, and tools such as `pg_dump` or `mongorestore` run
in containers from the engine vendor's official image: by default the image of
exactly the source's version, so a PostgreSQL 16.4 source is built with
`postgres:16.4-trixie`: a dump from a newer minor release can hold what an
older server rejects. A source running a release whose image is not published
yet fails to pull, and names an image in `source.image`.
`source.image` names another, for example one with the extensions the source
uses, or a copy in a private registry.

A **container runtime** adapter starts those containers, chosen by `runtime:`
in the spec (decision 73):

```yaml
runtime:
  docker: {}                     # any Docker-compatible daemon, through the docker command
```

```yaml
runtime:
  kubernetes:                    # pods, through kubectl
    namespace: datadouble
    claim: datadouble-work       # the PersistentVolumeClaim holding the work directory
    mount: /work                 # where that claim is mounted in Datadouble's own pod
```

- **Shared paths.** Every container sees the paths it works on at the same
  absolute path as Datadouble. With Docker they are bind-mounted, so the work
  directory must exist at the same path on the daemon's host; on Kubernetes,
  Datadouble runs as a pod with the work directory on a claim, and every
  engine pod mounts the same claim at the same path.
- **One user.** Containers run as Datadouble's own user and group, so either
  side can read and remove the other's files.
- **Credentials.** A working server accepts a password generated for that start,
  passed through the runtime's secret channel (the docker command's
  environment, or a Secret object) and never a command line. Tools that need
  the server run inside its container. Credentials made for the build are
  removed before an image is packaged (decision 74).
- **Leftovers.** Containers are labelled with the stage directory they serve;
  before a stage runs again the core removes what an interrupted attempt left
  behind (decision 75).

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

## Typing

Python with strict static typing, enforced in CI by `mypy --strict`, `pyright`
in strict mode, and `ruff` with its annotation rules. The aim is that no value
is "stringly typed":

- **Parse at the boundary.** Specs, manifests and JSON requests are parsed once
  into typed, immutable models (Pydantic in strict mode), which also generate
  the published JSON Schemas. Raw dicts never travel past the parser.
- **Closed sets are enums.** Stage names, strict-mode levels, outcomes, copy
  kinds and replicate modes are `StrEnum`s, not string literals.
- **Identifiers have types.** Table names, column paths, generations and channel
  names are distinct types (`NewType` or small value classes), so one can't be
  passed where another is expected.
- **Adapter interfaces are typed `Protocol`s,** and every package ships
  `py.typed`, so third-party adapters are checked against them too.
- `Any`, `cast` and `# type: ignore` need a comment explaining why, and CI
  counts them.

## Credentials

Configuration refers to secrets indirectly (environment variables, Vault paths,
files). Resolved secrets are never written to manifests, artifacts or logs.
