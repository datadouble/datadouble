# How our CI works

When you open a pull request, GitHub starts about forty jobs, and you can
merge only when the required ones are green. This page explains what those
jobs do, why they are arranged the way they are, and what to do when one of
them fails.

The page starts with a summary of a pull request's checks. Then it explains
the one idea the rest of the design follows from: the tests use two kinds of
database container. With that in place, the later sections cover how the
integration tests are split into groups and shards, how coverage is combined,
what is cached, how to run the same tests on your machine, and how to read a
red job.

[TESTING.md](https://github.com/datadouble/datadouble/blob/main/TESTING.md)
is the reference: it lists every `make` target and every testing rule. This
page is the tour.

## The short version

- Every pull request starts about forty jobs on GitHub's runners. They all
  start at once, so the pull request is ready when the **slowest** one
  finishes. In November 2026 that took about 17 minutes.
- Most jobs (lint, type checks, unit tests, security scans, the docs site,
  the container image) take a minute or two.
- The slow part is the **integration tests**, which run real databases in
  containers. They are split into **groups**, and slow groups into
  **shards**. Each shard is its own job, and they all run in parallel.
- A final job, **Integration coverage**, checks that every group passed and
  that, together, the groups test 100% of the code.
- The **sonar** job then sends that same coverage to SonarQube Cloud. It
  does not run the tests again.
- You can merge only when every required check is green, including Sonar's
  quality gate.

### What runs on a pull request

| Workflow | Jobs |
|---|---|
| CI | **Lint, types and unit tests** on Python 3.13; **Unit tests** on each later Python (3.14 and 3.15); **Claude Code plugin** validation; **Integration jobs**, which lists the integration shards; one **Integration** job per shard (20 today); one **CI/CD conformance** job per CI/CD system (five); **Integration coverage**; **sonar**; the **k8s** and **btrfs** privileged adapter jobs; **Documentation site** |
| PR Images | One job that builds the runtime image, pushes it to GitHub Container Registry, signs it with cosign, and attests its SBOM and provenance |
| CodeQL | Static analysis, one job for Python and one for the workflows |
| Snyk Security | Dependency and container scanning |
| Dependency Review | Checks the dependencies a pull request adds |

Some workflows also run at other times:

- **On merges to `main`:** CI, CodeQL, Snyk and OpenSSF Scorecard run again,
  and the Docs workflow deploys the site to GitHub Pages.
- **On a schedule:** CodeQL, Snyk and Scorecard weekly, and the nightly
  hosted run of the generated CI/CD workflows every night (see
  [CI/CD integration](ci-cd.md#testing)).
- **On a `v*` tag:** the Release workflow publishes the image, the Python
  packages and the GitHub release.

## Two kinds of database container

Most of what follows comes from one fact: the integration tests use two
different kinds of database container.

1. **Source databases** stand in for production. They are the databases the
   tests copy *from*: PostgreSQL with Pagila, MySQL, SQL Server and Oracle
   with Sakila, MongoDB with a generated cinema database, and so on. They are
   services in `compose.yaml`. Each job starts them once, and they hold the
   sample data.
2. **Working databases** are the containers *Datadouble itself* starts while
   it builds a golden image. It starts one to load the subset, one to
   anonymize a copy, one to package it, and one per image format to restore
   and check it. They run the database vendor's own image (decision 72), and
   they come and go during a test.

Because Datadouble starts working databases, the tests need a Docker daemon
that Datadouble can drive. Where that daemon comes from depends on where the
tests run:

- **In CI**, the tests use the runner's own Docker daemon
  (`compose.host.yaml`). The runner is thrown away after the job, so there is
  nothing to protect. One daemon also means each database image is downloaded
  only once.
- **On your machine**, the tests use a separate Docker daemon that runs
  inside a container (Docker-in-Docker). That way they can never touch your
  own containers and volumes. `make test-integration DOCKER=host` uses your
  own daemon instead, which is faster if you don't mind sharing it.

## The groups

Running every engine's tests in one job would take hours, and a failure would
not say which engine broke. So the integration suite is split into
**groups**, mostly one per engine. Each group starts only the source
databases it needs.

`tests/integration-groups.txt` lists the groups, and a unit test checks that
every test directory belongs to exactly one of them.

| Group | What it tests | Services it starts | Runs as | CI jobs |
|---|---|---|---|---|
| `core` | The core, the conformance harness, the scripts, the CI/CD adapters, and the btrfs, Docker, Kubernetes, S3, HTTP and Vault adapters | PostgreSQL, S3, Vault, an HTTPS export store | 999:999 | 1 |
| `postgres` | The PostgreSQL adapter | Two PostgreSQL servers, S3 | 999:999 | 1 |
| `mongodb` | The MongoDB adapter | MongoDB | 999:999 | 1 |
| `redis` | The Redis adapter | Redis | 999:999 | 1 |
| `elasticsearch` | The Elasticsearch adapter | Elasticsearch | 1000:0 | 2 |
| `opensearch` | The OpenSearch adapter | OpenSearch | 1000:1000 | 2 |
| `files` | The files adapter, beside PostgreSQL | PostgreSQL | 999:999 | 1 |
| `mysql` | The MySQL adapter, and the Jailer comparison | MySQL, Jailer's export | 999:999 | 2 |
| `mssql` | The SQL Server adapter | SQL Server | 10001:10001 | 2 |
| `oracle` | The Oracle adapter | Oracle | 54321:54321 | 6 |
| `salesforce` | The Salesforce adapter, against its fake org | None | 999:999 | 1 |

Each group runs in its own CI jobs. A slow engine does not hold the others
up, and a failure tells you at once which engine it was.

The table raises two questions: what the "CI jobs" column means, and why each
group runs as a different user. The next two sections answer them.

### Shards: one group, several jobs

Some groups take much longer than others. Oracle's tests, for example, add up
to about an hour. To keep the slowest group from setting the pace for the
whole pull request, a slow group is split into **shards**. Each shard is its
own job, and the shards run at the same time.

For example, **Integration (mysql 1/2)** runs about half of the MySQL tests,
and **Integration (mysql 2/2)** runs the rest. Together the shards run every
test exactly once.

**How the tests are divided.** Splitting by count alone would not work,
because some tests take much longer than others. An Oracle test that runs two
whole builds takes several minutes, and dividing by count could give one
shard all the slow ones. So the shards are balanced by how long each test
took in earlier CI runs, which `tests/integration-durations.json` records.

**Keeping the timings current.** Every **Integration coverage** job saves the
latest timings as the `integration-durations` artifact. When the shards'
times drift apart, download that artifact from a recent run on `main` and
commit it in place of `tests/integration-durations.json`.

**Why not shard everything.** Each extra job pays about two minutes of setup
again: building the test image, downloading the database image, and starting
the source database. So only groups whose tests take several minutes get
more than one shard.

**Changing the number of shards.** The number is a column in
`tests/integration-groups.txt`. Change it there and CI follows, because the
**Integration jobs** job reads that table to decide which jobs to start.

On your machine, groups run whole unless you ask for one shard, such as
`make test-group GROUP=mysql/2`.

### Why each group runs as a particular user

A physical golden image is the database's own files. When someone later
serves a copy of it, the database server opens those files as its own user:

- user 999 in the official PostgreSQL, MySQL and MongoDB images;
- user 10001 in SQL Server's image;
- user 54321 in Oracle's image.

If the files belong to anyone else, the server refuses them.

So Datadouble always runs as the user the engine's server runs as (decision
86), and the tests do the same. The test container starts as root only so
that it can switch to each group's user before running that group's tests.

## Coverage

Every line of code must be run by some test. This is the 100% gate.

No single group runs all the code, so coverage has to be combined across
groups. Each group's job records which lines its tests ran, and keeps that
record as a job artifact. The CI/CD conformance jobs do the same. Then the
**Integration coverage** job:

1. fails unless every integration shard and every CI/CD conformance job
   passed;
2. collects their coverage records;
3. combines them, so a line run by any group counts;
4. fails if the total is under 100%, listing the lines no test ran;
5. sends the combined report to Codecov, and passes it to the **sonar** job.

If you add code that only one engine reaches, that engine's group covers it.
Nothing else needs to.

## Caching, and what isn't cached

Every integration job needs the test image and several database images. This
section explains which of those CI caches, and why.

- **The test image is cached.** It holds Python and every package, and it is
  built with GitHub's build cache (`compose.cache.yaml`). A job rebuilds only
  the layers your change touched.
- **Database images are not cached.** They are large: SQL Server's is about
  1.6 GB, and GitHub's cache holds only 10 GB for the whole repository.
  Restoring a cached image is also no faster than downloading it from the
  vendor's registry. The registry is the cache.
- **Docker Hub images come through Google's mirror,** `mirror.gcr.io`
  (decision 138). Docker Hub limits how many images anonymous users can pull,
  counted per IP address. GitHub's runners share addresses, so a busy run
  could fail partway with `429 Too Many Requests`. To avoid that, each job
  that pulls images first points its Docker daemon at the mirror (the step
  **Pull Docker Hub images through its mirror**). If the mirror can't serve
  an image, Docker still pulls it from Docker Hub. Every image is pinned by
  digest, so the mirror can't change what runs.

## Running it yourself

You can run the same integration tests on your own machine. [Local
development](local-development.md) walks through a first run from a fresh
clone. These are the commands:

| Command | What it does |
|---|---|
| `make test-integration` | Every group, one after another, then the CI/CD conformance suite for every system, then the combined coverage with the 100% gate |
| `make test-group GROUP=mysql` | One group, as its CI job runs it |
| `make test-group GROUP=mysql/2` | Only the second of MySQL's shards, as its CI job runs it |
| `make test-group GROUP=mysql DOCKER=host` | The same group, on your own Docker daemon |
| `make clean-work` | Removes the temporary files a `DOCKER=host` run left behind (see below) |

The first run downloads every database image it needs, so it is slow. Later
runs reuse the images.

**You rarely need the whole suite.** If you changed one engine, run its group
and leave the rest to CI, which runs every group at once and finishes far
sooner. Run everything only if you changed the core, or code that more than
one engine uses.

**Temporary files with `DOCKER=host`.** These runs keep their temporary files
in `/var/tmp/datadouble-work`. That directory is on disk on purpose: `/tmp`
is often a small tmpfs held in memory, and working databases are large. A
failed test's files stay there so you can look at them. `make clean-work`
removes them, and so does the next `DOCKER=host` run.

## When a job goes red

Find the case that matches what you see.

**An Integration job failed.** Open that job's log. For a sharded group, open
the shard's own job, such as **Integration (mysql 2/2)**. pytest's summary at
the end of the log names the failing tests. The steps **Disk before** and
**Disk after** show the runner's free disk space, in case the group ran out.

**Integration coverage failed, but every group passed.** Coverage is under
100%. The log lists each file, and the line numbers in it that no test ran.

**sonar failed.** SonarQube Cloud's quality gate failed. The job log links to
the analysis, which says which condition needs attention: coverage on new
code, a new bug, or a security hotspot, for example.

**A job was cancelled after a long time.** Every job has a time limit
(`timeout-minutes`, decision 82), and every test has a limit of 10 minutes.
So a hang shows up as a timeout that names the stuck test, instead of holding
the pull request for hours.

**A setup step fails with a refused connection.** Every job blocks network
traffic except to the addresses it lists. A new download needs its address
added to that job's list, with a comment saying why it is there.

**Pulls fail with `429 Too Many Requests`.** This is Docker Hub's rate limit.
Check that the job has the mirror step, and that `mirror.gcr.io:443` is in
its list of allowed addresses.

**The Snyk check is red.** Red can mean that Snyk found something, or that it
rejected its token. Read the log before assuming which (see [external
services](external-services.md#snyk)).
