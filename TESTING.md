# Testing

How Datadouble is checked, locally and in CI. Every pull request must pass
`make check`; CI runs the same targets.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 and every tool)
- Docker Engine with the Compose and Buildx plugins
- GNU `make`
- [actionlint](https://github.com/rhysd/actionlint), for `make lint-workflows`

## Commands

| Command | What it runs | Where |
|---|---|---|
| `make check` | Everything below except the integration suite and the docs | Host |
| `make lint` | `ruff format --check`, `ruff check`, and `scripts/check_source.py` (copyright and SPDX headers, suppression reasons) | Host |
| `make lint-workflows` | `actionlint` and `zizmor` over `.github/workflows` | Host |
| `make typecheck` | `mypy --strict` and `pyright` in strict mode | Host |
| `make test` | Unit tests, with the 100% branch coverage gate | Host |
| `make test-integration` | The whole suite, integration tests included, in the default Compose group | Containers |
| `make docs` | Builds the documentation site in strict mode, which fails on broken links and anchors | Host |

## Test layers

**Unit tests** live next to the code they test (`core/tests/`, `tests/scripts/`)
and need nothing but Python. They must keep branch coverage at 100%. The only
lines excluded from coverage are `assert_never(...)` arms (which the type
checkers prove unreachable), `if TYPE_CHECKING:` blocks and `__main__` guards.

**Integration tests** are marked `@pytest.mark.integration` and run inside the
Docker Compose services, which they reach through environment variables (for
example `DATADOUBLE_TEST_POSTGRES_DSN`). `make test` deselects them.

**Conformance suites** under `conformance/` arrive with the adapters (M1 on):
every engine, CI/CD and agent skill adapter must pass its suite.

## Docker Compose groups

Plain `docker compose up` starts the default group. Other groups are profiles
that milestones add and that combine freely, for example
`docker compose --profile mysql --profile s3 up`.

| Group | Services | Since |
|---|---|---|
| default | Test runner, PostgreSQL 18 | M0 |

The full list of planned groups is in the [v1 plan](docs/v1-plan.md#test-environment).

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
  this and prints the running total; prefer restructuring code to adding one.

## In CI

| Workflow | Job | Runs |
|---|---|---|
| CI | Lint, types and unit tests | `make lint`, `make lint-workflows`, `make typecheck`, `make test` |
| CI | Integration tests (Docker Compose) | `make test-integration`, then uploads coverage to Codecov |
| CI | Documentation site | `make docs` |
| PR Images | Build, sign and attest PR image | Builds the runtime image, pushes it to GHCR, signs it with cosign and attests its SBOM and provenance |
| CodeQL, Dependency Review, Snyk Security, SonarQube Cloud, Scorecard | | Static analysis and dependency scanning |

Every job runs on a hardened runner that blocks outbound traffic except to the
endpoints it lists. A job that starts failing in a setup step with a refused
connection usually needs one more endpoint, and the comment beside the list
should say why it is there.
