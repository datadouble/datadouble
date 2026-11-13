# Testing

How Datadouble is checked, locally and in CI. Every pull request must pass
`make check`; CI runs the same targets.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python and every tool)
- Docker Engine with the Compose and Buildx plugins
- GNU `make`
- [actionlint](https://github.com/rhysd/actionlint), for `make lint-workflows`

## Commands

| Command | What it runs | Where |
|---|---|---|
| `make check` | Everything below except the integration suite and the docs | Host |
| `make lint` | `ruff format --check`, `ruff check`, and `scripts/check_source.py` (copyright and SPDX headers, suppression reasons and budget) | Host |
| `make lint-workflows` | `actionlint` and `zizmor` over `.github/workflows`, and `scripts/check_workflows.py` (every job sets `timeout-minutes`) | Host |
| `make typecheck` | `mypy --strict` and `pyright` in strict mode | Host |
| `make test` | Unit tests, with the 100% branch coverage gate on the core and scripts | Host |
| `make test-integration` | The whole suite, integration and conformance tests included: every integration group in turn, then their combined coverage with the 100% gate on every package | Containers |
| `make test-group GROUP=<name>` | One integration group, as its CI job runs it (`DOCKER=host` uses the host's Docker daemon) | Containers |
| `make test-k8s`, `make test-btrfs` | The Kubernetes and btrfs adapters against k3s and a real btrfs filesystem (privileged) | Containers |
| `make docs` | Builds the documentation site in strict mode, which fails on broken links and anchors | Host |

## Test layers

**Unit tests** live next to the code they test (`core/tests/`,
`adapters/*/tests/`, `tests/scripts/`) and need nothing but Python. On the host
they must keep branch coverage of the core and scripts at 100%; the Compose
suite holds every package, adapters included, to 100%. The only
lines excluded from coverage are `assert_never(...)` arms (which the type
checkers prove unreachable), `if TYPE_CHECKING:` blocks and `__main__` guards.

**Integration tests** are marked `@pytest.mark.integration` and run inside the
Docker Compose services, which they reach through environment variables (for
example `DATADOUBLE_TEST_POSTGRES_DSN`). `make test` deselects them. They run
in groups; [How our CI works](docs/how-our-ci-works.md) explains them for
newcomers.

**Conformance suites** under `conformance/` must pass for every engine adapter
(see below), and for CI/CD and agent skill adapters as they arrive.

## Supported Python versions

Datadouble supports Python 3.13 and every later release. Lint, type checks and
the unit tests run on 3.13, the minimum, and the type checkers target it, so
code never uses a newer feature by accident. The unit tests also run on each
later release:

| Python | Role |
|---|---|
| 3.13 | Minimum; lint, type checks and unit tests |
| 3.14 | Unit tests |
| 3.15 | Unit tests; the version in the container images |

**When a new CPython release comes out,** add it to the `compatibility` matrix
in `.github/workflows/ci.yml` and to this table, then add its
`Unit tests (Python 3.x)` check to the required checks on `main`. Moving the
container images to it is a separate decision; Dependabot does not propose it.

## Integration groups and Compose services

The integration suite runs in groups, listed in `tests/integration-groups.txt`
with each group's user, number of shards, Compose services and test paths;
`tests/scripts/test_integration_groups.py` checks that every test path belongs
to exactly one. `make test-group GROUP=<name>` starts only that group's
services, waits for them to be healthy, and runs its tests; `make
test-integration` runs every group in turn and combines their coverage.

In CI each shard of a group is its own job: shard K of N (`make test-group
GROUP=<name>/K`, which passes `--shard K/N` to pytest, `scripts/shard.py`)
takes every Nth test in collection order, starting with the Kth, so the
shards together run each test once. A group gets more shards when its job
becomes the slowest; each shard pays the job's setup again (about two
minutes: the test image, the engine images, the source database), so a group
whose tests take a few minutes keeps one. Locally a group runs whole.

| Group | Tests | Services | User | Shards |
|---|---|---|---|---|
| `core` | `core/tests`, `conformance/tests`, `tests`, and the btrfs, Docker, Kubernetes, S3 and Vault adapters | PostgreSQL 18 with Pagila (`dev/postgres`), S3 (SeaweedFS), OpenBao | 999:999 | 1 |
| `postgres` | The PostgreSQL adapter | PostgreSQL, S3 | 999:999 | 1 |
| `mongodb` | The MongoDB adapter | MongoDB 9.0 as a one-member replica set with a generated cinema database (`dev/mongodb`) | 999:999 | 1 |
| `mysql` | The MySQL adapter and the Jailer comparison | MySQL 8.4 with Sakila (`dev/mysql`), Jailer's export of `dev/jailer/sakila.jm` (`dev/jailer`) | 999:999 | 2 |
| `mssql` | The SQL Server adapter | SQL Server 2022 (Developer edition) with Sakila (`dev/mssql`) | 10001:10001 | 2 |
| `oracle` | The Oracle adapter | Oracle Database Free 23.26 with Sakila (`dev/oracle`), loaded through external tables | 54321:54321 | 4 |

Sakila's rows come from the shared fixture (`dev/sakila`, through the
`fixtures` service). Two more Compose groups are profiles that need
privileged containers, opt-in and run in their own CI jobs: `k8s` (`make
test-k8s`: k3s, and a test runner reading its kubeconfig, sharing a work volume
that pods reach through a `hostPath` PersistentVolume) and `btrfs` (`make
test-btrfs`: a privileged test runner with a loop-mounted btrfs filesystem).
Their tests are marked `k8s` and `btrfs`; elsewhere they skip, and stand-in
`kubectl` and `btrfs` scripts give the unit tests full coverage of the
adapters.

