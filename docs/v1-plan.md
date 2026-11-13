# Version 1 plan

The plan for building version 1, written so that a long autonomous development
run can check itself against it. Each milestone has acceptance criteria; the run
moves on only when they pass.

## Scope

1. A local development environment for every engine that can run locally.
2. Engine adapters, each implementing every stage, with a sample dataset:
   PostgreSQL, MySQL, SQL Server, Oracle, MongoDB, Redis, Elasticsearch,
   OpenSearch, a filesystem adapter, and Salesforce against an in-memory fake.
3. The core: spec and catalog lock, excludes, strict mode, the subset planner
   (both strategies), Jailer import, manifests and resumability, channels and
   image summaries, the JSON API and CLI.
4. Anonymization as described in [anonymization](anonymization.md): faker
   (deterministic), hash, null, format-preserving, engine-native expressions,
   and hooks.
5. Verify adapters: restore check, referential integrity, row counts, PII scan
   (database and files), SQL assertions.
6. Storage and replication adapters, for sourcing, cloning and replication:
   `filesystem`, `s3`, `btrfs`, `k8s-pv`, plus `docker-volume` for local and CI
   use.
7. Announce adapters, which let consumers know where clones can be accessed, as
   volumes or connection parameters: `k8s-secret`, `ci-outputs`, `dotenv`,
   `vault`. A `k8s-pv` copy's PersistentVolume is its own announcement
   (decision 61).
8. Operations: `promote`, `fetch`, `release`, `prune`, `activate`, in pool and
   on-demand modes.
9. Golden-image signing with cosign keyless signing in `build`, checked by
   `promote` and `fetch`.
