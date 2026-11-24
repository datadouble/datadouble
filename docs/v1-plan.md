# Version 1 plan

Version 1 was built by a long autonomous development run, not by people
working through a backlog. A run like that needs a plan it can check itself
against: what to build, in what order, and how to tell when each piece is
done. This page is that plan.

The work is divided into milestones. Each milestone has acceptance criteria,
and the run moves on to the next milestone only when they pass.

The page covers, in order:

1. the **scope** of version 1, and what is left out;
2. the **rules** the run works by, and the technical **choices** made before
   it started;
3. the **milestones** and their acceptance criteria;
4. the **details** the plan settles for particular areas: new adapters,
   sample data, the test environment, the documentation site and the supply
   chain.

## Scope

Version 1 includes:

1. A local development environment for every engine that can run locally.
2. Engine adapters, each implementing every stage, each with a sample
   dataset:
    - PostgreSQL, MySQL, SQL Server and Oracle;
    - MongoDB and Redis;
    - Elasticsearch and OpenSearch;
    - a `files` adapter, for file trees beside a database;
    - Salesforce, which reads a real org over its REST APIs. Its tests run
      against a fake org and recorded cassettes.
3. The core:
    - the spec and the catalog lock;
    - excludes and strict mode;
    - the subset planner, with both strategies;
    - Jailer import;
    - manifests and resumability;
    - channels and image summaries;
    - the JSON API and the CLI.
4. Anonymization as described in [anonymization](anonymization.md): faker
   (deterministic), hash, null, format-preserving, engine-native expressions,
   and hooks.
5. Verification: restore check, referential integrity, row counts, PII
   scan (of databases and of files), and native assertions.
6. Storage and replication adapters, for sourcing, cloning and replication:
   `filesystem`, `s3`, `btrfs` and `k8s-pv`, plus `docker-volume` for local
   and CI use.
7. Announce adapters, which tell consumers where they can reach their
   copies, as volumes or as connection parameters: `k8s-secret`,
   `ci-outputs`, `dotenv` and `vault`. A `k8s-pv` copy's PersistentVolume is
   its own announcement (decision 61).
8. Operations: `promote`, `fetch`, `replicate`, `claim`, `release`, `prune`
   and `activate`, in pool and on-demand modes.
9. Golden-image signing with cosign in the build, keyless or with a key pair,
   checked by `promote` and `fetch`.
10. Tests in containers under Docker Compose, with groups that can be mixed
    and matched.
