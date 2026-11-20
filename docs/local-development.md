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
| Oracle | Oracle Database Free (the full image, not `-lite`) | Sakila |
| MongoDB | official `mongo`, as a one-member replica set | Generated cinema database (`dev/mongodb/seed.js`) |
| Redis | official `redis` | Generated keyspace (strings, hashes, sets, streams) |
| Elasticsearch | official `elasticsearch`, as a single node | Generated indices derived from Pagila (`dev/elasticsearch`) |
| OpenSearch | official `opensearchproject/opensearch`, as a single node | The same indices (`dev/opensearch`) |
| Salesforce | none (see below) | A fake org's records, and recorded org metadata and records |

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

Salesforce cannot run in a container. Three layers cover it (decision 111):

1. **A fake org behind the real client.** The adapter's contact with
   Salesforce is a narrow client of its REST, Tooling and Bulk API 2.0
   interfaces: the API versions, the object list, describes, SOQL queries and
   exports. The fake (`datadouble_salesforce.fake`) is a SQLite file served
   over those same HTTP calls, so the whole adapter, client included, runs
   offline: a spec names it with `connection: {fake: /path/to/org.sqlite}`,
   and `sample_org(path)` writes a small one with made-up records. The engine
   conformance suite runs against it, in the `salesforce` integration group,
   which needs no Compose services.
2. **Recorded interactions (VCR-style).** Cassettes are the client's
   recorded HTTP request/response pairs, replayed in tests without an org.
   This is a proven pattern in the Salesforce ecosystem (CumulusCI's test
   suite works this way with pytest-vcr and simple-salesforce).
    - The recorder scrubs before it writes: only the headers replay needs are
      kept, and access tokens, session IDs, the instance URL and other
      Salesforce hosts, and org IDs are replaced.
    - Bulk API 2.0's polling, where the same request repeats with different
      responses, replays in order and then repeats the last response.
    - Tests never assert on record IDs or timestamps; they differ per org.
3. **Re-recording against a development org.** First check that the org
   has records worth recording: accounts with parent accounts, and contacts,
   opportunities and cases that refer to them. If it doesn't, `make
   seed-salesforce ORG=<alias>` loads the made-up records in
   `adapters/salesforce/tests/org/` with `sf data import tree`; every run adds
   another copy. Then `make record-salesforce ORG=<alias>` records `adapters/salesforce/tests/cassettes/org.json` from
   an org you have logged in to with `sf org login web --alias <alias>`;
   `SF_OBJECTS` names the objects (`Account,Contact,Opportunity,Case` by
   default). The access token goes from `sf` to the recorder through a pipe
   and is never written. A scheduled re-recording in CI, to catch API changes
   and cassette drift, needs a connected app (see
   [external services](external-services.md#salesforce)).

Only re-recording needs a Salesforce org; everything else runs offline.
Loading a golden image into an org (`bulk_load`) comes later.

## CI/CD systems

Each Tier 1 CI/CD system runs its generated jobs locally: `act` for GitHub
Actions (`make test-ci SYSTEM=github-actions`), and opt-in container profiles
under `dev/ci/` for GitLab CE and Runner, Jenkins, and a kind cluster with
Argo CD. See [CI/CD integration](ci-cd.md#testing).

## Conformance suite

Every adapter must pass a shared suite under `conformance/`, run against its
container (or fake): catalog introspection, consistent snapshot behavior, subset
correctness on the sample data, load round-trips, and excludes/strict-mode
behavior. CI/CD adapters have their own suite, `datadouble_conformance.ci`.
