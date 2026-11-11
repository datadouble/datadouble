# Decision log and open questions

## Decided

| # | Decision | Rationale |
|---|---|---|
| 1 | Name: **Datadouble** | Describes the job (a realistic stand-in for real data); free on PyPI, npm and GitHub |
| 2 | Pluggable adapters for dump/load, subset, anonymize, verify, storage, replicate | Breadth of engines is a core goal |
| 3 | Minimum systems: PostgreSQL, MySQL, SQL Server, Oracle, Redis, MongoDB, Salesforce | Required coverage |
| 4 | Subsetting is planned up front and pushed down into extract | Sources can be terabytes |
| 5 | All reads in a run share one consistent snapshot where the engine allows | Integrity of filtered tables |
| 6 | JSON in / JSON out is primary; CLI is a thin layer over it | Interfaces cannot drift |
| 7 | Stages write artifacts with manifests; runs are resumable and chainable | Requirement |
| 8 | Native Jailer `.jm` support, plus a converter that fails on schema mismatch | Design in Jailer, run repeatedly in Datadouble |
| 9 | Excludes by schema/table glob | Requirement |
| 10 | `strict: off \| tables \| columns`, default `columns`; removed/changed objects always fail | Unknown tables and columns are the usual PII leak |
| 11 | Containers and sample data for every engine; Salesforce via recorded interactions plus an in-memory fake | Local development requirement |
| 12 | One repository, each adapter a separately published package | Adapter interface will change heavily before 1.0 |
| 13 | ~~Repository private during planning~~ (superseded by 31) | Publish when ready |
| 14 | Licence: MIT | Maximum ease of adoption |
| 15 | CI/CD adapters, Tier 1: GitHub Actions, GitLab CI, Jenkins, Argo CD; each tested end to end | Datadouble mostly runs inside other teams' pipelines |
| 16 | Lifecycle split into jobs: build, promote, fetch, provision, release, prune, activate; stages `promote`, `fetch`, `announce` (named `publish` until decision 44); operations `release`, `prune`, `activate` | Jobs run in different environments, on different schedules, with different credentials |
| 17 | Everything up to `verify` runs where `extract` runs; `promote` into an export store is the only production crossing and refuses unverified images; non-production pulls from the export store only | Raw extracts and working databases contain real personal data |
| 18 | CI/CD adapters contain no pipeline logic and never put data artifacts in CI artifact storage | Same reason as the thin CLI; CI storage is often outside production |
| 19 | `build` refuses to generate or run on vendor-hosted runners | Prevents production data being pulled onto a vendor's machines |
| 20 | `verify` runs after `package`, against a scratch restore of the image | Verifies the artifact that ships, including that it restores |
| 21 | Run outcomes: success, warning, failure; anything that could leak personal data is a failure; no continue-on-error switch | Operators need "fix this soon" without weakening fail-loudly |
| 22 | Images identified by spec, engine, target engine version and generation; addressed through channels; image summaries (called sidecar manifests until decision 45) readable without download | Cheap change detection, rollback, and test channels that can't replace live images |
| 23 | `replicate` supports pool mode (N unclaimed copies, recycled by generation) and on-demand mode; copies are volumes or instances | Large images copy slower than people will wait; small ones suit per-request copies |
| 24 | Agent skills (Agent Skills format) for each job, the spec, and a whole pipeline; shipped in this repository as a Claude Code plugin and plain skill directories | Agents increasingly do pipeline setup; skills make it repeatable |
| 25 | Skills contain no logic: all files come from `datadouble` commands, and skills never weaken safety settings or handle secrets | Same reason as the thin CLI; an agent must not trade safety for a passing run |
| 26 | This repository is the Claude Code marketplace and the plugin; no separate marketplace repository and no packaging step | Skills stay versioned with the CLI and cannot drift; an org-wide marketplace can later reference this repository by tag |
| 27 | Implementation language: Python | Mature drivers for every target engine and the Faker ecosystem; the JSON interface keeps callers in other languages unaffected |
| 28 | Strict static typing: `mypy --strict` and `pyright` strict in CI, `ruff` with annotation rules; no stringly-typed values | Untyped dicts and magic strings make adapters hard to extend; types catch interface breaks before runtime |
| 29 | Anonymization strategies include engine-native expressions (SQL, MongoDB aggregation, Painless, Lua) as column rules; `after_transform` hooks that rewrite personal data declare the columns they rewrite | Expressions are often the simplest rule, and declaring columns keeps per-column hashes, reports and strict-mode resolution working |
| 30 | Version 1 scope is [the v1 plan](v1-plan.md), replacing the PostgreSQL-first proposal | Prove the core on one engine, then go wide in one long run |
| 31 | Everything is public: this repository, the documentation site, the example repository; the trademark and domain check is not a gate | Openness over caution for an MIT project |
| 32 | Copyright "(c) 2026- Tight Line LLC", MIT licence; every source file carries a copyright line and `SPDX-License-Identifier: MIT` | Clear ownership and licence on every file |
| 33 | Golden images are signed with cosign keyless signing in `build`; `promote` and `fetch` verify the signature | Non-production can check an image came from the production pipeline |
| 34 | Engines add Elasticsearch and OpenSearch, as separate adapters | Their APIs and clients have diverged since the fork |
| 35 | A filesystem engine adapter: selection by `find` expressions, cleanup by shell commands, a mandatory files PII scan | File trees that rows refer to are needed alongside databases and may hold personal data |
| 36 | Salesforce in version 1 works against the in-memory fake; cassettes are recorded later, outside the run, against a development org | The run never holds org credentials |
| 37 | `CHANGELOG.md` in Keep a Changelog format with an `[Unreleased]` section; every user-visible change and every security fix gets an entry; the release script refuses to tag with an empty `[Unreleased]` | Users learn what an upgrade changes, and security-only releases are possible and explained |
| 38 | Until 1.0, all packages are versioned and released together, with one changelog | One release gate and one changelog while the adapter interface is still changing; independent versions can follow once it is stable |
| 39 | Specs are accepted as YAML or JSON, both validated against one JSON Schema generated from the typed models | YAML for people editing by hand, JSON for programs; one schema keeps them identical |
| 40 | Golden image formats `physical` and `logical` are chosen in the spec (`package.formats`, CLI-overridable); omitted means every format the engine supports; an unsupported format fails the plan | Builds stay reproducible from spec and manifest, and no engine silently falls back |
| 41 | One build can produce several images through named groups in the spec; the extract is shared, each group gets its own working database, and all images from a build share a generation; image identity includes the group | Small images that are easy to reason about, plus combined ones, from one snapshot and without merging images later |
| 42 | Jenkins integration is a shared library in v1; a Jenkins plugin is a possible later addition | Avoids a Java plugin build and hosting process until users ask for it |
| 43 | The documentation site is generated from `docs/*.md`, with Design and Project sections live from M0 and Using Datadouble added in M10 | The docs stay the single source of truth, and the public site shows the design before there is software to document |
| 44 | The stage that tells consumers where clones can be accessed, as volumes or connection parameters, is named `announce` (was `publish`), and its adapters are announce adapters | It never writes data, and "publish" read as writing database dumps into targets such as Secrets or Vault |
| 45 | The small file stored next to each image, read by `fetch` without downloading the image, is the **image summary** (was "sidecar manifest") | "Sidecar" suggested a Kubernetes sidecar container |
| 46 | The documentation site is built with Zensical, pinned to an exact version | Material for MkDocs reached end of life on 5 November 2026; Zensical is its MIT-licensed successor from the same team, Python-based, and actively maintained, but still before 1.0 |
| 47 | Development tasks run through `make`; Python tooling runs through `uv`; tests and lint configuration live in the root `pyproject.toml` | One entry point that CI and people share |
| 48 | Python 3.13 is the minimum supported version and the type-checking target; CI runs the unit tests on every later release (3.14 and 3.15 now, new ones added as they come out); the container images run 3.15, and moving them to a new release is a deliberate change, not a Dependabot update | Installs from PyPI work on older CI runners and workstations, the images get the newest interpreter, and the matrix shows which dependencies lag a new release |

## Open

None at present.
