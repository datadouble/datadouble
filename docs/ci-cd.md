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
[golden images](architecture.md#golden-images)), engine, target engine version
and a **generation** (a unique ID assigned at build, shared by every image from
that build). Consumers usually select by
engine and engine version, since a copy must be started by a compatible server.

Every store holds, next to each image, a small **image summary**: a few
kilobytes with the image's identity, build time, content hashes, verification
result and signature reference, copied from its full manifest (which travels
inside the image). It is read without downloading the image:

```
export-store/shop/billing/postgres-18/
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
| **on-demand** | Make N copies for a named requester (pipeline, merge request, environment) | Small images, CI test jobs |

Copies are written under a temporary name and renamed when complete. A copy
that never completed is removed by `release`.

A copy is one of two kinds, and the replicate adapter declares which it
produces:

- **Volume:** a data directory the consumer starts its own server on (Docker
  volume, Kubernetes PersistentVolume, ZFS clone).
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
carries labels (spec, image group, engine, engine version, channel, owner) and
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
spec, group, engine version and generation, is itself the announcement
consumers match in a PersistentVolumeClaim selector (decision 61).

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
channel points to. Retention is also applied by engine version: once a newer
target engine version is in use, older versions' images, staging and pools can
be removed, keeping the K most recent versions. `prune` supports `--dry-run` and
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
datadouble ci init --system jenkins --job build,promote --spec prod.yml --runner prod-db
datadouble ci init --system jenkins --job fetch,provision,release,prune,activate --spec prod.yml
datadouble ci init --system gitlab --job provision,release --mode on-demand --spec prod.yml
```

The equivalent JSON request:

```json
{"command": "ci.init", "system": "jenkins", "jobs": ["build", "promote"],
 "spec": "prod.yml", "runner": "prod-db"}
```

The response lists the files written. Generated configuration:

- Pins the Datadouble version
- References secrets by name only
- Includes schedules and job chaining (for example `promote` after a successful
  `build`), with time zones stated explicitly
- Includes the system's concurrency controls
- Exposes run-time switches as job parameters: debug output, keeping the
  scratch work directory for inspection (it stays in production), dry runs for
  `prune`, and the channel to use

Generating `build` requires an explicit runner target (see
[where jobs run](#where-jobs-run)).

### Supported systems

**Tier 1**, shipped and tested from the first CI/CD release:

| System | Native form | Outputs | Secrets | Teardown hook |
|---|---|---|---|---|
| GitHub Actions | Container actions in this repository (`datadouble/datadouble/actions/build@v1`, one per job) and reusable workflows | `$GITHUB_OUTPUT`, `$GITHUB_STEP_SUMMARY` | Repository/environment secrets, OIDC to cloud storage | `pull_request: closed` workflow |
| GitLab CI | CI/CD Catalog components with `spec:inputs`, one per job | dotenv report artifacts | CI/CD variables, `id_tokens` OIDC | Environments with `on_stop` |
| Jenkins | Shared library with one step per job (`datadoubleBuild(spec: ...)`); `activate` uses an `input` step to choose the generation | Step return values, env vars | Credentials binding | `post { cleanup }` or a branch-deletion job |
| Argo CD | Helm chart: CronJobs for build, promote, fetch, pool provisioning and prune; PreSync hook Job for on-demand provision; PostDelete hook Job for release; ApplicationSet example with the pull-request generator | Kubernetes PersistentVolume or Secret via `k8s-pv` / `k8s-secret` | Kubernetes Secrets, External Secrets | PostDelete hook |

The Kubernetes pieces (container image, Job templates, RBAC) form a shared base
that Argo CD uses directly and that the Tier 2 Kubernetes systems build on.

**Tier 2**, after Tier 1 is stable: Argo Workflows, Tekton, Flux, Azure
Pipelines, CircleCI, Bitbucket Pipelines and Buildkite.

GitLab's CI/CD Catalog needs the components to live in a GitLab project, so they
are published from a GitLab mirror of this repository. Self-managed GitLab users
can mirror that project.

### Runtime bridging

The core receives the CI system as an explicit `--ci <system>` (set by generated
configuration and the native steps), not by auto-detection. The CI/CD adapter
then:

- Resolves secret references through the system's mechanism and registers every
  resolved value with the system's log masking.
- Writes the JSON response's outputs as native job outputs, maps the outcome
  (success, warning, failure) to the native build state, and writes a short
  summary (image ID, generation, row counts, verification result, copies made
  or released) where the system has one, so the system's own notifications
  report it.
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
production scratch space, named in the spec, not the CI workspace.

Manifests and reports may be uploaded as CI artifacts, so they must never
contain data values. In particular, `pii-scan` reports name columns and
detector types, never the matched values.

## Where jobs run

`build` and `promote` must run on infrastructure inside the production
boundary: self-hosted GitHub runners (or Actions Runner Controller), GitLab
runners selected by tag, Jenkins agents selected by label, or a Kubernetes
cluster in production.

- `datadouble ci init --job build` fails without `--runner`. It never emits a
  vendor-hosted runner such as `ubuntu-latest`.
- At run time, `build` fails if the CI/CD adapter can identify a vendor-hosted
  runner (for example, GitHub's `RUNNER_ENVIRONMENT=github-hosted`).
- `build` runs engine software in containers (decision 72), so its runner
  needs the spec's container runtime: a Docker-compatible daemon that sees
  the work directory at the same path, or, for `kubernetes`, a pod whose work
  directory is on the PersistentVolumeClaim the engine pods share.

Non-production jobs get separate generated configuration and separate secrets.
Generation never puts production credentials into a non-production job.

## Testing

Each CI/CD adapter is tested at three levels.

**1. Generated configuration.** Golden-file tests for every system × job
combination, plus validation with each system's own tooling:

| System | Validator |
|---|---|
| GitHub Actions | `actionlint` |
| GitLab CI | CI Lint API against the containerized GitLab below |
| Jenkins | Declarative pipeline validator in the Jenkins container; shared-library unit tests with JenkinsPipelineUnit |
| Argo CD / Kubernetes | `helm lint`, `kubeconform` |

**2. Local end-to-end.** Each system runs in containers under `dev/ci/`,
alongside the PostgreSQL dev profile, and runs every job against the Pagila
sample:

| System | Local runtime |
|---|---|
| GitHub Actions | `act` with a self-hosted-style runner image |
| GitLab CI | GitLab CE and GitLab Runner containers |
| Jenkins | `jenkins/jenkins` configured with Configuration as Code |
| Argo CD | kind cluster with Argo CD installed |

These profiles are heavy and opt-in (`docker compose --profile ci-gitlab up`).

**3. Scheduled runs on hosted services.** GitHub Actions on this repository and
a GitLab.com project, nightly. They use ephemeral self-hosted runners started by
the test, so the hosted-runner refusal in `build` is exercised exactly as in
real use. This catches API and runner changes that local runtimes like `act`
don't reproduce.

### CI/CD conformance suite

Every CI/CD adapter must pass a shared suite under `conformance/ci/`:

- build → promote → fetch → provision completes in both pool and on-demand
  modes, and each job's native outputs and build state match its JSON response.
- A warning outcome maps to the native warning state; a failure stores nothing.
- A known test secret never appears in the job logs.
- `promote` refuses an image whose verification failed or was skipped.
- `build` refuses to generate or run on a vendor-hosted runner.
- A `build` killed mid-run resumes from the last completed stage on retry.
- `fetch` skips the download when nothing newer exists, and `replicate` never
  copies an image `fetch` is still writing.
- `activate` rolls the staging channel back, and new copies come from the
  chosen generation.
- `release` removes every on-demand copy `provision` created, `release
  --expired` collects a copy whose teardown was skipped, and pool recycling
  replaces old-generation copies without emptying the pool.
- `prune` never removes an image a channel points to.
- No data artifact is uploaded to the CI system's artifact storage.