11. GitHub CI that builds signed, attested runtime images (see [supply
    chain](#supply-chain)).
12. Tier 1 CI/CD libraries: GitHub Actions, GitLab CI, Jenkins and Argo CD.
13. All agent skills, including help with the `files` adapter, and the
    Claude Code plugin.
14. Two public resources:
    - a GitHub Pages documentation site at
      `https://datadouble.tightlinesoftware.com/`, built from the Markdown in
      `docs/` (see [documentation site](#documentation-site));
    - an example repository, `datadouble/example-pipeline`, covering both
      the production and the non-production side.

### Not in version 1

- **Recording Salesforce API cassettes.** The run builds the tooling for
  recording, scrubbing and replaying cassettes. The cassettes themselves are
  recorded afterwards against a development org, outside the run, so the run
  never holds org credentials.
- **ZFS and CSI-snapshot replication.**
- **Tier 2 CI/CD systems.**

## How the run works

The run works without a person watching every step, so it follows fixed
rules:

- **One branch and pull request per milestone,** with checkpoint commits. A
  pull request merges when its required checks pass. The run never pushes to
  `main` directly.
- **No releases.** The run never tags a release or publishes to PyPI. The
  first release is a maintainer's decision.
- **Changelog.** Every milestone pull request adds its user-visible changes
  to `CHANGELOG.md` under `[Unreleased]`. The first release's notes are then
  ready when it is cut.
- **Design changes go through the docs.** If implementation shows that a
  design document is wrong, the same pull request updates the document and
  records the decision.
- **Machine.** Running Oracle and SQL Server together needs roughly 6 GB of
  RAM or more, and the btrfs tests need privileged containers.
- **CI minutes.** Runs on hosted CI/CD services are scheduled nightly, not
  run on every push.

## Choices made for the run

These technical choices were settled before the run started, so it does not
have to revisit them:

- **Python 3.13 is the minimum version,** and CI tests every later release.
  The container images run Python 3.15 (decision 48).
- **Typing** follows [architecture](architecture.md#typing): `mypy --strict`,
  `pyright` in strict mode, and `ruff`, with no unexplained suppressions.
- **Anonymization runs inside the working database.** Faker generates seeded
  tables of values in the Datadouble process, and the engine applies them in
  native updates. There is no separate anonymization service.
- **One Datadouble image, with no engine software in it.** Engine servers and
  tools run in the vendors' own images, through a container runtime
  (decision 72). So Datadouble never redistributes Oracle's or Microsoft's
  software.
- **Sample data licences** are checked before a dataset is committed. Where
  a licence is unclear, the data is generated instead.
- **Copyright and licence:** every source file starts with SPDX headers (see
  [AGENTS.md](https://github.com/datadouble/datadouble/blob/main/AGENTS.md)).

## Milestones

The milestones are ordered so that the core is proven end to end on one
engine first (M1 and M2). Then it is tested on an engine of a different shape
(M3, MongoDB). Only after that does the run go for breadth.

### M0: Foundations

**Builds:** package scaffolding with strict typing, the Docker Compose
test environment, CI with the [supply-chain](#supply-chain) controls,
`TESTING.md`, the release script with its changelog gate, and the
documentation site with its Design section.

**Done when:**

- `docker compose up` passes a smoke test;
- typing and lint pass, with every suppression explained and counted;
- CI publishes a signed, attested image for the pull request;
- the site builds with no broken links and is live on its domain.

### M1: Core and PostgreSQL, end to end

**Builds:** the spec, the lock, excludes, strict mode, both planner
strategies, snapshot extract, load, anonymization, package, verify, store to
the filesystem and to S3, manifests, resume, the JSON API and the CLI.

**Done when:**

- Pagila runs from plan through store, and passes the engine conformance
  suite;
- a killed run resumes;
- both planner strategies produce identical row sets;
- the strict-mode cases behave as specified.

### M2: Non-production side

**Builds:** promote, the export store, image summaries, channels, image
signing, fetch, replicate (filesystem, btrfs, docker-volume and k8s-pv), the
announce adapters, pool and on-demand modes, release, prune and activate.

**Done when:**

- promote refuses unverified or unsigned images;
- fetch skips when nothing is newer;
- pool recycling never empties the pool;
- activate rolls back;
- prune never removes a channel's target.

### M3: MongoDB

**Done when:** conformance passes, with no engine-specific code in the core.

### M4: MySQL, SQL Server, Oracle

**Builds,** in five pull requests (decision 80): container runtimes, MySQL,
the Jailer front end, SQL Server, and Oracle.

**Done when:**

- each engine passes conformance;
- Jailer and Datadouble produce identical key sets on Sakila.

### M4.5: Performance tuning

**Builds:**

- profiles of builds and of the integration suite (`pytest --durations`);
- working databases sized so that empty engine space (MySQL's redo and undo
  logs, say) neither slows builds nor bloats physical images;
- integration tests that run in parallel;
- pulled vendor images kept between Compose runs.

**Done when:** each change is measured before and after, and the suite's
wall time and the physical image sizes are recorded in `TESTING.md`.

### M5: Redis, Elasticsearch, OpenSearch

**Done when:** each passes conformance, including strict mode on keys or
mapping fields.

### M6: Files

**Done when:**

- only files for subset rows are copied;
- an unmatched top-level directory fails the plan;
- a planted file of fake personal data fails verify.

### M7: Salesforce

**Builds:** the Salesforce adapter, which reads a real org over its REST APIs
with an instance URL and an access token. Its tests run against a fake org (a
SQLite file served over HTTP) and against cassettes recorded from a real org
(`make record-salesforce`), so they need no org. M7 also builds the tooling
for recording, scrubbing and replaying the cassettes.

**Done when:**

- conformance passes offline;
- the scrubber removes tokens, session IDs, instance URLs and org IDs from a
  synthetic recording.

### M8: CI/CD libraries

**Builds,** in five pull requests (decision 99):

1. GitHub Actions, with the shared CI/CD core and conformance suite;
2. GitLab CI;
3. Jenkins;
4. Argo CD;
5. the nightly hosted runs.

**Done when:** the CI/CD conformance suite passes for all four systems,
locally and in the nightly hosted runs.

### M9: Agent skills and plugin

**Done when:** `claude plugin validate` passes, and every skill scenario
holds, including the cases where a skill must refuse.

### M10: Using Datadouble documentation and example repository

**Builds:** the quickstart, the generated spec and CLI references, and the
guides to the CI/CD libraries and agent skills, added to the existing site.

**Done when:**

- the example repository's pipelines run green from a fresh clone, on both
  sides of the boundary;
- every spec field and CLI command appears in the generated references.

### M11: Source requirements

Decision 107. A source may depend on add-ons, such as PostgreSQL extensions,
that an image must also have.

**Builds,** in three pull requests:

1. the core, with PostgreSQL;
2. Redis, Elasticsearch, OpenSearch and MySQL;
3. SQL Server, Oracle and MongoDB.

Salesforce's managed packages are part of M7.

**Done when:**

- each engine reports its source's add-ons in the lock;
- a build against an image that lacks one fails before extract, naming it;
- PostgreSQL copies run the source's extension versions;
- the docs, the example repository and the skills cover it.

The milestones say what to build. The rest of this page records the
decisions the plan makes for particular areas, so the run doesn't have to
make them along the way.

## New adapters

Most adapters wrap engines that Datadouble's design already covered. Three
are new in shape, and need their own notes.

### Elasticsearch and OpenSearch

Elasticsearch and OpenSearch get separate adapters. Their APIs have diverged
since the fork, in security, snapshots and k-NN, and the official
Elasticsearch client refuses to talk to OpenSearch.

Neither engine has foreign keys, so the adapters work differently from the
relational ones:

- **Subsetting** is per-index queries, plus declared join keys.
- **Strict mode** applies to mapping fields.
- **A consistent view** across indices comes from point-in-time searches.

### Files

Many applications keep files beside their database, such as uploaded avatars
and documents. The `files` adapter copies those files, and only those that
belong to the rows in the database subset. A spec describes them in a `files`
section:

```yaml
files:
  connection:
    root: /srv/uploads
    accept_inconsistent: true
  select:                                   # find(1) tests per top-level directory
    avatars: -mtime -30
    documents: -false                       # only the files paths_from names
  paths_from:                               # follow the database subset
    table: customer_documents
    path: '{url}'                           # a template over the row's columns
    match: '^https://cdn[.]example[.]com/uploads/([^?]+)'   # group 1 is the path
  exclude: [tmp, cache]
  cleanup:                                  # shell, run on the copy only
    - find . -name '*.log' -delete
    - exiftool -all= -overwrite_original -r ./avatars
  image: vendor/exiftool:13                 # the image cleanup runs in
```

- **A second source (decision 97).** Each top-level directory is a table of
  files, keyed by their path. The files build shares the spec's generation,
  image groups and stores, with one image per group.
- **Consistency.** A consistent snapshot needs btrfs, ZFS or LVM. Elsewhere,
  extract fails unless the spec explicitly accepts an inconsistent copy, and
  the manifest records that it did. Version 1 takes no filesystem snapshots.
- **Strict mode.** Every top-level directory is either selected or
  explicitly excluded. A new directory fails the plan.
- **Following the database subset.** `paths_from` reads the column's values
  for the chosen rows, inside the database's snapshot, and those files are
  copied too. So only files for rows in the subset are copied.
- **Cleanup is not proof.** A files PII scan stays mandatory in `verify`.
- **Portability.** The `select` tests run with GNU find from the Datadouble
  image's pinned Debian base. The `datadouble-files` skill explains each `find` expression back in plain words, and dry-runs it
  against a sample directory before writing it into the spec.

## Sample datasets

Every adapter is tested against a sample dataset:

| Adapter | Dataset |
|---|---|
| PostgreSQL | Pagila |
| MySQL | Sakila (also the Jailer conformance test) |
| SQL Server | Sakila (decision 78) |
| Oracle | Sakila (decision 78) |
| MongoDB | Generated cinema database (`sample_mflix`'s licence is unclear) |
| Redis | Generated keyspace with every data type and key-encoded references |
| Elasticsearch, OpenSearch | Generated indices derived from Pagila, with join keys across indices |
| Files | Generated upload tree keyed by customer IDs in a PostgreSQL table, with EXIF metadata and a text file containing fake personal data |
| Salesforce | For tests, a fake org: a SQLite file of made-up records (`sample_org`), served over HTTP |

Every dataset includes the awkward cases listed in [local
development](local-development.md).

## Test environment

The tests run under Docker Compose. `compose.yaml` defines the services, and
the integration suite is split into groups, listed in
`tests/integration-groups.txt`, each with only the services it needs.

| Services | Used for | Notes |
|---|---|---|
| Test runner; PostgreSQL with Pagila; MongoDB; MySQL, SQL Server and Oracle Database Free with Sakila (and Jailer's export of it); Redis; single-node Elasticsearch and OpenSearch; S3 (SeaweedFS); Docker-in-Docker; OpenBao; an HTTPS export store | Core, engine, S3, Docker, HTTP and Vault tests (decisions 56, 64, 78) | The SQL Server and Oracle images are amd64-only |
| `btrfs` profile: a privileged test runner with a loop-mounted btrfs volume | The btrfs adapter | Needs privileged Docker |
| `k8s` profile: k3s in a container | The Kubernetes adapters: `k8s-pv`, `k8s-secret` and the `kubernetes` runtime (decision 64) | Needs privileged Docker |

Every service except the two profiles starts on plain `docker compose up`,
but you rarely want them all:

```bash
make test-group GROUP=mysql      # one group, with only its services
make test-k8s                    # the k8s profile
make test-btrfs                  # the btrfs profile
```

The CI/CD systems are not Compose services. `make test-ci SYSTEM=<name>` runs
each one in its own local runtime: `act`, GitLab CE, Jenkins, or a kind
cluster with Argo CD. [Local development](local-development.md) and [How our
CI works](how-our-ci-works.md) describe the suite in full.

## Documentation site

The site is built from the Markdown in `docs/`, which is the single source of
truth. No page is maintained as a separate copy.

| Section | Contents | Milestone |
|---|---|---|
| Home | The production-boundary diagram, overview and design principles | M0 |
| Design | Every design document, rendered as is, in reading order | M0 |
| Using Datadouble | Quickstart, spec reference generated from the JSON Schema, CLI reference generated from the CLI, extensions and plugins, CI/CD libraries, agent skills, the example repository | M10 |
| Project | Decisions, the v1 plan, external services, how our CI works, changelog, contributing | M0 |

- **Generator:** Zensical, at the version `uv.lock` holds (decisions 46 and
  136).
- **Theme:** the colors, type, and light and dark modes of the design book,
  with its production-boundary diagram as the home page figure.
- **Publishing:** a workflow builds the site on every pull request, and
  broken links fail the build. Merges to `main` deploy it to GitHub Pages.

## Supply chain

Datadouble handles production data, so the code and images that do it must
be trustworthy. The plan's controls:

- **Workflows:** every action is pinned by commit SHA,
  `step-security/harden-runner` runs on every job on GitHub-hosted runners,
  and workflow tokens are read-only by default.
- **Scanning:** CodeQL, dependency review, OpenSSF Scorecard, Snyk, SonarQube
  Cloud and Codecov.
- **SBOMs:** generated with Syft, with SBOM attestations and build provenance
  attestations.
- **Signing:** cosign keyless signing of every published image through
  GitHub OIDC, with documented `cosign verify` and `gh attestation verify`
  commands.
- **Images:** an image per pull request for testing. Release images are built
  only from tags.
- **Packages:** PyPI trusted publishing, with attestations for every package.

Account setup for these services is in [external
services](external-services.md).
