# CI/CD integration

Most teams will run Datadouble from a CI/CD system they already use, such as
GitHub Actions, GitLab CI, Jenkins or Argo CD. A weekly job builds a new golden
image, another job copies it out of production, and other jobs hand copies to
developers and test pipelines. So Datadouble has to be quick to wire into
whatever system a team has, starting from nothing but a spec. And every
integration has to be tested as thoroughly as an engine adapter, because a
mistake in one can leak personal data.

One idea shapes all of this: the **production boundary**. Real personal data
must stay inside production, but the copies Datadouble makes are used outside
it. This page explains that boundary first, then the design built around it,
in this order:

1. [The production boundary](#the-production-boundary): what may cross it, and
   why that decides how the work is split.
2. [Jobs](#jobs): the units of work a CI/CD system runs, and which side of the
   boundary each one runs on.
3. [Images, generations and channels](#images-generations-and-channels): how
   an image is named and found, which every later job relies on.
4. [The production side](#the-production-side): building an image, promoting
   it across the boundary, and re-locking the catalog.
5. [The non-production side](#the-non-production-side): fetching an image and
   making copies of it.
6. [Operations on images and copies](#operations-on-images-and-copies):
   release, prune and activate.
7. [CI/CD adapters](#ci-cd-adapters): how Datadouble writes jobs for each
   system and connects to it while they run.
8. [Where jobs run](#where-jobs-run): the machines jobs run on, and how the
   boundary is enforced there.
9. [Testing](#testing): how every adapter is tested.

For step-by-step instructions, see [CI/CD libraries](using/ci-cd-libraries.md).

## The production boundary {#the-production-boundary}

Building an image means handling real personal data. Extract copies rows out
of the source, and the working database holds them until they are anonymized.
Both the raw extract artifacts and the working database contain real
personal data.

So Datadouble draws a line around production, called the **production
boundary**:

- Everything up to and including `verify` runs in the same environment as
  `extract`, **inside** the boundary.
- The only thing that ever leaves production is a **golden image**: the
  packaged, anonymized and verified result of a build.
- Machines **outside** the boundary (staging, test and developer environments,
  called non-production here) never hold credentials for source systems,
  production scratch space or production storage.

This page calls the two halves the **production side** and the
**non-production side**. The rule has consequences for almost everything that
follows:

- The work splits into jobs that each run on one side only
  ([jobs](#jobs)).
- One stage, `promote`, is the gate an image passes through to leave
  production ([promote](#promote)).
- The non-production side reads images from a single place, the export
  store, with read-only credentials ([export stores](#export-stores)).
- Each side gets its own generated configuration and its own secrets
  ([generation](#generation)).
- A CI system's own artifact storage never receives data
  ([no data in CI storage](#no-data-in-ci-storage)).
- Production jobs refuse to run on a machine nobody has marked as inside the
  boundary ([where jobs run](#where-jobs-run)).

## Jobs {#jobs}

The [architecture](architecture.md) describes Datadouble's work as a pipeline
of stages. For CI/CD, the pipeline runs all the way to the copies people use:

```
plan → extract → load → transform → package → verify → store → promote → fetch → replicate → announce
```

Three more **operations** act on images and copies that already exist:
`release`, `prune` and `activate`.

A CI/CD system doesn't run that pipeline in one go. Building an image takes
hours and happens weekly, while checking for a new one is cheap and can happen
every few minutes. So the lifecycle splits into **jobs**. Each job is a fixed
group of stages or operations. It runs in a known place, and is usually
started on its own schedule:

| Job | Stages or operation | Runs in | Typical trigger |
|---|---|---|---|
| **build** | plan → extract → load → transform → package → verify → store | Production | Schedule (for example, weekly) |
| **promote** | promote | Production | After a successful build |
| **fetch** | fetch | Non-production | Schedule (cheap: reads the channel, and downloads only a new image) |
| **provision** | replicate → announce | Non-production | Schedule (pool mode), or a request such as a pull request (on-demand mode) |
| **release** | release | Non-production | Pull request closed, environment stopped, or schedule |
| **prune** | prune | Each environment | Schedule |
| **activate** | activate | Non-production | Manual (rollback) |
| **lock** | lock (re-lock the catalog for review) | Production | Manual, after a schema change |

`replicate` and `announce` are separate stages, because where copies are
announced varies independently of how they are made. They normally run as one
job, `provision`.

The rest of this page follows the jobs in order, from building an image to
cleaning up its copies. First, though, the jobs need a shared way to name an
image and say which one is current.

## Images, generations and channels {#images-generations-and-channels}

Jobs on the two sides never talk to each other directly. A build in production
stores an image. Later, a fetch job outside production asks whether there is a
newer one. For that to work, every image needs a precise identity, a cheap
way to describe it, and a pointer to the one currently in use.

### Image identity {#image-identity}

An image is identified by five things:

- the spec's name;
- the image group (see [golden images](architecture.md#golden-images));
- the engine;
- the engine's **compatibility line**, explained below;
- a **generation**: a unique ID assigned at build time and shared by every
  image from that build.

Consumers usually choose an image by engine and line, because a copy must be
started by a compatible server.

### Compatibility lines {#compatibility-lines}

The **compatibility line** is the part of the server version that decides
which servers can open a copy. Each engine adapter documents how much of the
version that is (decision 120):

| Engine | Line | Example | Why |
|---|---|---|---|
| PostgreSQL | Major version | `17` | |
| SQL Server | Major version | `16` | Its images are tagged by year (`2022`) |
| Oracle | Major version | `23` | |
| MySQL | Major and minor | `8.4` | 8.0 cannot open what 8.4 wrote |
| MongoDB | Major and minor | `8.0` | |
| Redis | Major and minor | `7.4` | Each minor version writes its own RDB version |
| Elasticsearch, OpenSearch | Major and minor | `8.15` | A snapshot restores only on the same or a later minor |

A build uses the source's exact version. When the source is upgraded within a
line, the next image simply joins that line. An upgrade to a new line starts a
new series of images. The image summary (below) also records the exact
version, as `server_version`.

To serve a copy, a consumer needs a server of the copy's line that is at least
as new as the copy's `server_version`. The newest release of a line opens what
older releases wrote. In practice:

- Where the vendor publishes a tag for the line, use it: `postgres:17`,
  `mysql:8.4`, `redis:7.4`, `mongo:8.0`.
- Elsewhere, use the newest exact release.
- Oracle is the exception. A pluggable database plugs cleanly only into the
  release update that wrote it, so serve Oracle copies with exactly
  `server_version`.

### Image summaries {#image-summaries}

An image can be many gigabytes, and a job that only wants to know "is there
anything new?" shouldn't have to download it. So every store holds, next to
each image, a small **image summary** of a few kilobytes. It is copied from
the image's full manifest, which travels inside the image, and holds:

- the image's identity;
- its build time;
- its content hashes;
- its verification result and strict-mode levels;
- the exact server version it was built from;
- its signature reference.

Here is one image group's directory in a store:

```
export-store/shop/billing/
  index.json                           # image lines and generations, for stores that cannot list
  postgres-18/
    0193a7c21b4e9f30.physical.tar.gz   # one archive per format, possibly many GB
    0193a7c21b4e9f30.logical.tar.gz
    0193a7c21b4e9f30.verify.json       # the full verification report
    0193a7c21b4e9f30.sigstore.json     # the signature bundle
    0193a7c21b4e9f30.summary.json      # the image summary, written last
    channels/latest.json               # points at generation 0193a7c21b4e9f30
```

The `index.json` file is explained under [export stores](#the-index).

### Channels {#channels}

Images are addressed through **channels**: named, movable pointers such as
`latest`. A channel points at the current generation.

- `promote` advances a channel in the export store.
- `fetch` advances a channel in non-production staging.

Test pipelines use their own channel, or their own store. That way,
experimenting with a spec never replaces the image that live copies are made
from.

With images named and found this way, the jobs can be described one side at a
time.

## The production side {#the-production-side}

Three jobs run inside the boundary: `build` makes an image, `promote` lets it
out, and `lock` re-locks the catalog when the source's schema changes.

### build {#build}

`build` runs the stages from `plan` to `store`. Two details matter for CI/CD.

First, `verify` runs after `package`, against a scratch restore of the
packaged image. So verification covers the artifact that actually ships,
including whether it restores at all. Its checks include:

- referential integrity;
- row counts;
- PII (personally identifiable information) scans;
- SQL assertions the user supplies.

Second, a spec may declare SQL **hooks**:

- `before_load`, for server settings and extensions;
- `after_transform`, for seed data developers need, such as test accounts.

Hooks run before `verify`, so their effects are verified too. See
[anonymization](anonymization.md#hooks).

A run ends in one of three **outcomes**:

| Outcome | Meaning | Example |
|---|---|---|
| success | Image built and verified | |
| warning | Image built and verified, with something an operator should fix | A `files.paths_from` value that matched no pattern, so its file was not copied |
| failure | No image stored | Any verification failure, any strict-mode violation |

Anything that could put real personal data into a copy is always a failure,
never a warning. CI/CD adapters map warnings to each system's own state, as
[runtime bridging](#runtime-bridging) describes.

### promote: the gate {#promote}

`promote` is the only way out of production. It runs on the production side
and copies an image into an **export store**, the only storage the
non-production side can read.

It checks every image before copying any, and refuses an image unless it
has all of these:

- a passing `verify` report: at least one check run, and none failed;
- strict-mode levels, for every kind, at least as strict as `promote.strict`
  requires (by default `data: columns`, and `warn` for each kind of object);
- a signature that the spec's `trust` settings accept;
- content hashes that match the stored artifacts.

On the other side, `fetch` pulls from the export store with read-only
credentials for that store alone.

That is a pull model. A push-based variant, where production writes directly
into non-production storage, is just a choice of storage adapter. The gate is
the same either way.

### Export stores {#export-stores}

An export store can be any store adapter:

- a directory (`filesystem`);
- a bucket (`s3`);
- for `fetch` only, a web server (`http`).

#### The http store {#the-http-store}

The `http` store suits a production side that already publishes files through
a web server. The non-production side can reach that server over HTTPS, but
can neither mount its storage nor call an S3 API on it.

The `http` store is read-only. `promote` writes with the filesystem or S3
store into the directory the server serves, and `fetch` reads through the
server. Any write through the `http` store fails, naming the store. It
authenticates with basic auth or a bearer token, given as secret references,
and can take its own CA bundle. See the
[adapter's README](https://github.com/datadouble/datadouble/tree/main/adapters/http).

#### File modes in a filesystem store {#file-modes}

When a web server serves a filesystem export store, the server's user must be
able to read what `promote` wrote. The filesystem store's settings control
that:

- `path` is the directory.
- `mode` is an octal string such as `"0640"`, the default.

Every file the store writes gets that mode, whatever the umask. The mode is
set before the file is renamed into place. Every directory the store creates
gets the mode plus the search bit wherever the mode has the read bit, so
whoever can read the files can reach them:

| `mode` | Files | New directories |
|---|---|---|
| `"0640"` (default) | `0640` | `0750` |
| `"0644"` | `0644` | `0755` |

Directories that already exist keep their mode. So a web server can read
`promote`'s files in two ways:

- with the default mode, when the server runs in the group of `promote`'s
  user;
- with `"0644"`, as any user.

Two kinds of mode are refused when the store's settings are read, with an
error naming `mode`: a mode that lets anyone but the owner write, and a mode
that leaves the owner unable to read and write.

```yaml
promote:
  to:
    - filesystem:
        path: /srv/exports/datadouble
        mode: "0644"
```

#### The index {#the-index}

`fetch` lists the store to find each image group's engine versions. Web
servers have no standard way to list a directory. So `promote` writes an
**index**, `index.json`, under each `<spec>/<group>/` directory on every export
store. It names the group's image lines and the generations that have a
summary in each.

- `promote` rewrites the index after each image's summary and channel.
- `prune --target export` rewrites it to match what is left, or removes it
  when nothing is left.

A store that cannot list keys reads the index instead. `fetch` takes only the
line names from it, then reads the channel and summary as it would from any
store. So the `http` store needs nothing specific to one web server, and
everything else in the store's layout is unchanged.

`promote` rebuilds the index by listing each export store. So an S3 export
store needs `s3:ListBucket` for `promote`'s credentials.

### The lock job {#the-lock-job}

Strict mode refuses to plan without a **catalog lock**: a committed record of
the source's tables and columns, which someone has reviewed for personal
data. When the source's schema changes, the lock has to be renewed.
`datadouble lock` does that, but it reads the source, so it can run only
inside the boundary. Where the only machine there is a CI runner, the manual
`lock` job (`ci init --job lock`) re-locks:

- It runs on the production side, on the same `--runner` as `build`. It gets
  only the secrets that the spec's `sources` and `notify` sections name.
- Like `build`, it is refused on a vendor's hosted runner.
- It has no schedule, because someone always means to re-lock.
- It runs `datadouble lock --spec <spec> --output datadouble-lock`. That
  writes two files into the `datadouble-lock` workspace directory and leaves
  the committed lock as it is:
    - `catalog.lock`, the new lock;
    - `changes.txt`, its changes against the committed lock (the list the CLI
      prints).
- It publishes that directory as the CI system's job artifact: a GitHub
  artifact, a GitLab job artifact, or a Jenkins archived artifact. Kubernetes
  keeps no job artifacts, so on Argo CD the job prints both files to its log.
  Its termination message carries the outputs: `fingerprint`, and, when
  there was a committed lock, `changes`, the number of changes.
- It never commits. A person reviews the changes, checks every new or changed
  column for personal data, and commits the lock. That review is the point of
  strict mode, so no job does it for them.

Publishing the lock as a CI artifact is safe. A lock contains table, column and
key names, engine type names and requirement versions, never values, so it is
consistent with [no data in CI storage](#no-data-in-ci-storage) (decision
123). Type names can carry schema literals, such as a MySQL `enum` column's
labels. Those are part of the schema, and already in the committed lock.

## The non-production side {#the-non-production-side}

Once an image is in the export store, the non-production side takes over. It
holds read-only credentials for the export store and nothing else from
production. Three stages turn an image into copies people can use: `fetch`
brings the image in, `replicate` makes copies, and `announce` tells consumers
where they are.

### fetch {#fetch}

`fetch` pulls an image from the export store into non-production **staging**,
the local area copies are made from. It:

1. reads the export store's channel, and skips all the work below when
   staging already holds the generation it points at;
2. reads the image summary and checks the image's signature (decision 58);
3. downloads the image's archive in the format `fetch.format` names, and
   checks it against the summary's hash;
4. unpacks it into the form `replicate` copies from, for example a
   ready-to-mount data directory;
5. advances the staging channel.

Because the first step reads only a few bytes, running `fetch` every few
minutes is cheap.

Two safeguards protect staging:

- `fetch` unpacks an image under a temporary name, holding the image line's
  lock, and renames it when complete. So `replicate` never copies a partial
  image.
- Staging keeps the newest `fetch.keep` generations (3 by default), so
  `activate` can move the channel back to a previous generation if a new
  image turns out to be bad.

### replicate {#replicate}

`replicate` makes copies of the staged image. It has two modes, chosen by how
long a copy takes to make:

| Mode | Behavior | Suits |
|---|---|---|
| **pool** | Keep N unclaimed copies of the active generation ready; consumers claim one | Large images, where copying takes longer than a developer will wait |
| **on-demand** | Make N copies for a named requester (a pipeline, merge request or environment); asking again makes none until some are released | Small images, CI test jobs |

Each copy is recorded before it is made, and marked ready when complete. A
copy that never completed is removed the next time `replicate`, `claim` or
`release` works on its image line.

A copy is one of two kinds, and each replicate adapter declares which kind it
produces:

- **Volume:** a data directory the consumer starts its own server on, such as
  a Docker volume, a Kubernetes PersistentVolume or a ZFS clone. Without
  extra setup, a server started on a volume has only its local accounts.
  `package.accounts` gives such copies accounts that log in over the network
  (decision 121, see
  [accounts in physical images](architecture.md#accounts-in-physical-images)).
- **Instance:** a running server with an endpoint: a host, port, database, and
  a reference to credentials.

| Replicate adapter | Copies | How consumers claim one |
|---|---|---|
| filesystem | A directory, with shared blocks where the filesystem allows | `datadouble claim` |
| btrfs | A subvolume of reflinked files (decision 63) | `datadouble claim` |
| docker-volume | A Docker volume, or a container running the engine's server on one (decision 62) | `datadouble claim` |
| k8s-pv | A directory and a PersistentVolume for it | A bound claim; a released volume is removed and replaced |

In pool mode, consumers outside Kubernetes take a copy with
`datadouble claim --owner NAME` and give it back with
`datadouble release --owner NAME` (decision 60).

### announce {#announce}

A copy is no use until its consumer knows where it is. `announce` tells
consumers where each copy (also called a clone) can be reached, either as a
volume or as connection parameters. It never writes data: `replicate` has
already made the clone, and `announce` only records where it is and how to
reach it.

- **Volume clones** are announced as a volume that consumers can claim and
  start their own server on.
- **Instance clones** are announced as connection parameters: host, port,
  database, and the clone's own credentials.

Every clone is announced with its copy ID and labels: spec, image group,
engine, compatibility line (`engine-version`), generation, build time
(`built-at`), mode (pool or on-demand) and, when it has one, owner.

| Announce adapter | Writes | For |
|---|---|---|
| k8s-secret | A Secret per copy, with its location and, for instances, credentials | Instance copies on Kubernetes |
| ci-outputs | Job outputs of the running CI system: `GITHUB_OUTPUT`, a GitLab dotenv report, or a JSON file | On-demand copies in CI |
| vault | A Vault (or OpenBao) KV v2 secret per copy | Any |
| dotenv | A dotenv file, readable only by its owner | Local and simple CI use |

A clone's credentials go only to targets that can keep a secret: `k8s-secret`,
`vault` and `dotenv`. `ci-outputs` announces the endpoint without them,
because job outputs on most systems are not secret.

A `k8s-pv` copy needs no separate announcement. Its PersistentVolume carries
labels under `datadouble.dev/` for the copy's ID (`copy`), spec, group,
engine, engine line (`engine-version`), exact server version
(`server-version`) and generation. That volume is itself the
announcement: consumers match it with a PersistentVolumeClaim selector
(decision 61).

#### Reaching another cluster {#reaching-another-cluster}

`k8s-pv`, `k8s-secret` and the `kubernetes` runtime use `kubectl`. Without
other settings, it reaches the cluster a pod runs in through its service
account, or the cluster of the ambient kubeconfig elsewhere. A job that drives a different cluster gives
each of them two settings:

- `kubeconfig`: a secret reference to the kubeconfig's content
  (`env: NAME`);
- optionally, `context`.

This is never the runner's own kubeconfig file. The job's container sees only
the paths in the spec, and runs as the engine's user, not the runner's. A
`file: /path` reference suits a secret file mounted for the purpose: `ci init`
mounts it into the job, and the engine's user must be able to read it.

The image has no exec credential plugins, so the kubeconfig must carry a token
or a client certificate. As with any secret reference, `ci init` lists the
variable for the jobs whose part of the spec references it, and each system binds
it from its own secret store (decision 124).

## Operations on images and copies {#operations-on-images-and-copies}

Images and copies pile up. Copies outlive the pull requests they were made
for, old generations fill the stores, and sometimes a new image turns out to
be bad. Three operations deal with what already exists.

### release {#release}

`release` destroys copies. Which copies it releases depends on how they were
made:

- **On-demand copies** are released by copy ID or by owner, which is the
  normal teardown path. A scheduled sweep with `--expired` catches copies
  past `replicate.expires_hours` whose teardown never ran.
- **Pool copies** are recycled by `replicate`, not `release`. When another
  generation becomes active (a newer one, or an older one after `activate`),
  unclaimed copies of other generations are removed as fresh copies
  replace them, and any left over once the pool is full. So the pool never
  empties. For urgent replacement,
  `replicate --all-at-once` removes them before making new ones.
- **Claimed copies** belong to their consumer until released. On Kubernetes,
  a `k8s-pv` volume is kept (`Retain`) when its claim is deleted, and
  Datadouble removes the released volume the next time `replicate`, `claim`
  or `release` runs for its image line.
- **Incomplete copies** are always removed, by the same three commands.

#### Withdrawing announcements {#withdrawing-announcements}

The `release` command also withdraws a released copy's announcements, so its
credentials don't linger in a Secret or a vault. This is harder than it
sounds, because the place an announcement went can depend on where it was
made. A `k8s-secret` Secret announced from a laptop goes into the laptop's
kubeconfig context's namespace, while a release sweep may run in a pod in
another namespace.

Only `release` withdraws announcements. A copy that `replicate`, `claim` or
`prune` removes, such as a recycled pool copy, keeps its announcements.
[#104](https://github.com/datadouble/datadouble/issues/104) tracks this gap.

So Datadouble's registry of copies records, for each copy, the targets each
`announce` entry reported. `release` gives each entry back only its own
targets, and the entry withdraws them from where they were actually written
(decision 135). In the example, the sweep looks for the Secret in the
laptop's namespace.

This works only if both of these hold:

- every process that announces or releases with the spec reaches the same
  cluster through that entry;
- the releasing identity may delete Secrets in every namespace a copy was
  announced into.

A copy with nothing recorded is looked for where the entry's settings resolve
now.

Withdrawing tries every target of every entry. Then, if any failed, it fails
once, naming each announcement left behind for a person to remove by hand. The
copies themselves are already gone at that point.

Entries are told apart by adapter and position: `k8s-secret`, then
`k8s-secret#2` for the second entry of the same adapter. So reordering or
removing entries of the same adapter while copies are live also leaves their
announcements to remove by hand.

### prune {#prune}

`prune` applies retention to images in any store and in staging. Its
`--target` says where: `store` (the build's stores), `export` (the export
stores) or `staging`. For each image line it keeps:

- the newest `prune.keep` generations (3 by default; `fetch.keep` in
  staging);
- in stores, anything built within `prune.keep_days` days (7 by default);
- always, whatever any channel points at.

Retention also works by compatibility line. Only a group's newest
`prune.keep_versions` lines (2 by default) keep their generations. Older
lines keep only their channels' targets. With `--target staging`, their
unclaimed pool copies are removed too.

`prune` supports `--dry-run`, which reports what it would remove and the
space it would recover.

### activate {#activate}

`activate` moves the staging channel to a generation someone chooses. Its main
use is rollback: if a new image turns out to be bad, `activate` points the
channel back at a previous generation that staging still holds, and new copies
come from that generation. Pool copies of other generations are then recycled
as [release](#release) describes.

## CI/CD adapters {#ci-cd-adapters}

So far this page has described what the jobs do. A **CI/CD adapter** is what
turns them into something a particular CI/CD system runs. It contains **no
pipeline logic**. Like the CLI, it only builds JSON requests and maps the CI
system's conventions onto them. It has three parts:

1. **Distribution:** the system's own packaging, so a pipeline references a
   versioned Datadouble step rather than a shell script.
2. **Generation:** `datadouble ci init` writes pipeline configuration for a
   spec and one or more jobs, ready to commit.
3. **Runtime bridging:** while a job runs, the adapter translates secrets,
   outputs, outcomes, log masking, summaries and concurrency between the CI
   system and Datadouble's JSON interface.

### Generation {#generation}

`datadouble ci init` writes one side of the boundary at a time:

```
datadouble ci init --system github-actions --job build,promote,prune,lock \
    --spec datadouble/shop.yml --runner prod-db --work /srv/datadouble/work
datadouble ci init --system github-actions --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner staging
```

The equivalent JSON request for the first command, without `lock`:

```json
{"command": "ci.init", "system": "github-actions", "jobs": ["build", "promote", "prune"],
 "spec": "datadouble/shop.yml", "runner": "prod-db", "work": "/srv/datadouble/work"}
```

The response lists the files written and the environment variables the jobs
read secrets from.

One call writes one side. Mixing production and non-production jobs in one
call fails. `prune` runs on either side, so `prune` on its own needs `--side`.

Datadouble's core, not the adapter, decides what every job runs (decision
100):

- its `datadouble` command lines;
- its triggers, schedules and parameters;
- which secrets it reads;
- which host paths it needs.

The adapter only writes that in its system's form. So the jobs are the same on
every system.

Generated configuration:

- **Pins the Datadouble version:** the native step at that release's tag, and
  the release's image.
- **References secrets by name only.** Each job gets only the secrets named
  in the spec sections it reads, and mounts only the absolute paths named
  there:
    - `build` reads `sources`, `files`, `package` (only the password
      references of `package.accounts`), `store`, `signing`, `secrets` and
      `runtime`. It also gets `COSIGN_PASSWORD` for a build signed with a key;
    - `promote` reads `store`, `promote` and `trust`;
    - `lock` reads `sources`;
    - `fetch` reads `fetch` and `trust`;
    - `provision` and `release` read `fetch`, `replicate`, `announce` and
      `runtime`;
    - `activate` reads `fetch`;
    - `prune` reads `store` and `promote` in production, and `fetch` and
      `replicate` outside it;
    - every job reads `notify`, and gets each `--env NAME` given, for
      credentials that adapters read from the environment, such as
      `AWS_ACCESS_KEY_ID`.
- **Includes schedules,** in UTC, and job chaining: `promote` after a
  successful `build`, and a pool's `provision` after `fetch`.
- **Includes the system's concurrency controls,** per job. On GitHub,
  on-demand copies also get one per request.
- **Exposes run-time switches as job parameters:** `--dry-run` for `prune`,
  and the generation to `activate`.
- **Passes the CI system's run ID as `--retry-key` to `build`,** so retrying a
  failed run resumes the generation it started (decision 100). Jenkins runs a
  retry as a new build, so its `build` job takes a `RETRY_KEY` parameter:
  give it the failed build's `BUILD_TAG` to resume it.
- **Publishes the `lock` job's new lock and its changes** as the job's
  artifact (see [the lock job](#the-lock-job)). No other job publishes files.
- **Writes the spec's `notify` email** in the system's own form, or lists the
  setup in `instructions` (see [notifications](#notifications)).
- **Makes on-demand copies when a pull or merge request opens,** and releases
  them when it closes. The owner is `pr-<number>` on GitHub and Argo CD, and
  `mr-<iid>` on GitLab. Jenkins shares no request events across
  installations, so there a request's own pipeline starts `provision` and
  `release` with an `OWNER` parameter. On GitHub and GitLab, requests from
  forks never run on the team's runners.

The mode, pool or on-demand, comes from the spec's `replicate` section.

Every production-side call needs an explicit runner, `--runner`. That
includes `build`, `promote` and `lock`, and also `prune --side production` on
its own. GitLab, Jenkins and Argo CD need `--runner` on both sides: runner
tags, agent labels, or the side's namespace. On GitHub, a non-production call
without it runs on any `self-hosted` runner. `build` also needs `--work`, an
absolute work directory. See [where jobs run](#where-jobs-run).

### Supported systems {#supported-systems}

**Tier 1** systems are shipped and tested from the first CI/CD release. Each
one has its own native form:

| System | Native form | Outputs | Secrets | Teardown hook |
|---|---|---|---|---|
| GitHub Actions | A workflow per job, using one action in this repository (`datadouble/datadouble/ci/github-actions`) at the release's tag, which runs the Datadouble image | `$GITHUB_OUTPUT`, `$GITHUB_STEP_SUMMARY` | Repository secrets of the same name; the workflow's identity token for keyless signing | A `pull_request: closed` workflow |
| GitLab CI | An included file of jobs per side, extending a job template that this repository publishes at each release's tag, pinned by its SHA-256 (`include: remote` with `integrity`) | dotenv report artifacts | Masked, protected CI/CD variables | Environments with `on_stop` |
| Jenkins | A declarative Jenkinsfile per job, loading this repository's shared library at the release's tag and calling its one `datadouble` step, on an agent with Docker or, with `--agent kubernetes`, in a pod of the Kubernetes plugin; `activate` takes the generation as a parameter | Step return values, the build's description | Secret text credentials, bound with `withCredentials` | A request's own pipeline starts `release` with its `OWNER`, for example in `post { cleanup }` |
| Argo CD | An Application per side, installing this repository's Helm chart at the release's tag: a CronJob per job, with a following job also running as a later step of its leader's pod; a PreSync hook Job for on-demand provision; a PostDelete hook Job for release. The adapter's README has an ApplicationSet example with the pull-request generator | Kubernetes PersistentVolume or Secret via `k8s-pv` or `k8s-secret`; containers' termination messages | A Kubernetes Secret, `datadouble-<spec>`, with a key per variable | PostDelete hook |

The Kubernetes pieces (container image, Job templates, RBAC) form a shared
base. Argo CD uses it directly, and the Tier 2 Kubernetes systems will build
on it.

**Tier 2** systems come after Tier 1 is stable: Argo Workflows, Tekton, Flux,
Azure Pipelines, CircleCI, Bitbucket Pipelines and Buildkite.

GitLab's CI/CD Catalog needs components to live in a GitLab project, so they
will be published from a GitLab mirror of this repository. Until that project
exists, generated jobs include the job template straight from this repository
(decision 101). Self-managed GitLab users can mirror the file or the project.

Some setup cannot live in a system's configuration. GitLab's pipeline
schedules are one example. `ci init` lists those steps in its response, under
`instructions`. A GitLab schedule names the job it runs in the
`DATADOUBLE_JOB` variable.

### Runtime bridging {#runtime-bridging}

Generation happens once. Runtime bridging happens every time a job runs. The
core learns which CI system it is running under from an explicit
`--ci <system>`, which the native steps set (`ci` in a JSON request). It never
guesses. The CI/CD adapter then handles four things.

**Secrets.** Secrets reach Datadouble as environment variables, set from the
system's own secrets. The adapter registers every secret value Datadouble
resolves with the system's log masking, where the system has it (GitHub's
`add-mask`). GitLab masks the variables defined as masked, and Jenkins masks
the credentials it binds, so their adapters have nothing to add.

**Outputs and outcomes.** The adapter:

- writes the JSON response's outputs as native job outputs: `outcome`, the
  `generation` a run built, and the `error` code of a failure;
- maps the outcome (success, warning or failure) to the native build state;
- writes the summary the CLI prints wherever the system has a place for one,
  so the system's own notifications report it.

Each system shows a warning in its own way:

| System | A warning appears as |
|---|---|
| GitHub Actions | A passing step with a warning annotation (GitHub Actions has no warning state) |
| GitLab CI | An allowed failure: a distinct exit code that `allow_failure: exit_codes` recognizes |
| Jenkins | An `UNSTABLE` build |
| Argo CD | A succeeding Kubernetes Job that says so in its log |

**Concurrency.** Generated configuration includes the system's concurrency
controls: GitHub `concurrency`, GitLab `resource_group`, Jenkins
`disableConcurrentBuilds`, and a CronJob's `concurrencyPolicy: Forbid` on Argo
CD. Different systems may run jobs for the same spec, so the core also locks
each image line in staging: only one `fetch`, `replicate` or `release` works
on a line at a time.

**Failure.** There is no continue-on-error switch. A failed stage stops the
job.

### Notifications {#notifications}

A failed build is no use if nobody hears about it. A spec's `notify` section
configures notifications for generated jobs that fail or end with a warning
(decision 125):

```yaml
notify:
  when: [failure, warning]          # default: [failure]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
    - email:
        to: [data-team@example.com]
```

#### Why notify is in the spec {#why-notify-is-in-the-spec}

`notify` lives in the spec, not in `ci init`'s options. `ci init` runs again
whenever the spec, the Datadouble version or the job options change. Its
options are recorded only in the files it writes. So an option left off a
later run would silently drop notifications. The spec already describes an
image's whole life (decision 57), and notifications belong to it.

#### Webhooks {#webhooks}

Datadouble posts webhooks itself, after the command and before the job ends.
So they work the same way on every system, Kubernetes included.

- Generated jobs name themselves with `--ci-job` (`ci.job` in JSON).
- Every job receives the webhook URL's secret, since every job can fail.
- One webhook serves both sides of the boundary, since both read the same
  spec. A `file: ...` reference becomes a path mounted in every job.

The URL is a secret reference, because a webhook's URL usually carries its
credential. Credentials in the URL (`https://user:token@host/...`) are sent
as basic authentication, and only over HTTPS: a URL with credentials and
`http://` is refused.

Posts go through the environment's proxy settings (`HTTPS_PROXY`,
`HTTP_PROXY`, `NO_PROXY`). They verify the server's certificate and host name,
and never follow a redirect.

The body is a JSON POST:

```json
{"spec": "shop", "job": "build", "outcome": "failure",
 "generation": null, "error": "store_failed",
 "run_url": "https://github.com/example/shop/actions/runs/42",
 "text": "Datadouble shop: build failed (store_failed) - https://github.com/example/shop/actions/runs/42"}
```

- `generation` is the generation a build made. It is given with a warning or
  a failed verification, and is null with an error code.
- `error` is the error code of a failed request. It is null for a failed
  verification, whose report is in the job's log.
- `run_url` is the system's page for the run. It is null on Argo CD, where a
  Job has no page.
- `text` is one line for people, built from the other fields. It says
  `failed` for a failure and `passed with warnings` for a warning, and
  leaves out the fields that are null.

The body carries names, codes and outcomes, never data values or secrets
(decisions 18 and 54). That holds for `text` too.

A webhook to a chat room takes `format: chat`. Datadouble then posts only
the line, as `{"text": "..."}`, which Google Chat, Slack, Mattermost and
Rocket.Chat incoming webhooks all accept:

```yaml
notify:
  to:
    - webhook:
        url:
          env: CHAT_WEBHOOK
        format: chat
```

Google Chat refuses a body with any key it doesn't know, so it needs
`chat`. The default, `format: json`, posts the whole body above, for a
receiver written for Datadouble.

Each attempt at a job posts. So a system that runs a failed job again, such as
a Kubernetes Job with a `backoffLimit`, notifies once per attempt. A follower
in the same run (`promote` after `build`) notifies as its own job. Argo CD
never runs a pod again because its follower failed: a `podFailurePolicy` fails
the Job instead. Running the pod again would run the leader again too, so a
build that warned would build, and notify, a second time.

#### When a webhook fails {#when-a-webhook-fails}

A notification that fails never changes the job's outcome. A webhook that
cannot be posted to is a warning line in the job's log. That covers a timeout
of ten seconds, a refused connection, a redirect, and any status other than
2xx. The warning names the webhook by its position and never quotes its URL.
The job's outcome is what it would have been without the webhook.

A missing webhook secret depends on the system:

- Jenkins' `withCredentials` and Argo CD's `secretKeyRef` fail the job before
  it starts, as for any missing credential.
- GitHub and GitLab pass an empty variable instead. The job runs, and its log
  warns that the webhook's variable is not set.

#### Email {#email}

Datadouble doesn't send email itself. It uses the system's own mechanism,
where there is one:

| System | Email |
|---|---|
| GitHub Actions | None a workflow can send. GitHub mails a failed run only to whoever started it, and never mails a warning, so `ci init` says so in `instructions`. A webhook to a service that sends mail can reach a list |
| GitLab CI | The project's Pipeline status emails integration, set up by hand and listed in `instructions`. It mails broken pipelines only, so warnings need a webhook. On-demand provision and release run in merge request pipelines, which see no protected variables, so `instructions` asks for the webhook's variable to be available there |
| Jenkins | The `mail` step in each Jenkinsfile's `post` section: `failure` and `aborted` (a timed-out build) for failures, `unstable` for warnings. A mail that cannot be sent is logged and leaves the build's result alone |
| Argo CD | None: `ci init` fails, naming email, since Kubernetes Jobs have no mail mechanism |

#### Jobs that fail before Datadouble starts {#jobs-that-fail-before-datadouble-starts}

A job that fails before Datadouble starts posts no webhook, because nothing
was running to post it. Examples:

- an image that cannot be pulled;
- a runner without Docker;
- a path the runner lacks;
- a spec that does not parse;
- a job the system stops at its time limit.

The system's own notifications cover those, where it has them:

- GitHub's failed-run mail to whoever started the run;
- GitLab's Pipeline status emails;
- Jenkins' `post` mail, when the spec lists email, which runs whatever failed.

Argo CD has none. So `ci init` asks the operator to alert on failed Jobs in
the namespace with the cluster's monitoring, such as kube-state-metrics'
`kube_job_status_failed`.

### No data in CI storage {#no-data-in-ci-storage}

CI systems have their own storage for artifacts and caches, and it is often
outside production. GitHub's artifact storage is on github.com, for example.
So CI/CD adapters never upload extract artifacts, working databases or golden
images there.

That rules out keeping a failed build's progress in the CI workspace.
Instead, resumability for `build` ([decision 7](decisions.md)) uses a
persistent work directory in production scratch space. It is named when the
jobs are generated, with `--work`.

Manifests and reports may be uploaded as CI artifacts, so they must never
contain data values. In particular, `pii-scan` reports name columns and
detector types, never the matched values. The `lock` job's catalog lock and
its changes are published this way too. They hold names, types, keys and
requirement versions, never values (decision 123).

## Where jobs run {#where-jobs-run}

The last piece is the machines. The boundary is only real if production jobs
actually run inside it, so Datadouble checks the machine, not just the
configuration.

### Production runners {#production-runners}

`build`, `promote` and `lock` must run on infrastructure inside the
production boundary. Depending on the system, that is:

- self-hosted GitHub runners, or Actions Runner Controller;
- GitLab runners selected by tag;
- Jenkins agents selected by label;
- a Kubernetes cluster in production.

Four rules keep them there:

- **Runners are marked inside the boundary.** Reading a spec's sources
  (`lock`, `plan`, and every `run` that plans or extracts) and `promote`
  refuse to run unless the machine's environment sets
  `DATADOUBLE_BOUNDARY=inside`. The error is `outside_boundary` (decision
  98). The operator sets the mark once, where the runner is configured:
    - a self-hosted GitHub runner's `.env`;
    - a GitLab runner's `environment`;
    - a Jenkins agent's environment;
    - the pod template of Datadouble's Kubernetes jobs.

    A machine nobody marked, such as a vendor's hosted runner or an
    unfamiliar CI system, is refused before anything connects.

- **Generation needs a runner.** `datadouble ci init --job build` (or
  `promote`, or `lock`) fails without `--runner`. It never writes a
  vendor-hosted runner: the GitHub and GitLab adapters refuse their vendors'
  runner labels, such as `ubuntu-latest` or `saas-linux-small-amd64`, on
  either side.
- **Hosted runners are refused even when marked.** At run time, reading the
  source and `promote` also fail if any installed CI/CD adapter recognizes a
  vendor-hosted runner, for example from GitHub's
  `RUNNER_ENVIRONMENT=github-hosted`. This holds even when the machine is
  marked inside.
- **`build` needs a container runtime.** `build` runs engine software in
  containers (decision 72), so its runner needs the spec's container runtime.
  That is a Docker-compatible daemon that sees the work directory at the same
  path, or, for `kubernetes`, a pod whose work directory is on the
  PersistentVolumeClaim the engine pods share.

Non-production jobs get separate generated configuration and separate
secrets. Generation never puts production credentials into a non-production
job.

### How a job runs Datadouble {#how-a-job-runs-datadouble}

By default, each job's native step runs the pinned Datadouble image with the
runner's Docker daemon (decision 100). The image runs:

- as the engine's user (decision 86);
- on the host's network, so it reaches the source, and the working servers'
  published ports, just as the runner would.

The workspace is mounted at the same path inside the container. So is every
absolute path the job's spec sections name: stores, staging, copies, a Unix
socket directory, a secret file and the work directory. They must keep their
paths because the engine containers Datadouble starts mount them by those
paths. A job's runner therefore needs Docker, with those paths owned by the
engine's user.

#### Another Docker command or daemon {#another-docker-command-or-daemon}

The steps use the `docker` command and the daemon at `/var/run/docker.sock`.
To use something else, such as Docker installed outside `PATH`, or another
Docker-compatible daemon on another socket, either:

- set it in the runner's environment: `DATADOUBLE_DOCKER` for the command,
  and `DATADOUBLE_DOCKER_SOCKET` or a `unix://` `DOCKER_HOST` for the socket;
  or
- pass `ci init --docker` and `--docker-socket`, which write them into the
  generated configuration (decision 126).

A step reads the socket's group from the configured path. It starts the image
on that daemon (setting `DOCKER_HOST`, and `CONTAINER_HOST` for podman), and
mounts the socket at `/var/run/docker.sock` inside the Datadouble container.
That is where `DOCKER_HOST` points, and where the `docker` runtime finds it as
always.

Rootless Docker is not supported. Its user namespace breaks both the socket's
group and the shared user that decision 73 relies on.

#### Kubernetes agents {#kubernetes-agents}

Some systems run their agents as Kubernetes pods. There, a runner with Docker
may not exist. `ci init --agent kubernetes` instead runs each job in a pod of
its own (decision 127). Only Jenkins supports this, through the Kubernetes
plugin's `agent { kubernetes { ... } }`. Docker stays the default.

It names things as the Argo CD adapter does (decision 103):

- `--runner` is the side's namespace, and the pod mounts that namespace's
  `datadouble` PersistentVolumeClaim.
- `--cloud` names the plugin's cloud. Without it, the plugin's default cloud
  is used.

The two sides need separate namespaces, ideally separate clusters, since each
side's claim holds that side's data.

**Engines and the claim.** Jobs that start engine software need the spec's
`kubernetes` runtime on that claim, with every path in the spec under the
runtime's `mount`. Those jobs' pods mount the claim at `mount`, the path the
engine pods use. Other jobs mount it at their paths' common directory.

**The runtime's namespace.** It is best to leave the runtime's `namespace`
out. Engine pods then start in the namespace of the pod that starts them,
which is each side's own, so one spec serves both sides. A runtime that does
name a namespace must name the side's own, for every job that starts engines,
with Jenkins and Argo CD alike (decision 133). Otherwise `ci init` refuses.

**Containers and users.** The pinned image runs as a container of the pod, as
the engine's user, and the step runs `datadouble` in it. The plugin's own
agent container keeps its image's user. It starts with a umask that lets both
containers write the pod's workspace.

**The boundary mark.** On Kubernetes agents, the boundary mark is only the
namespace's `datadouble-boundary` ConfigMap, mounted as a file. An
environment variable won't do, because the CI system lays its own environment
over the container's.

**Permissions.** The pods run as a service account,
`datadouble-<spec>-<side>`. Its RBAC lets Datadouble run engine pods (pods,
their logs, exec and port forwarding, and Secrets), read
PersistentVolumeClaims and, for `k8s-pv`, manage PersistentVolumes.
`ci init`'s `instructions` list it, with the plugin's cloud and the claim.

Kubernetes agents refuse a Docker command or socket. GitLab's Kubernetes
executor and GitHub's Actions Runner Controller could follow the same model.

## Testing {#testing}

An adapter that writes broken configuration, or leaks a secret into a log,
fails in someone else's pipeline. So each CI/CD adapter is tested at three
levels, from cheapest to most realistic.

### 1. Generated configuration {#testing-generated-configuration}

Golden-file tests cover every combination of system and job. Each system's
own tooling then validates the files:

| System | Validator |
|---|---|
| GitHub Actions | `actionlint` |
| GitLab CI | The CI Lint API, against the containerized GitLab below |
| Jenkins | The declarative pipeline validator in the Jenkins container, with the Kubernetes plugin installed for Kubernetes agents' Jenkinsfiles; the conformance suite runs the shared library itself |
| Argo CD / Kubernetes | `helm lint`, `kubeconform` |

### 2. Local end-to-end {#testing-local-end-to-end}

Each system runs locally, and runs every job as `ci init` wrote it, against a
small shop database in PostgreSQL. The Datadouble image is built from the
checkout. Run it with `make test-ci SYSTEM=<name>`.

| System | Local runtime |
|---|---|
| GitHub Actions | `act`, running jobs on the host as a self-hosted runner does |
| GitLab CI | GitLab CE and a GitLab Runner with the shell executor, in containers |
| Jenkins | `jenkins/jenkins` with pinned plugins, configured with Configuration as Code, its built-in node running jobs with the host's Docker daemon |
| Jenkins, Kubernetes agents (`jenkins-kubernetes`) | The same Jenkins on the host, its Kubernetes plugin's cloud starting every job as a pod on a kind cluster that reaches it over WebSocket, with job repositories served by `git daemon`; this has its own CI job |
| Argo CD | A kind cluster with Argo CD's core install and a Helm repository serving the checkout's chart |

`make test-ci` builds the Datadouble image and runs the system's conformance
tests, which start the system's local runtime themselves. `SYSTEM` is one of
`github-actions`, `gitlab`, `jenkins`, `jenkins-kubernetes` and `kubernetes`
(Argo CD). `dev/ci/` holds the images for the GitLab and Jenkins runtimes.
`make test-integration` runs every system after the engine groups. See
[CI/CD systems](local-development.md#cicd-systems) for running them locally.

### 3. Scheduled runs on hosted services {#testing-scheduled-runs}

Local runtimes like `act` don't reproduce every API and runner change. So
GitHub Actions on this repository runs the generated jobs nightly, on a
self-hosted runner that the test starts, and the hosted-runner refusal in
`build` is exercised exactly as in real use. A nightly run on a GitLab.com
project is planned, once that project exists (decision 99).

On GitHub (`.github/workflows/nightly.yml`, decision 104):

1. A job on a GitHub-hosted machine starts a source database and a self-hosted
   runner on that machine, marked inside the boundary.
2. It runs, in order, the workflows that `ci init` writes for
   `tests/nightly/nightly.yml`: build, promote (by `workflow_run`), fetch,
   provision (by `workflow_run`), and prune on both sides.
3. The build signs without keys, through GitHub's identity token.
4. The job checks that the hosted machine itself is refused, even when marked
   inside.

Those workflows are committed, since GitHub runs only workflows in the
repository. `scripts/nightly.py` writes them, using the checkout's action and
without schedules, and `make lint-workflows` fails if they are stale.

### CI/CD conformance suite {#ci-cd-conformance-suite}

Every CI/CD adapter must pass a shared suite, `datadouble_conformance.ci` in
the conformance package. The adapter's tests supply a harness that runs one
generated job in the system's local runtime. Each test starts from a fresh
source, stores and repository, with jobs `ci init` wrote for both sides. The
suite checks that:

- build → promote → fetch → provision completes in both pool and on-demand
  modes, and each job's native outputs and build state match its JSON
  response;
- a warning outcome maps to the native warning state, and a failure stores
  nothing;
- a known test secret never appears in the job logs;
- `promote` refuses when verification failed;
- `build` refuses to generate or run on a vendor-hosted runner, or on a runner
  nobody marked inside the boundary;
- a `build` that fails part way resumes from the last completed stage when the
  run is retried;
- `fetch` skips the download when nothing newer exists;
- `activate` rolls the staging channel back, and new copies come from the
  chosen generation;
- `release` removes every on-demand copy `provision` created when the request
  closes, and its scheduled sweep runs;
- `prune` never removes an image a channel points to;
- no data artifact is uploaded to the CI system's artifact storage;
- `lock` publishes the new lock and its changes (in the job's artifact, or on
  Argo CD in its log), leaves the committed lock as it is, and is refused on a
  vendor-hosted runner;
- a failing job posts its outcome to the spec's webhook, a warning does too, a
  succeeding job does not, and a webhook that cannot be reached leaves the
  job's outcome alone;
- where jobs run as pods (Jenkins with Kubernetes agents), each job runs in a
  pod holding everything its Jenkinsfile declares, under the RBAC that
  `instructions` lists, and a namespace without the `datadouble-boundary`
  ConfigMap is refused `build`, `promote` and `lock` even when Jenkins'
  environment sets the mark.

The core's own tests cover what needs a clock or a race rather than a CI
system:

- `release --expired` collecting a copy past its expiry;
- pool recycling across generations;
- `replicate` never copying an image `fetch` is still writing.