The test runner holds no engine software. Working databases, restores and
engine tools run in the vendors' images, as the `docker` container runtime
runs them in production (`DATADOUBLE_TEST_RUNTIME` gives the runtime
settings), on one of two daemons:

- **Docker-in-Docker** (the default, on developers' machines): the `docker`
  service, isolated from the host's own containers. The runner and the daemon
  share the `work` volume at `/work`, where temporary files live (`TMPDIR`),
  because containers mount work directories at the same path. Compose service
  names do not resolve in those containers, so the tests connect to sources by
  address.
- **The host's daemon** (`DOCKER=host`, in CI): `compose.host.yaml` mounts the
  Docker socket, mounts a host directory (`HOST_WORK`, by default
  `/var/tmp/datadouble-work`, on disk where `/tmp` is often a small tmpfs in
  memory) at the same path for temporary files, joins engine containers to the
  Compose network, and reaches them through their published ports on the
  host. One daemon means one copy of each engine image. pytest keeps only
  failed tests' temporary directories, since working databases fill them.

The tests run as unprivileged users, as Datadouble should, and containers run
as the same user as the tests that start them. Datadouble runs as the user an
engine's copies are served as (decision 86), so `scripts/integration.sh`
starts as root only to run each group as its user, with the Docker socket's
group when the tests drive the host's daemon. Each group writes its own
coverage data, and the combined report must reach 100%. Working-database tests
for SQL Server therefore run only in Compose; its plan and extract code also
runs against a server on the host.

GitHub's Ubuntu runners, like other Ubuntu hosts with MySQL installed, ship an
AppArmor profile for `/usr/sbin/mysqld`. Containers that Docker-in-Docker
starts run without a profile of their own (it cannot apply one), so the host's
profile attaches to their `mysqld` by path and refuses to let it write working
databases under `/work`. Containers on the host's own daemon run under
Docker's default profile, which the host's does not override, so CI is
unaffected; anyone running the suite with Docker-in-Docker on a host with that
profile has to unload it (`sudo apparmor_parser --remove
/etc/apparmor.d/usr.sbin.mysqld`).

Pagila is fetched when `dev/postgres` is built, at a pinned commit and checked
against its SHA-256. It uses pgvector, so `dev/postgres` is built on the pinned
`pgvector/pgvector` image and the conformance spec names the same image in
`source.image`: the source and its working databases run exactly the same
server. The cinema database is generated
by `dev/mongodb/seed.js` when the service first starts. Sakila's rows are
committed as bulk-load files in `dev/sakila` (converted once from jOOQ's
collection by `scripts/sakila_bulk.py`); the `fixtures` service copies them
into a volume that `dev/mysql` and `dev/mssql` mount read-only and load (with
`LOAD DATA` and `BULK INSERT`) at start, so they are in no image and never
change underneath the tests. `dev/mssql` takes jOOQ's SQL Server schema, which
lacks MySQL Sakila's triggers; its awkward cases add one, with the cases
particular to SQL Server.

## Conformance suites

`conformance/` is the `datadouble-conformance` package. An engine adapter's
tests provide a `harness` fixture (the engine, a spec over its sample data, a
column that spec anonymizes, and a scratch source the suite may change) and
declare `class TestConformance(EngineConformance)`. The suite checks catalog
references, snapshot consistency, the strict-mode table, that both subset
strategies produce identical restored images, a whole build from plan to store,
a run killed with SIGKILL that resumes, and two image groups agreeing on
anonymized values.

## Rules

- **No bind mounts from the repository.** On machines whose home directories
  are network mounts, the Docker daemon cannot read them. Build what a
  container needs into its image, use named volumes for state, and copy results
  out of stopped containers (as `make test-integration` does with the coverage
  report).
- **Tests are hermetic.** A test creates what it needs (temporary directories,
  throwaway git repositories, its own database objects) and never depends on
  the state of the developer's checkout.
- **Suppressions carry a reason.** A `noqa`, `type: ignore`, `pyright: ignore`
  or `pragma: no cover` comment needs a reason after ` - `, and every use of
  `Any` or `cast` needs a `# why:` comment. `scripts/check_source.py` enforces
  this, and the total must equal the budget in `pyproject.toml`
  (`[tool.datadouble] suppressions`): a change that adds one raises the budget
  and its pull request says why, and a change that removes one lowers it.
  Prefer restructuring code to adding one; external programs run only through
  `datadouble.programs`, which holds the one subprocess suppression.
- **Everything has a time limit.** Each CI job sets `timeout-minutes`, a few
  times its normal run (`scripts/check_workflows.py` enforces this), and each
  test fails after 10 minutes with its stack (`pytest-timeout`), so a hang
  shows where it is stuck and never holds a pull request's checks for hours.

## In CI

| Workflow | Job | Runs |
|---|---|---|
| CI | Lint, types and unit tests | `make lint`, `make lint-workflows`, `make typecheck`, `make test`, on Python 3.13 |
| CI | Unit tests (Python 3.x) | `make test` on each later supported version |
| CI | k8s / btrfs adapters (privileged) | `make test-k8s`, `make test-btrfs` |
| CI | Integration jobs | Lists the integration jobs, one per shard of each group, from `tests/integration-groups.txt` |
| CI | Integration (group, or group K/N) | One job per shard, in parallel: `make test-group GROUP=<name>[/K] DOCKER=host`, with the test image's layers from GitHub's build cache (`compose.cache.yaml`); keeps the shard's coverage data as an artifact |
| CI | Integration coverage | Checks every shard passed, combines their coverage with the 100% gate, uploads it to Codecov and keeps the report for `sonar` |
| CI | sonar | `scripts/check_coverage.py` on the combined report (every analysed file named exactly once), then the scan, which waits for the quality gate and fails the job if it fails |
| CI | Documentation site | `make docs` |
| PR Images | Build, sign and attest PR image | Builds the runtime image, pushes it to GHCR, signs it with cosign and attests its SBOM and provenance |
| CodeQL, Dependency Review, Snyk Security, Scorecard | | Static analysis and dependency scanning |

Every job runs on a hardened runner that blocks outbound traffic except to the
endpoints it lists. A job that starts failing in a setup step with a refused
connection usually needs one more endpoint, and the comment beside the list
should say why it is there.
