# Local development and testing

Every engine must be developable on a laptop, with sample data, without access to
any production system.

## Containers and sample data

A container profile per engine under `dev/`, each seeded with a well-known
sample dataset:

| Engine | Image | Sample data |
|---|---|---|
| PostgreSQL | official `postgres` | Pagila |
| MySQL | official `mysql` | Sakila (also Jailer's Demo-Sakila model) |
| SQL Server | Microsoft `mssql/server` (Developer edition) | Sakila |
| Oracle | Oracle Database Free | Sakila |
| MongoDB | official `mongo`, as a one-member replica set | Generated cinema database (`dev/mongodb/seed.js`) |
| Redis | official `redis` | Generated keyspace (strings, hashes, sets, streams) |
| Salesforce | none (see below) | Recorded org metadata and records |

Profiles can be started individually (`docker compose --profile postgres up`) so
nobody has to run Oracle to work on the Redis adapter.

The relational engines share one Sakila fixture: its rows are committed as
bulk-load files in `dev/sakila`, a Compose `fixtures` service puts them in a
volume, and each database container mounts it read-only and bulk-loads it at
start (decision 78). Fixture data never changes underneath the tests.

Sample datasets should include the awkward cases the subset planner must handle:
cycles, self-references, composite keys, polymorphic references, and tables with
no declared foreign keys.

## Salesforce

Salesforce cannot run in a container. Three layers cover it:

1. **Recorded interactions (VCR-style).** Adapter tests replay recorded HTTP
   request/response pairs ("cassettes") for describe calls, SOQL queries, and
   Bulk API export and load. This is a proven pattern in the Salesforce ecosystem
   (CumulusCI's test suite works this way with pytest-vcr and simple-salesforce).
   Requirements:
    - Scrub OAuth tokens, session IDs, instance URLs and org IDs before committing.
    - Support Bulk API 2.0's asynchronous polling, where the same request repeats
      with different responses, through ordered, repeatable matching.
    - Never assert on record IDs or timestamps; they differ per org.
2. **An in-memory fake behind a narrow interface.** The adapter's contact with
   Salesforce is limited to `describe`, `query`, `bulk_export` and `bulk_load`. A
   SQLite-backed fake implements those, with schemas from recorded `describe`
   output, giving the planner and load paths stateful tests (insert, then query
   it back).
3. **Scheduled re-recording against a scratch org.** Catches Salesforce API
   changes and cassette drift.

Only re-recording needs a Salesforce org; everything else runs offline.

## CI/CD systems

Each Tier 1 CI/CD system also has an opt-in container profile under `dev/ci/`
(GitLab CE and Runner, Jenkins, a kind cluster with Argo CD, and `act` for GitHub
Actions). See [CI/CD integration](ci-cd.md#testing).

## Conformance suite

Every adapter must pass a shared suite under `conformance/`, run against its
container (or fake): catalog introspection, consistent snapshot behavior, subset
correctness on the sample data, load round-trips, and excludes/strict-mode
behavior. CI/CD adapters have their own suite under `conformance/ci/`.
