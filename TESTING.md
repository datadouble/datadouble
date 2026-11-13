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
| `make test-integration` | The whole suite, integration and conformance tests included, in the default Compose group, with the 100% gate on every package | Containers |
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
example `DATADOUBLE_TEST_POSTGRES_DSN`). `make test` deselects them.

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

## Docker Compose groups

Plain `docker compose up` starts the default group. Other groups are profiles
that milestones add and that combine freely, for example
`docker compose --profile mysql up`.

| Group | Services | Since |
|---|---|---|
| default | Test runner (Python, cosign, kubectl, the Docker CLI); PostgreSQL 18 with Pagila (`dev/postgres`); MongoDB 9.0 as a one-member replica set with a generated cinema database (`dev/mongodb`); MySQL 8.4 with Sakila (`dev/mysql`), loaded from the shared Sakila fixture (`dev/sakila`, through the `fixtures` service); SQL Server 2022 (Developer edition) with Sakila (`dev/mssql`), loaded from the same fixture; Jailer's export of `dev/jailer/sakila.jm` (`dev/jailer`, run once by the `jailer` service); S3 (SeaweedFS); Docker-in-Docker; OpenBao | M0; Pagila and S3 in M1; Docker and OpenBao in M2; MongoDB in M3; MySQL and SQL Server in M4 |
| `k8s` (`make test-k8s`) | k3s, and a test runner reading its kubeconfig, sharing a work volume that pods reach through a `hostPath` PersistentVolume | M2; the Kubernetes runtime in M4 |
| `btrfs` (`make test-btrfs`) | A privileged test runner with a loop-mounted btrfs filesystem | M2 |

The full list of planned groups is in the [v1 plan](docs/v1-plan.md#test-environment).

The `k8s` and `btrfs` groups need privileged containers, so they are opt-in and
run in their own CI job. Their tests are marked `k8s` and `btrfs`; elsewhere
they skip, and stand-in `kubectl` and `btrfs` scripts give the unit tests full
coverage of the adapters. The Docker adapter's containers go to the
Docker-in-Docker daemon, never to the host's.

The test runner holds no engine software. Working databases, restores and
engine tools run in the vendors' images on the Docker-in-Docker daemon, as the
`docker` container runtime runs them in production (`DATADOUBLE_TEST_RUNTIME`
gives the runtime settings). The runner and the daemon share the `work` volume
at `/work`, where temporary files live (`TMPDIR`), because containers mount
work directories at the same path. Compose service names do not resolve in
those containers, so the PostgreSQL tests connect to the source by address.
The suite runs as an unprivileged user, as Datadouble should, and containers
run as that user too.

GitHub's Ubuntu runners ship MySQL with an AppArmor profile for
`/usr/sbin/mysqld`. Containers that Docker-in-Docker starts run without a
profile of their own (it cannot apply one), so the host's profile attaches to
their `mysqld` by path and refuses to let it write working databases under
`/work`. The integration and SonarQube Cloud jobs therefore unload that
profile first (`apparmor_parser --remove`): the runner is throwaway and its
MySQL is never started. Remove those steps if the runner image stops shipping
the profile, or if the suite moves off Docker-in-Docker. Anyone running the
suite on a host with the same profile has to unload it too.

Pagila is fetched when `dev/postgres` is built, at a pinned commit and checked
against its SHA-256. It uses pgvector, so `dev/postgres` is built on the pinned
`pgvector/pgvector` image and the conformance spec names the same image in
`source.image`: the source and its working databases run exactly the same
server. The cinema database is generated
by `dev/mongodb/seed.js` when the service first starts. Sakila's rows are
committed as bulk-load files in `dev/sakila` (converted once from jOOQ's
collection by `scripts/sakila_bulk.py`); the `fixtures` service copies them
into a volume that `dev/mysql` mounts read-only and loads with `LOAD DATA` at
start, so they are in no image and never change underneath the tests.

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
| CI | Integration tests (Docker Compose) | `make test-integration`, then uploads coverage to Codecov |
| CI | Documentation site | `make docs` |
| PR Images | Build, sign and attest PR image | Builds the runtime image, pushes it to GHCR, signs it with cosign and attests its SBOM and provenance |
| SonarQube Cloud | sonar | `make test-integration` for coverage, `scripts/check_coverage.py` to check the report names every analysed file exactly once, then the scan, which waits for the quality gate and fails the job if it fails |
| CodeQL, Dependency Review, Snyk Security, Scorecard | | Static analysis and dependency scanning |

Every job runs on a hardened runner that blocks outbound traffic except to the
endpoints it lists. A job that starts failing in a setup step with a refused
connection usually needs one more endpoint, and the comment beside the list
should say why it is there.
