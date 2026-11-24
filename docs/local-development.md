# Local development and testing

Datadouble supports many engines, and a contributor should be able to work on
any of them from a laptop. Every engine must be developable locally, with
sample data, without access to any production system.

This page gets you from a fresh clone to a passing test run. It then explains
what the tests run against: the containers and sample data for each engine,
the special arrangements for Salesforce, which cannot run in a container, and
the local runtimes for the CI/CD systems. It ends with the conformance suite
that every adapter must pass.

[TESTING.md](https://github.com/datadouble/datadouble/blob/main/TESTING.md)
lists every `make` target and testing rule, and [How our CI
works](how-our-ci-works.md) explains how CI runs the same tests.

## From a clone to a passing test run

### What you need

- [uv](https://docs.astral.sh/uv/), which installs Python and every
  development tool;
- Docker Engine with the Compose and Buildx plugins;
- GNU `make`;
- [actionlint](https://github.com/rhysd/actionlint), for `make
  lint-workflows`;
- [act](https://github.com/nektos/act), only if you run the GitHub Actions
  conformance suite (`make test-ci SYSTEM=github-actions`).

Some tests need more from the machine:

- Running Oracle and SQL Server together needs roughly 6 GB of RAM or more.
- The SQL Server and Oracle images are amd64-only.
- The btrfs and Kubernetes tests need privileged containers.

### 1. Install the workspace

```bash
git clone https://github.com/datadouble/datadouble.git
cd datadouble
uv sync
```

`uv sync` installs the right Python and every package in the workspace, with
the development tools.

### 2. Run the checks that need no containers

```bash
make check
```

`make check` runs lint, the workflow lint, the type checks and the unit
tests. The unit tests need nothing but Python, and they must keep branch
coverage of the core, the CI/CD adapters and the scripts at 100%. Every pull
request must pass `make check`.

### 3. Run one integration group

The integration tests run real databases in containers. They are split into
groups, mostly one per engine. Start with the group for the engine you are
working on:

```bash
make test-group GROUP=postgres
```

This builds the test image, starts only the Compose services that group
needs, waits until they are healthy, runs the group's tests, and removes the
services afterwards. The first run downloads the database images, so it is
slow. Later runs reuse them.

By default the tests drive a Docker daemon of their own, inside a container,
so they never touch your own containers and volumes. `DOCKER=host` uses your
own daemon instead, which is faster. [How our CI
works](how-our-ci-works.md#two-kinds-of-database-container) explains why the
tests need a daemon at all.

### 4. Run more only when you need to

You rarely need the whole suite locally. CI runs every group at once, far
faster than one machine can. Run more than your own engine's group only if
you changed the core, or code that more than one engine uses:

| Command | Runs |
|---|---|
| `make test-integration` | Every integration group in turn, the CI/CD conformance suite for every system, then the combined coverage with the 100% gate |
| `make test-k8s`, `make test-btrfs` | The Kubernetes and btrfs adapters against k3s and a real btrfs filesystem (privileged containers) |
| `make test-ci SYSTEM=<name>` | The CI/CD conformance suite for one system (see [CI/CD systems](#cicd-systems)) |
| `make docs` | The documentation site, in strict mode, which fails on broken links |

If a first run fails in a way the test output doesn't explain, check
TESTING.md's notes on your platform. For example, an Ubuntu host with MySQL
installed has an AppArmor profile that stops MySQL in Docker-in-Docker from
writing its files.

## Containers and sample data

The tests copy *from* source databases that stand in for production. Each
engine has one, defined as a service in `compose.yaml` and built from a
directory under `dev/`, and each is seeded with a well-known sample dataset:

| Engine | Image | Sample data |
|---|---|---|
| PostgreSQL | `pgvector/pgvector`, the official `postgres` image with pgvector | Pagila |
| MySQL | official `mysql` | Sakila (also Jailer's Demo-Sakila model) |
| SQL Server | Microsoft `mssql/server` (Developer edition) | Sakila |
| Oracle | Oracle Database Free (the full image, not `-lite`) | Sakila |
| MongoDB | official `mongo`, as a one-member replica set | Generated cinema database (`dev/mongodb/seed.js`) |
| Redis | official `redis` | Generated keyspace with every data type, such as strings, hashes, sets and streams |
| Elasticsearch | official `elasticsearch`, as a single node | Generated indices derived from Pagila (`dev/elasticsearch`) |
| OpenSearch | official `opensearchproject/opensearch`, as a single node | The same indices (`dev/opensearch`) |
| Salesforce | none (see [Salesforce](#salesforce)) | A fake org's records, and recorded org metadata and records |

**Start only what you need.** Nobody should have to run Oracle to work on the
Redis adapter. `make test-group` starts only its group's services. To start
one engine by hand, name its service:

```bash
docker compose up --detach --wait redis
```

Plain `docker compose up` starts every service except the privileged `k8s`
and `btrfs` profiles.

**The shared Sakila fixture.** The relational engines share one copy of
Sakila (decision 78):

1. Its rows are committed as bulk-load files in `dev/sakila`.
2. A Compose service, `fixtures`, puts them in a volume.
3. Each database container mounts that volume read-only and bulk-loads it at
   start.

So the fixture data is in no image and never changes underneath the tests.

**Awkward cases.** Sample datasets should include the awkward cases the
subset planner must handle:

- cycles;
- self-references;
- composite keys;
- polymorphic references;
- tables with no declared foreign keys.

## Salesforce

Salesforce cannot run in a container, so it needs a different approach.
Three layers cover it (decision 111). The first two run offline, and only the
third needs a real Salesforce org.

### 1. A fake org behind the real client

The adapter talks to Salesforce through a narrow client of its REST, Tooling
and Bulk API 2.0 interfaces. The client asks for the API versions, the object
list, describes, SOQL queries and exports.

The fake org (`datadouble_salesforce.fake`) is a SQLite file served over
those same HTTP calls. So the whole adapter, client included, runs offline.

- A spec names a fake org with a `connection` like this:

    ```yaml
    connection:
      fake: /path/to/org.sqlite
    ```

- `sample_org(path)` writes a small fake org with made-up records.
- The engine conformance suite runs against the fake, in the `salesforce`
  integration group, which needs no Compose services.

### 2. Recorded interactions (VCR-style)

A fake can only behave the way its authors expect. To check the adapter
against what Salesforce really sends, the tests also replay **cassettes**:
the client's recorded HTTP request and response pairs, replayed without an
org. This is a proven pattern in the Salesforce ecosystem. CumulusCI's test
suite works this way, with pytest-vcr and simple-salesforce.

- **The recorder scrubs before it writes.** It keeps only the headers replay
  needs. It replaces access tokens, session IDs, the instance URL and other
  Salesforce hosts, and org IDs.
- **Polling replays in order.** Bulk API 2.0 polls, so the same request
  repeats with different responses. Replay returns them in order, then
  repeats the last response.
- **Tests never assert on record IDs or timestamps,** because they differ
  from org to org.

### 3. Re-recording against an org that stands in for a source

Cassettes go stale when Salesforce's API changes, so a maintainer re-records
them from time to time. This is the only step that needs an org.

Use a Developer Edition org whose data looks like production data, as
Salesforce's own sample records do, so the PII scan has something to find.

1. **Log in to the org** with `sf org login web --alias <alias>`.
2. **Check that the org has records worth recording:** accounts with parent
   accounts, and contacts, opportunities and cases that refer to them.
3. **If it doesn't, seed it** with `make seed-salesforce ORG=<alias>`. This
   loads the records in `adapters/salesforce/tests/org/` with `sf data import
   tree`.
    - The records are invented but look real.
    - They fill fields that field-level security often hides, such as
      opt-outs, pronouns and gender identity. The seeding user needs edit
      access to those fields.
    - Every run adds another copy of them.
    - `SF_SEED_PLAN=plan-subscriptions.json` also loads subscriptions, for an
      org with the `Subscription__c` custom object.
4. **Record the cassette** with `make record-salesforce ORG=<alias>`. It
   writes `adapters/salesforce/tests/cassettes/org.json`.
    - `SF_OBJECTS` names the objects to record. The default is
      `Account,Contact,Opportunity,Case`.
    - `SF_HIDDEN` lists fields, as `Object.Field`, that field-level security
      may hide.
    - The recording user should be able to read every field, so the cassette
      holds them all.
5. **Record the hidden-fields cassette** with `make record-salesforce-hidden
   ORG=<alias>`. It writes `org-hidden.json`, which holds only the API
   versions and the catalog. It is for the test of hidden fields: the
   catalog fails on fields that field-level security hides, and names them
   (decision 113). So record it while the user lacks access to those fields,
   for example with the seeding permission sets unassigned. Then assign them
   again.

**What the recorder keeps.** The access token goes from `sf` to the recorder
through a pipe and is never written to disk. The recorder scrubs credentials
and the org's identity (its instance URL and ID), but keeps the records as
they are (decision 112).

A scheduled re-recording in CI, to catch API changes and cassette drift,
would need a connected app. [External
services](external-services.md#salesforce) describes how to set one up.

Only re-recording needs a Salesforce org. Everything else runs offline.
Loading a golden image into an org (`bulk_load`) comes later.

## CI/CD systems {#cicd-systems}

Datadouble's CI/CD adapters write jobs for GitHub Actions, GitLab CI, Jenkins
and Argo CD, the Tier 1 systems. Each of them runs its generated jobs locally,
with `make test-ci SYSTEM=<name>`:

| System | `SYSTEM` | Local runtime |
|---|---|---|
| GitHub Actions | `github-actions` | `act` |
| GitLab CI | `gitlab` | GitLab CE and a GitLab Runner, in containers built from `dev/ci/gitlab` |
| Jenkins | `jenkins` | Jenkins in a container built from `dev/ci/jenkins` |
| Jenkins with Kubernetes agents | `jenkins-kubernetes` | The same Jenkins, with agents on a kind cluster |
| Argo CD | `kubernetes` | A kind cluster with Argo CD |

[CI/CD integration](ci-cd.md#testing) describes these runtimes and what the
tests check.

## Conformance suite

Every engine adapter must pass a shared suite under `conformance/`, run
against its container (or, for Salesforce, its fake). The suite checks:

- catalog introspection;
- consistent snapshot behavior;
- subset correctness on the sample data;
- load round-trips;
- excludes and strict-mode behavior.

CI/CD adapters have their own suite, `datadouble_conformance.ci`.
