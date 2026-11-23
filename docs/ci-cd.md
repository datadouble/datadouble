# CI/CD integration

Datadouble runs mostly inside other people's pipelines. Given a spec, it must be
quick to wire any part of the lifecycle into the CI/CD system a team already
uses, and every integration must be tested as thoroughly as an engine adapter.

## Jobs

The lifecycle splits into jobs. Each is a fixed group of pipeline stages or
operations, runs in a known place, and is usually triggered on its own schedule:

| Job | Stages / operation | Runs in | Typical trigger |
|---|---|---|---|
| **build** | plan → extract → load → transform → package → verify → store | Production | Schedule (e.g. weekly) |
| **promote** | promote | Production | After a successful build |
| **fetch** | fetch | Non-production | Schedule (cheap: reads the channel and the image summary, not the image) |
| **provision** | replicate → announce | Non-production | Schedule (pool mode) or a request such as a pull request (on-demand mode) |
| **release** | release | Non-production | Pull request closed, environment stopped, or schedule |
| **prune** | prune | Each environment | Schedule |
| **activate** | activate | Non-production | Manual (rollback) |
| **lock** | lock (re-lock the catalog for review) | Production | Manual, after a schema change |

`replicate` and `announce` are separate stages because announce targets vary
independently of replication, but they normally run as one job.

### The production boundary

Raw extract artifacts and the working database contain real personal data.
Everything up to and including `verify` therefore runs in the same environment
as `extract`. The only thing that leaves production is a golden image.

`promote` is the gate. It runs in production and copies an image into an
**export store**, the only storage non-production can read. It refuses an image
unless its manifest records:

- A passing `verify` report, with no checks skipped
- A strict-mode level at least as strict as the export store requires (default
  `columns`)
- Content hashes that match the stored artifacts

`fetch` then pulls from the export store with read-only credentials for that
store alone. Non-production never holds credentials for source systems,
production scratch space or production storage. A push-based variant (production
writes directly into non-production storage) is a storage adapter choice; the
gate is the same.

### Export stores