10. Tests in containers under Docker Compose, with mix-and-match groups.
11. GitHub CI that builds signed, attested runtime images (see
    [supply chain](#supply-chain)).
12. Tier 1 CI/CD libraries: GitHub Actions, GitLab CI, Jenkins, Argo CD.
13. All agent skills, including filesystem help, and the Claude Code plugin.
14. A GitHub Pages documentation site at
    `https://datadouble.tightlinesoftware.com/`, built from the Markdown in
    `docs/` (see [documentation site](#documentation-site)), and a public example
    repository, `datadouble/example-pipeline`, covering the production and
    non-production sides.

### Not in version 1

- Recording Salesforce API cassettes. The run builds the recording, scrubbing
  and replay tooling; cassettes are recorded afterwards against a development
  org, outside the run, so the run never holds org credentials.
- ZFS and CSI-snapshot replication.
- Tier 2 CI/CD systems.

## Choices made for the run

- **Python 3.13** as the minimum version, with every later release tested in
  CI; the container images run Python 3.15 (decision 48).
- **Typing** per [architecture](architecture.md#typing): `mypy --strict`,
  `pyright` strict and `ruff`, with no unexplained suppressions.
- **Anonymization runs inside the working database.** Faker generates seeded
  value tables in the Datadouble process; the engine applies them in native
  updates. There is no separate anonymization service.
- **One Datadouble image, no engine software in it.** Engine servers and tools
  run in the vendors' own images through a container runtime (decision 72),
  so Datadouble never redistributes Oracle's or Microsoft's software.
- **Sample data licences** are checked before a dataset is committed; data is
  generated where a licence is unclear.
- **Copyright and licence:** every source file starts with SPDX headers (see
  [AGENTS.md](https://github.com/datadouble/datadouble/blob/main/AGENTS.md)).

## New adapters

### Elasticsearch and OpenSearch

Separate adapters, because the APIs have diverged since the fork (security,
snapshots, k-NN) and the official Elasticsearch client refuses to talk to
OpenSearch. Neither has foreign keys, so subsetting is per-index queries plus
declared join keys, strict mode applies to mapping fields, and point-in-time
searches give a consistent view across indices.

### Filesystem

```yaml
files:
  root: /srv/uploads
  select:                                   # one or more find(1) expressions
    - find . -path './avatars/*' -type f -mtime -30
    - find . -path './documents/*' -type f -size -10M
  paths_from: customer_documents.path       # follow the database subset
  exclude: ["./tmp", "./cache"]
  cleanup:                                  # shell, run on the copy only
    - find . -name '*.log' -delete
    - exiftool -all= -overwrite_original -r ./avatars
```

- **Consistency:** a consistent snapshot needs btrfs, ZFS or LVM. Elsewhere,
  extract fails unless the spec explicitly accepts an inconsistent copy, and the
  manifest records it.
- **Strict mode:** every top-level directory is matched by a `find` or
  explicitly excluded.
- **Following the database subset:** `paths_from` feeds a path list from the
  database extract to `find`, so only files for rows in the subset are copied.
- **Cleanup is not proof.** A files PII scan stays mandatory in `verify`.
- **Portability:** GNU findutils is pinned in the image. The filesystem skill
  explains each expression back in plain words and dry-runs it against a sample
  directory before writing it into the spec.

## Sample datasets

| Adapter | Dataset |
|---|---|
| PostgreSQL | Pagila |
| MySQL | Sakila (also the Jailer conformance test) |
| SQL Server | Sakila (decision 78) |
| Oracle | Sakila (decision 78) |
| MongoDB | Generated cinema database (`sample_mflix`'s licence is unclear) |
| Redis | Generated keyspace with every data type and key-encoded references |
| Elasticsearch, OpenSearch | Generated indices derived from Pagila, with join keys across indices |
| Filesystem | Generated upload tree keyed by Pagila customer IDs, with EXIF metadata and text files containing fake personal data |
| Salesforce | In-memory fake seeded from `describe` output |

Every dataset includes the awkward cases listed in
[local development](local-development.md).

## Documentation site

The site is built from the Markdown in `docs/`, the single source of truth; no
page is maintained as a separate copy.

| Section | Contents | Milestone |
|---|---|---|
| Home | The production-boundary diagram, overview and design principles | M0 |
| Design | Every design document, rendered as is, in reading order | M0 |
| Using Datadouble | Quickstart, spec reference generated from the JSON Schema, CLI reference generated from the CLI, CI/CD libraries, agent skills, the example repository | M10 |
| Project | Decisions, the v1 plan, external services, changelog, contributing | M0 |

- **Generator:** Zensical, pinned to an exact version (decision 46).
- **Theme:** the colors, type and light and dark modes of the design book, with
  its production-boundary diagram as the home page figure.
- **Publishing:** a workflow builds the site on every pull request (broken links
  fail the build) and deploys it to GitHub Pages on merges to `main`.

## Test environment

Docker Compose profiles. Services without a profile start on plain
`docker compose up`; the rest are opt-in and combinable.

| Group | Services | Notes |
|---|---|---|
| default | Test runner, PostgreSQL with Pagila, MongoDB, MySQL, SQL Server and Oracle Database Free with Sakila (and Jailer's export of it), S3 (SeaweedFS), Docker-in-Docker, OpenBao | Core, engine, S3, Docker and Vault tests (decisions 56, 64, 78); the SQL Server and Oracle images are amd64-only |
| `redis` | One engine | |
| `elasticsearch`, `opensearch` | Single-node clusters | |

| `btrfs` | Privileged test runner with a loop-mounted btrfs volume | Needs privileged Docker |
| `k8s` | k3s in a container | k8s-pv, k8s-secret, Argo CD (decision 64) |
| `ci-gitlab`, `ci-jenkins`, `ci-argocd` | CI systems for end-to-end tests | Heavy |

```
docker compose up                                  # core + PostgreSQL
docker compose --profile mysql up                  # MySQL as well
docker compose --profile k8s --profile btrfs up    # replication back ends
```

## Supply chain

- Every action pinned by commit SHA; `step-security/harden-runner` on every job;
  workflow tokens read-only by default
- CodeQL, dependency review, OpenSSF Scorecard, Snyk, SonarQube Cloud, Codecov
- SBOMs (Syft) with SBOM attestations, and build provenance attestations
- cosign keyless signing of every published image through GitHub OIDC, with
  documented `cosign verify` and `gh attestation verify` commands
- Images per pull request for testing; release images only from tags
- PyPI trusted publishing with attestations for every package

Account setup for these services is in
[external services](external-services.md).

## Milestones

Ordered so the core is proven end to end on one engine, then tested on a
differently shaped engine, before breadth.

| # | Milestone | Done when |
|---|---|---|
| M0 | **Foundations.** Package scaffolding with strict typing, Compose default group, CI with the supply-chain controls, `TESTING.md`, the release script with its changelog gate, and the documentation site with its Design section | `docker compose up` passes a smoke test; typing and lint pass, with every suppression explained and counted; CI publishes a signed, attested image for the pull request; the site builds with no broken links and is live on its domain |
| M1 | **Core and PostgreSQL, end to end.** Spec, lock, excludes, strict mode, both planner strategies, snapshot extract, load, anonymization, package, verify, store to filesystem and S3, manifests, resume, JSON API and CLI | Pagila runs plan through store and passes the engine conformance suite; a killed run resumes; both strategies produce identical row sets; strict-mode cases behave as specified |
| M2 | **Non-production side.** Promote, export store, image summaries, channels, image signing, fetch, replicate (filesystem, btrfs, docker-volume, k8s-pv), announce adapters, pool and on-demand modes, release, prune, activate | Promote refuses unverified or unsigned images; fetch skips when nothing is newer; pool recycling never empties the pool; activate rolls back; prune never removes a channel's target |
| M3 | **MongoDB** | Conformance passes with no engine-specific code in the core |
| M4 | **MySQL, SQL Server, Oracle.** In five pull requests (decision 80): container runtimes, MySQL, the Jailer front end, SQL Server, Oracle | Each passes conformance; Jailer and Datadouble produce identical key sets on Sakila |
| M4.5 | **Performance tuning.** Profile builds and the integration suite (`pytest --durations`); size working databases so empty engine space (MySQL's redo and undo logs, say) neither slows builds nor bloats physical images; run integration tests in parallel; keep pulled vendor images between Compose runs | Measured before and after; suite wall time and physical image sizes recorded in `TESTING.md` |
| M5 | **Redis, Elasticsearch, OpenSearch** | Each passes conformance, including strict mode on keys or mapping fields |
| M6 | **Filesystem** | Only files for subset rows are copied; an unmatched top-level directory fails the plan; a planted file of fake personal data fails verify |
| M7 | **Salesforce against the fake**, plus cassette recording, scrubbing and replay tooling | Conformance passes offline; the scrubber removes tokens, session IDs, instance URLs and org IDs from a synthetic recording |
| M8 | **CI/CD libraries** | The CI/CD conformance suite passes for all four systems, locally and in the nightly hosted runs |
| M9 | **Agent skills and plugin** | `claude plugin validate` passes and every skill scenario holds, including the refusal cases |
| M10 | **Using Datadouble documentation and example repository.** Quickstart, generated spec and CLI references, CI/CD library and agent skill guides, added to the existing site | The example repository's pipelines run green from a fresh clone, on both sides of the boundary; every spec field and CLI command appears in the generated references |

## How the run works

- **One branch and pull request per milestone,** with checkpoint commits. A
  pull request merges when its required checks pass; the run never pushes to
  `main` directly.
- **No releases.** The run never tags a release or publishes to PyPI; the
  first release is a maintainer's decision.
- **Changelog.** Every milestone pull request adds its user-visible changes to
  `CHANGELOG.md` under `[Unreleased]`, so the first release's notes are ready
  when it is cut.
- **Design changes go through the docs.** If implementation shows a design
  doc is wrong, the same pull request updates the doc and records the decision.
- **Machine:** running Oracle and SQL Server together needs roughly 6 GB of RAM
  or more, and the btrfs tests need privileged containers.
- **CI minutes:** heavy profiles and hosted CI/CD runs are scheduled, not run on
  every push.
