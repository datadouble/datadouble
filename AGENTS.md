# Guidance for AI coding agents

Datadouble makes realistic, safe copies of production data for development and
testing: extract with subsetting, anonymize, verify, package as a golden image,
store, and replicate. See the [README](README.md).

## Status

Design complete; version 1 is being built to [the v1 plan](docs/v1-plan.md).

## Building and testing

`make check` must pass before a pull request; [TESTING.md](TESTING.md) lists
every target and the testing rules. In short: `make lint`, `make
lint-workflows`, `make typecheck`, `make test` on the host, and `make
test-integration` for the Docker Compose suite. Never bind-mount the repository
into containers.

## Before proposing anything

1. Read [docs/decisions.md](docs/decisions.md). Decided items are settled; don't
   reopen them unless asked. Open questions are listed there too.
2. Read the design document for the area you're touching:
   - [requirements](docs/requirements.md)
   - [architecture](docs/architecture.md): stages, adapters, manifests, interfaces, repo layout
   - [subsetting](docs/subsetting.md)
   - [anonymization](docs/anonymization.md)
   - [excludes and strict mode](docs/excludes-and-strict-mode.md)
   - [Jailer compatibility](docs/jailer.md)
   - [CI/CD integration](docs/ci-cd.md): jobs, the production boundary, CI/CD adapters
   - [agent skills](docs/agent-skills.md)
   - [local development and testing](docs/local-development.md)
   - [prior art](docs/prior-art.md)
   - [v1 plan](docs/v1-plan.md): scope, milestones and acceptance criteria
   - [external services](docs/external-services.md): accounts and secrets CI needs

## Design principles to preserve

- **Engine breadth is a core goal:** PostgreSQL, MySQL, SQL Server, Oracle,
  MongoDB, Redis, Elasticsearch, OpenSearch, Salesforce and filesystems. Don't design anything that only works for one
  engine without saying how the others fit.
- **Assume terabyte-scale sources.** Subsetting is pushed down into extract and
  reads one consistent snapshot. Designs that require a full copy first don't work.
- **The core contains no engine-specific logic.** Engine quirks stay inside
  adapters behind narrow interfaces.
- **JSON in / JSON out is the primary interface.** The CLI only builds JSON
  requests; never add behavior that exists in one interface but not the other.
- **Fail loudly.** No silent configuration merges, and no warn-and-continue for
  anything that could put real personal data into a copy. `strict: columns` is
  the default.
- **Real personal data stays in production.** Only a verified golden image
  crosses out, through `promote`. CI/CD adapters and agent skills contain no
  pipeline logic; they only drive `datadouble` commands.
- **Wrap mature tools rather than reimplement them** (native dump tools, SFDMU,
  DBLab Engine, Jailer models).

## Working conventions

- Record new decisions in `docs/decisions.md` in the same change that makes them.
- Add a `CHANGELOG.md` entry under `[Unreleased]` for every user-visible change
  and every security fix, following [CONTRIBUTING.md](CONTRIBUTING.md#changelog).
- Keep design docs in sync with any change that alters behavior they describe.
- Never put credentials, real hostnames or real personal data in the repository,
  including in examples, fixtures and recorded API cassettes.
- Code is fully typed and must pass `mypy --strict`, `pyright` (strict) and
  `ruff`. No stringly-typed values: use enums, typed identifiers and parsed
  models (see [architecture](docs/architecture.md#typing)).
- Every source file starts with a copyright line and SPDX licence identifier,
  in the file's comment syntax:
  ```
  # Copyright (c) 2026- Tight Line LLC
  # SPDX-License-Identifier: MIT
  ```
- Commit messages: a short imperative subject line, then a body explaining why.