An export store is any store adapter: a directory (`filesystem`), a bucket
(`s3`), or, for `fetch` only, a web server (`http`). The `http` store suits a
production side that already publishes files through a web server
non-production can reach over HTTPS but can neither mount nor call an S3 API
on. It is read-only: `promote` writes with the filesystem or S3 store into
the directory the server serves, and `fetch` reads through the server, with
basic auth or a bearer token given as secret references, and optionally its
own CA bundle. Any write through it fails, naming the store. See the
[adapter's README](https://github.com/datadouble/datadouble/tree/main/adapters/http).

Web servers have no standard listing, and `fetch` lists the store to find
each group's engine versions. So `promote` writes an **index** under each
`<spec>/<group>/` (`index.json`) on every export store, naming the group's
image lines and the generations with a summary in each, and rewrites it
after each image's summary and channel; `prune --target export` rewrites it
to match what is left, or removes it when nothing is. A store that cannot
list keys reads the index instead: `fetch` takes only its line names, then
reads the channel and summary as with any store, so the `http` store needs
nothing server-specific. Everything else in the store's layout is unchanged.
`promote` rebuilds the index by listing each export store, so an S3 export
store needs `s3:ListBucket` for `promote`'s credentials.

### The lock job

Strict mode refuses to plan without a catalog lock, and `datadouble lock`
reads the source, so it runs only inside the boundary. Where the only machine
there is a CI runner, the manual `lock` job (`ci init --job lock`) re-locks:

- It runs on the production side, on the same `--runner` as `build`, with
  only the secrets the spec's `source` section names. It is refused on a
  vendor's hosted runner, like `build`.
- It has no schedule: someone always means to re-lock.
- It runs `datadouble lock --spec <spec> --output datadouble-lock`, which
  writes the new lock (`catalog.lock`) and its changes against the committed
  lock (`changes.txt`, the list the CLI prints) into that workspace
  directory, leaving the committed lock as it is.
- It publishes that directory as the CI system's job artifact: a GitHub
  artifact, a GitLab job artifact, a Jenkins archived artifact. Kubernetes
  keeps no job artifacts, so on Argo CD the job prints both files to its log,
  and its termination message carries the outputs (`fingerprint`, and
  `changes`, the number of changes).
- It never commits. A person reviews the changes, checks every new or
  changed column for personal data, and commits the lock. That review is the
  point of strict mode, so no job does it for them.

A lock holds table, column and key names, engine type names and requirement
versions, never values, so publishing it as a CI artifact is consistent with
[no data in CI storage](#no-data-in-ci-storage) (decision 123). Type names
can carry schema literals, such as a MySQL `enum` column's labels; those are
part of the schema and already in the committed lock.

## Stages and operations

The CI/CD work extends the [architecture](architecture.md) pipeline to:

```
plan → extract → load → transform → package → verify → store → promote → fetch → replicate → announce
```

plus three operations that act on existing images and copies: `release`,
`prune` and `activate`.

### build

`verify` runs after `package`, against a scratch restore of the packaged image,
so verification covers the artifact that actually ships, including whether it
restores at all. Its checks include referential integrity, row counts, PII
scans and user-supplied SQL assertions.

A spec may declare SQL **hooks**: `before_load` (server settings, extensions)
and `after_transform` (seed data developers need, such as test accounts). Hooks
run before `verify`, so their effects are verified too. See
[anonymization](anonymization.md#hooks).

A run ends in one of three outcomes:

| Outcome | Meaning | Example |
|---|---|---|
| success | Image built and verified | |
| warning | Image built and verified, with something an operator should fix | Source server version newer than the load server |
| failure | No image stored | Any verification failure, any strict-mode violation |

Anything that could put real personal data into a copy is always a failure,
never a warning. CI/CD adapters map warnings to the system's native state
(Jenkins `UNSTABLE`, a GitHub Actions warning annotation, a distinct exit code
that GitLab `allow_failure: exit_codes` recognizes).

### Image identity, channels and image summaries

An image is identified by spec name, image group (see
[golden images](architecture.md#golden-images)), engine, the engine's
**compatibility line** and a **generation** (a unique ID assigned at build,
shared by every image from that build). Consumers usually select by engine and
line, since a copy must be started by a compatible server.

The line is the part of the server version that decides which servers can
open a copy, and each engine adapter says how much of the version that is
(decision 120): the major version for PostgreSQL (`17`), SQL Server (`16`,
whose images are tagged by year, `2022`) and Oracle (`23`); the major and
minor for MySQL (`8.4`, since 8.0 cannot open what 8.4 wrote), MongoDB
(`8.0`), Redis (`7.4`, each minor writing its own RDB version), and
Elasticsearch and OpenSearch (`8.15`, since a snapshot restores only on the
same or a later minor). A build uses the source's exact version, so when the
source is upgraded within a line, the next image simply joins it; an upgrade
to a new line starts a new series of images. The image summary records the
exact version too (`server_version`).

A consumer serving a copy needs a server of the copy's line, at least as new
as the copy's `server_version`: the newest release of the line opens what
older ones wrote. Where the vendor publishes a tag for the line (`postgres:17`,
`mysql:8.4`, `redis:7.4`, `mongo:8.0`, `opensearchproject/opensearch:2`), that
tag does; elsewhere, use the newest exact release. Oracle copies are the
exception: a pluggable database plugs cleanly only into the release update
that wrote it, so serve them with exactly `server_version`.

Every store holds, next to each image, a small **image summary**: a few
kilobytes with the image's identity, build time, content hashes, verification
result and signature reference, copied from its full manifest (which travels
inside the image). It is read without downloading the image:

```
export-store/shop/billing/
  index.json                           # image lines and generations, for stores that cannot list
  postgres-18/
    0193a7c21b4e9f30.physical.tar.gz   # one archive per format, possibly many GB
    0193a7c21b4e9f30.logical.tar.gz
    0193a7c21b4e9f30.verify.json       # the full verification report
    0193a7c21b4e9f30.summary.json      # the image summary, written last
    channels/latest                    # points at generation 0193a7c21b4e9f30
```

`fetch` reads the channel and the image summary to decide whether anything newer
exists, so running it every few minutes is cheap; it downloads an image only
when there is one, then checks its signature (decision 58) and the summary's
hashes.

Images are addressed through **channels**: named, movable pointers such as
`latest`. `promote` advances a channel in the export store; `fetch` advances one
in staging. Test pipelines use their own channel (or their own store), so
experimenting with a spec never replaces the image live copies are made from.

### fetch

`fetch` pulls an image from the export store into non-production **staging**,
checks its hashes, unpacks it into the form `replicate` copies from (for example
a ready-to-mount data directory), and advances the staging channel. It skips
work when the image summary shows nothing newer than what is already staged.

An image being written to staging is locked until complete, so `replicate`
never copies a partial image. Staging keeps the last N images, so `activate` can
move the channel back to a previous generation if a new image turns out to be
bad.

### replicate

`replicate` has two modes:

| Mode | Behavior | Suits |
|---|---|---|
| **pool** | Keep N unclaimed copies of the active generation ready; consumers claim one | Large images, where copying takes longer than a developer will wait |
| **on-demand** | Make N copies for a named requester (pipeline, merge request, environment); asking again makes none until some are released | Small images, CI test jobs |

Copies are written under a temporary name and renamed when complete. A copy
that never completed is removed by `release`.

A copy is one of two kinds, and the replicate adapter declares which it
produces:

- **Volume:** a data directory the consumer starts its own server on (Docker
  volume, Kubernetes PersistentVolume, ZFS clone).
  `package.accounts` gives such copies accounts that log in over the network
  (decision 121, [Accounts in physical images](architecture.md#accounts-in-physical-images));
  without them, a server started on one has only its local accounts.
- **Instance:** a running server with an endpoint (host, port, database, and a
  reference to credentials).

| Replicate adapter | Copies | Claims |
|---|---|---|
| filesystem | A directory, with shared blocks where the filesystem allows | `datadouble claim` |
| btrfs | A subvolume of reflinked files (decision 63) | `datadouble claim` |
| docker-volume | A Docker volume, or a container running the engine's server on one (decision 62) | `datadouble claim` |
| k8s-pv | A directory and a PersistentVolume for it | A bound claim; a released volume is removed and replaced |

In pool mode, consumers outside Kubernetes take a copy with `datadouble claim
--owner NAME` and give it back with `datadouble release --owner NAME`
(decision 60).

### announce

`announce` lets consumers know where database clones can be accessed, either as volumes or as connection parameters. It never writes data: `replicate` has already made the
clone, and `announce` only records where it is and how to reach it. Every clone
carries labels (spec, image group, engine, compatibility line, channel, owner) and
provenance (image ID, generation, build time).

- **Volume clones** are announced as a volume consumers can claim and start
  their own server on.
- **Instance clones** (a running server) are announced as connection
  parameters: host, port, database, and the clone's own credentials.

| Announce adapter | Writes | For |
|---|---|---|
| k8s-secret | A Secret per copy, with its location and, for instances, credentials | Instance copies on Kubernetes |
| ci-outputs | Job outputs of the running CI system: `GITHUB_OUTPUT`, a GitLab dotenv report, or a JSON file | On-demand copies in CI |
| vault | A Vault (or OpenBao) KV v2 secret per copy | Any |
| dotenv | A dotenv file, readable only by its owner | Local and simple CI use |

On Kubernetes, a `k8s-pv` copy's PersistentVolume, labelled with the copy's
spec, group, engine line (`engine-version`), exact server version
(`server-version`) and generation, is itself the announcement consumers match
in a PersistentVolumeClaim selector (decision 61).

A clone's credentials go only to secret-capable targets (`k8s-secret`,
`vault`, `dotenv`). `ci-outputs` announces the endpoint without them, because
job outputs on most systems are not secret.

### release

- **On-demand copies** are released by copy ID or owner (the normal teardown
  path), or by `--expired` for a scheduled sweep of copies whose teardown never
  ran.
- **Pool copies** are recycled by generation. When another generation becomes
  active (a newer one, or an older one after `activate`), unclaimed copies of
  other generations are replaced one for one as new copies are made, so the
  pool never empties. An all-at-once mode (`--all-at-once`) exists for urgent
  replacement. Claimed copies belong to their consumer (for Kubernetes,
  a `Delete` reclaim policy removes the volume when the claim is deleted).
- Incomplete copies are always removed.

### prune

`prune` applies retention to images in any store and in staging: keep the last
N generations and anything younger than M days, and never remove the image a
channel points to. Retention is also applied by compatibility line: once a
newer line is in use, older lines' images, staging and pools can be removed,
keeping the K most recent lines. `prune` supports `--dry-run` and
reports the space it would recover.

## CI/CD adapters

A CI/CD adapter contains **no pipeline logic**. Like the CLI, it only builds JSON
requests and maps the CI system's conventions onto them. It has three parts:

1. **Distribution:** the system's native packaging, so a pipeline references a
   versioned Datadouble step rather than a shell script.
2. **Generation:** `datadouble ci init` writes pipeline configuration for one
   or more jobs and a spec, ready to commit.
3. **Runtime bridging:** secrets, outputs, outcomes, log masking, summaries and
   concurrency, translated between the CI system and Datadouble's JSON interface.

### Generation

```
datadouble ci init --system github-actions --job build,promote,prune,lock \
    --spec datadouble/shop.yml --runner prod-db --work /srv/datadouble/work
datadouble ci init --system github-actions --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner staging
```

The equivalent JSON request:

```json
{"command": "ci.init", "system": "github-actions", "jobs": ["build", "promote", "prune"],
 "spec": "datadouble/shop.yml", "runner": "prod-db", "work": "/srv/datadouble/work"}
```

The response lists the files written and the environment variables the jobs
read secrets from. One call writes one side of the boundary: mixing
production and non-production jobs fails, and `prune` alone needs `--side`.

The core decides what every job runs (decision 100): its `datadouble`
command lines, triggers, schedules and parameters, which secrets it reads and
which host paths it needs. The adapter only writes that in its system's form,
so the jobs are the same on every system. Generated configuration:

- Pins the Datadouble version: the native step at that release's tag, and
  the release's image.
- References secrets by name only. Each job gets only the secrets its own
  spec sections name (`build` the source's, the build store's and the
  password references of `package.accounts`, `fetch` the trust
  settings'), plus `COSIGN_PASSWORD` for a build signed with a key, and
  any `--env NAME` given for credentials adapters read from the
  environment, such as `AWS_ACCESS_KEY_ID`.
- Includes schedules, in UTC, and job chaining: `promote` after a successful
  `build`, and a pool's `provision` after `fetch`.
- Includes the system's concurrency controls, per job, and per request for
  on-demand copies.
- Exposes run-time switches as job parameters: `--dry-run` for `prune`, and
  the generation to `activate`.
- Passes the CI system's run ID as `--retry-key` to `build`, so retrying a
  failed run resumes the generation it started (decision 100).
- Publishes the `lock` job's new lock and its changes as the job's artifact
  (see [the lock job](#the-lock-job)); no other job publishes files.
- Makes on-demand copies when a pull or merge request opens, for
  `pr-<number>`, and releases them when it closes. Requests from forks never
  run on the team's runners.

The mode, pool or on-demand, comes from the spec's `replicate` section.
Generating `build`, `promote` or `lock` requires an explicit runner target,
and `build` an absolute work directory (see [where jobs run](#where-jobs-run)).

### Supported systems

**Tier 1**, shipped and tested from the first CI/CD release:

| System | Native form | Outputs | Secrets | Teardown hook |
|---|---|---|---|---|
| GitHub Actions | A workflow per job, using one action in this repository (`datadouble/datadouble/ci/github-actions@v1`) that runs the Datadouble image | `$GITHUB_OUTPUT`, `$GITHUB_STEP_SUMMARY` | Repository/environment secrets, OIDC to cloud storage and for keyless signing | `pull_request: closed` workflow |
| GitLab CI | An included file of jobs extending a job template that this repository publishes at each release's tag, pinned by its SHA-256 (`include: remote` with `integrity`); CI/CD Catalog components once the GitLab mirror exists | dotenv report artifacts | CI/CD variables, `id_tokens` OIDC | Environments with `on_stop` |
| Jenkins | A declarative Jenkinsfile per job, loading this repository's shared library at the release's tag and calling its one `datadouble` step; `activate` takes the generation as a parameter | Step return values, the build's description | Credentials binding | A request's own pipeline starts `release` with its `OWNER`, for example in `post { cleanup }` |
| Argo CD | An Application per side, installing this repository's Helm chart at the release's tag: a CronJob per job, a following job also running as a later step of its leader's pod; PreSync hook Job for on-demand provision; PostDelete hook Job for release; ApplicationSet example with the pull-request generator | Kubernetes PersistentVolume or Secret via `k8s-pv` / `k8s-secret`; containers' termination messages | Kubernetes Secrets, External Secrets | PostDelete hook |

The Kubernetes pieces (container image, Job templates, RBAC) form a shared base
that Argo CD uses directly and that the Tier 2 Kubernetes systems build on.

**Tier 2**, after Tier 1 is stable: Argo Workflows, Tekton, Flux, Azure
Pipelines, CircleCI, Bitbucket Pipelines and Buildkite.

GitLab's CI/CD Catalog needs the components to live in a GitLab project, so they
will be published from a GitLab mirror of this repository. Until that project
exists, generated jobs include the job template straight from this repository
(decision 101). Self-managed GitLab users can mirror the file or the project.

Some setup cannot live in a system's configuration: GitLab's pipeline
schedules, for one. `ci init` lists those steps in its response
(`instructions`), and a GitLab schedule names the job it runs in the
`DATADOUBLE_JOB` variable.

### Runtime bridging

The core receives the CI system as an explicit `--ci <system>` (set by the
native steps; `ci` in a JSON request), not by auto-detection. The CI/CD
adapter then:

- Registers every secret value Datadouble resolves with the system's log
  masking, where the system has it (GitHub's `add-mask`). Secrets reach
  Datadouble as environment variables, set from the system's own secrets.
- Writes the JSON response's outputs as native job outputs (`outcome`, the
  `generation` a run built, the `error` code of a failure), maps the outcome
  (success, warning, failure) to the native build state, and writes the
  summary the CLI prints where the system has one, so the system's own
  notifications report it. GitHub Actions has no warning state, so a warning
  is a passing step with a warning annotation.
- Emits concurrency controls in generated configuration (GitHub `concurrency`,
  GitLab `resource_group`, Jenkins `disableConcurrentBuilds` / `lock`). The core
  also takes locks in storage and staging, because different systems may run
  jobs for the same spec.

There is no continue-on-error switch. A failed stage stops the job.

### No data in CI storage

CI systems' own artifact and cache storage is often outside production (GitHub's
artifact storage is on github.com, for example). CI/CD adapters never upload
extract artifacts, working databases or golden images there. Resumability
([decision 7](decisions.md)) for `build` uses a persistent work directory in
production scratch space, named when the jobs are generated (`--work`), not the
CI workspace.

Manifests and reports may be uploaded as CI artifacts, so they must never
contain data values. In particular, `pii-scan` reports name columns and
detector types, never the matched values. The `lock` job's catalog lock and
its changes are published this way: they hold names, types, keys and
requirement versions, never values (decision 123).

## Where jobs run

`build`, `promote` and `lock` must run on infrastructure inside the
production boundary: self-hosted GitHub runners (or Actions Runner Controller), GitLab
runners selected by tag, Jenkins agents selected by label, or a Kubernetes
cluster in production.

- **Runners are marked inside the boundary.** Reading a spec's source (`lock`,
  `plan`, and every `run` that plans or extracts) and `promote` refuse to run
  unless the machine's environment sets `DATADOUBLE_BOUNDARY=inside`, with the
  error `outside_boundary` (decision 98). The operator sets it once, where the
  runner is configured: a self-hosted GitHub runner's `.env`, a GitLab
  runner's `environment`, a Jenkins agent's environment, or the pod template of
  Datadouble's Kubernetes jobs. A machine nobody marked, such as a vendor's
  hosted runner or an unfamiliar CI system, is refused before anything
  connects.
- `datadouble ci init --job build` (or `promote`, or `lock`) fails without
  `--runner`. It never emits a vendor-hosted runner such as `ubuntu-latest`.
- At run time, reading the source and `promote` also fail if any installed
  CI/CD adapter recognizes a vendor-hosted runner (for example, GitHub's
  `RUNNER_ENVIRONMENT=github-hosted`), even one marked inside.
- `build` runs engine software in containers (decision 72), so its runner
  needs the spec's container runtime: a Docker-compatible daemon that sees
  the work directory at the same path, or, for `kubernetes`, a pod whose work
  directory is on the PersistentVolumeClaim the engine pods share.

Non-production jobs get separate generated configuration and separate secrets.
Generation never puts production credentials into a non-production job.

### How a job runs Datadouble

Native steps run the pinned Datadouble image with the runner's Docker daemon
(decision 100). The image runs as the engine's user (decision 86), on the
host's network, so it reaches the source and the working servers' published
ports as the runner would. The workspace, and every absolute path the job's
spec sections name (stores, staging, copies, a Unix socket directory, a
secret file, the work directory), are mounted at the same path, because the engine containers
Datadouble starts mount them by those paths. A job's runner therefore needs
Docker, with those paths owned by the engine's user.

## Testing

Each CI/CD adapter is tested at three levels.

**1. Generated configuration.** Golden-file tests for every system × job
combination, plus validation with each system's own tooling:

| System | Validator |
|---|---|
| GitHub Actions | `actionlint` |
| GitLab CI | CI Lint API against the containerized GitLab below |
| Jenkins | Declarative pipeline validator in the Jenkins container; the conformance suite runs the shared library itself |
| Argo CD / Kubernetes | `helm lint`, `kubeconform` |

**2. Local end-to-end.** Each system runs locally, and runs every job, as
`ci init` wrote it, against a small shop database in PostgreSQL, with the
Datadouble image built from the checkout (`make test-ci SYSTEM=<name>`):

| System | Local runtime |
|---|---|
| GitHub Actions | `act`, running jobs on the host as a self-hosted runner does |
| GitLab CI | GitLab CE and a GitLab Runner with the shell executor, in containers |
| Jenkins | `jenkins/jenkins` with pinned plugins, configured with Configuration as Code, its built-in node running jobs with the host's Docker daemon |
| Argo CD | kind cluster with Argo CD's core install and a Helm repository serving the checkout's chart |

The container-based systems' profiles live under `dev/ci/`; they are heavy and
opt-in (`docker compose --profile ci-gitlab up`).

**3. Scheduled runs on hosted services.** GitHub Actions on this repository and
a GitLab.com project, nightly (the GitLab run waits for that project; see
decision 99). They use self-hosted runners started by the test, so the
hosted-runner refusal in `build` is exercised exactly as in real use. This
catches API and runner changes that local runtimes like `act` don't
reproduce.

On GitHub (`.github/workflows/nightly.yml`, decision 104), a job on a
GitHub-hosted machine starts a source database and a self-hosted runner on
that machine, marked inside the boundary, then runs the workflows `ci init`
writes for `tests/nightly/nightly.yml` in order: build, promote by
`workflow_run`, fetch, provision by `workflow_run`, and prune on both sides.
Those workflows are committed, since GitHub runs only workflows in the
repository; `scripts/nightly.py` writes them, using the checkout's action and
without schedules, and `make lint-workflows` fails if they are stale. The
build signs without keys through GitHub's identity token, and the job checks
that the hosted machine itself is refused even when marked inside.

### CI/CD conformance suite

Every CI/CD adapter must pass a shared suite, `datadouble_conformance.ci` in
the conformance package. The adapter's tests supply a harness that runs one
generated job in the system's local runtime; each test starts from a fresh
source, stores and repository, with jobs `ci init` wrote for both sides:

- build → promote → fetch → provision completes in both pool and on-demand
  modes, and each job's native outputs and build state match its JSON response.
- A warning outcome maps to the native warning state; a failure stores nothing.
- A known test secret never appears in the job logs.
- `promote` refuses when verification failed.
- `build` refuses to generate or run on a vendor-hosted runner, or on a runner
  nobody marked inside the boundary.
- A `build` that fails part way resumes from the last completed stage when the
  run is retried.
- `fetch` skips the download when nothing newer exists.
- `activate` rolls the staging channel back, and new copies come from the
  chosen generation.
- `release` removes every on-demand copy `provision` created when the request
  closes, and its scheduled sweep runs.
- `prune` never removes an image a channel points to.
- No data artifact is uploaded to the CI system's artifact storage.
- `lock` publishes the new lock and its changes (in the job's artifact, or on
  Argo CD its log), leaves the committed lock as it is, and is refused on a
  vendor-hosted runner.

The core's own tests cover what needs a clock or a race rather than a CI
system: `release --expired` collecting a copy past its expiry, pool recycling
across generations, and `replicate` never copying an image `fetch` is still
writing.
