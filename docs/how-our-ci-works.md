# How our CI works

A plain-language tour of what happens when you open a pull request, why it is
built this way, and what to do when it goes red. [TESTING.md](https://github.com/datadouble/datadouble/blob/main/TESTING.md)
has the reference details: every `make` target and every testing rule.

## The short version

- Every pull request starts about fifteen jobs on GitHub's runners. They all
  start at once, so the pull request is ready when the **slowest** one
  finishes.
- Most jobs (lint, types, unit tests, security scans, the docs site, the
  container image) take a minute or two.
- The slow part is the **integration tests**, which run real databases in
  containers. They are split into **groups**, one job per group, running in
  parallel.
- A final job, **Integration coverage**, checks that every group passed
  and that, together, they test 100% of the code. The **sonar** job then
  sends that same coverage to SonarQube Cloud; it does not run the tests
  again.
- You can merge only when every required check is green, including Sonar's
  quality gate.

## Two kinds of database container

This is the idea everything else follows from.

1. **Source databases** are the pretend production databases the tests copy
   *from*: PostgreSQL with Pagila, MySQL, SQL Server and Oracle with Sakila,
   MongoDB with a generated cinema database. They are services in
   `compose.yaml`, started once per job, and they hold the sample data.
2. **Working databases** are the containers *Datadouble itself* starts while it
   builds a golden image: one to load the subset, one to anonymize a copy, one
   to package it, and one per image format to restore and check it. They run
   the database vendor's own image (decision 72), and they come and go during
   the test.

So the tests need a Docker daemon that Datadouble can drive, to start and stop
working databases.

- **In CI** the tests use the runner's own Docker daemon
  (`compose.host.yaml`). The runner is thrown away after the job, so there is
  nothing to protect, and one daemon means each database image is downloaded
  once.
- **On your machine** the tests use a separate Docker daemon running inside a
  container (Docker-in-Docker), so they can never touch your own containers
  and volumes. `make test-integration DOCKER=host` uses your own daemon
  instead, which is faster if you don't mind that.

## The groups

`tests/integration-groups.txt` lists them, and a unit test checks that every
test directory belongs to exactly one.

| Group | What it tests | Services it starts | Runs as | CI jobs |
|---|---|---|---|---|
| `core` | The core, the conformance harness, the scripts, and the btrfs, Docker, Kubernetes, S3 and Vault adapters | PostgreSQL, S3, Vault | 999:999 | 1 |
| `postgres` | The PostgreSQL adapter | PostgreSQL, S3 | 999:999 | 1 |
| `mongodb` | The MongoDB adapter | MongoDB | 999:999 | 1 |
| `mysql` | The MySQL adapter, and the Jailer comparison | MySQL, Jailer's export | 999:999 | 2 |
| `mssql` | The SQL Server adapter | SQL Server | 10001:10001 | 2 |
| `oracle` | The Oracle adapter | Oracle | 54321:54321 | 6 |

Each group runs in its own CI jobs, so a slow engine does not hold the others
up, and a failure tells you at once which engine it was.

### Shards: one group, several jobs

A slow group is split into **shards**, each its own job running at the same
time: **Integration (mysql 1/2)** runs about half the MySQL tests and
**Integration (mysql 2/2)** the rest. Together they run every test exactly
once.

The shards are balanced by how long each test took in earlier CI runs, which
`tests/integration-durations.json` records. Some tests take much longer than
others (an Oracle test that runs two whole builds takes several minutes), so
splitting by count alone would give one shard all the slow ones. Every
**Integration coverage** job saves the latest timings as the
`integration-durations` artifact; when the shards' times drift apart,
download it from a recent run on `main` and commit it in place of the file.

Each extra job pays about two minutes of setup again (building the test
image, downloading the database image, starting the source database), so
only groups whose tests take several minutes get more than one. The number
of shards is a column in `tests/integration-groups.txt`; change it there and
CI follows (the **Integration jobs** job reads the table). On your machine
groups run whole, unless you ask for a shard: `make test-group
GROUP=mysql/2`.

### Why each group runs as a particular user

A physical golden image is the database's own files. When someone later
serves a copy of it, the database server opens those files as its own user:
user 999 in the official PostgreSQL, MySQL and MongoDB images, 10001 in SQL
Server's, 54321 in Oracle's. If the files belong to anyone else, the server refuses them.

So Datadouble always runs as the user the engine's server runs as (decision
86), and the tests do the same: the test container starts as root only to
switch to each group's user before running its tests.

## Coverage

Every line of code must be run by some test (the 100% gate). Each group's job
records which lines its tests ran and keeps that record as a job artifact. The
**Integration coverage** job then:

1. collects every group's record,
2. combines them (a line run by any group counts),
3. fails if the total is under 100%, listing the lines no test ran,
4. sends the combined report to Codecov, and passes it to the sonar job.

If you add code that only one engine reaches, that engine's group covers it;
nothing else needs to.

## Caching, and what isn't cached

- **The test image** (Python and every package) is built with GitHub's build
  cache (`compose.cache.yaml`), so a job rebuilds only what your change
  touched.
- **Database images are not cached.** They are large (SQL Server's is about
  1.6 GB), GitHub's cache holds 10 GB for the whole repository, and restoring
  a cached image is no faster than downloading it from the vendor's registry.
  The registry is the cache.

## Running it yourself

| Command | What it does |
|---|---|
| `make test-integration` | Every group, one after another, then the combined coverage with the 100% gate |
| `make test-group GROUP=mysql` | One group, as its CI job does |
| `make test-group GROUP=mysql/2` | Only the second of MySQL's shards, as its CI job does |
| `make test-group GROUP=mysql DOCKER=host` | The same, on your own Docker daemon |
| `make clean-work` | Remove the temporary files a `DOCKER=host` run left behind (see below) |

The first run downloads every database image, so it is slow; later runs reuse
them.

You rarely need the whole suite on your own machine. If you changed one
engine, run its group and leave the rest to CI. Run everything only if you
changed the core or code more than one engine uses.

With `DOCKER=host`, the tests keep their temporary files in
`/var/tmp/datadouble-work` (on disk: `/tmp` is often a small tmpfs held in
memory, and working databases are large). A failed test's files stay there
so you can look at them; `make clean-work` removes them, and so does the next
`DOCKER=host` run.

## When a job goes red

- **Integration (group) failed:** open that job's log (for a sharded group,
  the shard's job, such as **Integration (mysql 2/2)**). pytest's summary at the
  end names the failing tests. The lines **Disk before** and **Disk after**
  show the runner's free disk space, in case a group ran out.
- **Integration coverage failed but every group passed:** coverage is
  under 100%. The log lists each file and the line numbers no test ran.
- **sonar failed:** SonarQube Cloud's quality gate failed. The job log links to
  the analysis, which says which condition (coverage on new code, a new bug, a
  security hotspot) needs attention.
- **A job was cancelled after a long time:** every job has a time limit
  (`timeout-minutes`, decision 82), and every test one of 10 minutes, so a
  hang shows up as a timeout naming the stuck test instead of holding the pull
  request for hours.
- **A setup step fails with a refused connection:** every job blocks network
  traffic except to the addresses it lists. A new download needs its address
  added to that job's list, with a comment saying why.
