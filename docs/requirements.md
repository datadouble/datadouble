# Requirements

This page lists what Datadouble must do. Each requirement is short and links to
the page that explains how Datadouble meets it.

The requirements come from one situation. Developers and test systems need
data that behaves like production, but they must never get real people's
data. Production systems are large, use many different engines, and change
over time. So Datadouble has to cut a big source down to a small, consistent
piece, make it safe, prove it is safe, and hand out copies, for every engine a
team runs.

The page goes from what Datadouble does (functional requirements), to how
people and systems drive it (interfaces), to how its developers work on it
(development), and ends with the rules every part must follow
(non-functional requirements).

## Functional

These requirements describe what a run does to data.

1. **Breadth of engines.** Datadouble supports at least these systems:

    - PostgreSQL, MySQL, Microsoft SQL Server and Oracle;
    - Redis and MongoDB;
    - Elasticsearch and OpenSearch;
    - Salesforce;
    - filesystem trees.

    Breadth is a core goal, not a stretch goal.

2. **Pluggable adapters.** Work that depends on one outside system lives in
   an adapter, so a team can add an implementation or swap one for another.
   Datadouble finds adapters through Python entry points, one group per kind:

    - **Engines,** which read a source and build its images, mostly by
      wrapping the engine's native dump tools: one for each system above.
    - **Stores,** which hold images: a local filesystem, S3 object storage,
      and HTTP for reading only.
    - **Replicators,** which turn a golden image into many copies: plain
      directories, Docker volumes, btrfs copy-on-write snapshots and
      Kubernetes persistent volumes.
    - **Announcers,** which tell consumers where copies are: dotenv files,
      CI/CD outputs, Kubernetes Secrets and Vault.
    - **Container runtimes,** which start the engine software: Docker and
      Kubernetes.
    - **CI/CD systems:** GitHub Actions, GitLab CI, Jenkins and Argo CD.

   Three concerns work the same way for every engine, so they are built into
   the core instead:

    - **Subsetting.**
    - **Anonymization:** Faker-style generators, hashing, nulling,
      format-preserving replacement, engine-native expressions such as SQL,
      and hooks for changes that aren't column-shaped. See
      [anonymization](anonymization.md).
    - **Verification:** referential integrity, row counts, PII scanning
      (looking for personally identifiable information) and the spec's own
      assertions.

3. **Subsetting during extract.** Sources may hold hundreds of gigabytes or
   terabytes, so copying everything first is not an option. Datadouble must
   push its filtering down to the source. The result must still be
   relationally consistent, and the run must transfer as little data as it
   can. See [subsetting](subsetting.md).

4. **Native Jailer support.** Many teams already describe their subsets as
   Jailer extraction models: a `.jm` file plus a data model folder.
   Datadouble can use such a model directly as the subset definition, and
   can convert it to Datadouble's own format. Conversion fails if the model
   does not match the schema. See [Jailer](jailer.md).

5. **Excludes.** A spec can leave out schemas and tables by name, with
   wildcards:

    ```yaml
    exclude:
      - "dba_maint_*"
      - "test_*"
    ```

6. **Strict mode, on by default.** Unmasked personal data usually leaks into
   copies through a table or column nobody reviewed. Strict mode guards
   against that:

    - For tables and columns it uses its strictest level (`data: columns`).
      The plan fails when the extract would contain a table or column that
      the catalog lock, the reviewed copy of the source's catalog, does not
      list.
    - For functions, views, triggers and other objects, a change only warns,
      unless it breaks something.

    See [excludes and strict mode](excludes-and-strict-mode.md).

7. **Resumable, chainable stages.** One command can run several stages in a
   row. If a run fails, resuming it skips every stage that completed with
   the same inputs.

## Interfaces

These requirements describe how people, pipelines and agents drive
Datadouble.

8. **Programmatic interface.** A program sends a JSON request and gets a JSON
   response.
9. **CLI.** The command line follows the usual form,
   `datadouble <command> --switch ... args`. It is a thin layer over the JSON
   interface, and every CLI invocation can print the JSON request it stands
   for.
10. **CI/CD adapters** for the major CI/CD systems. Tier 1 is GitHub Actions,
    GitLab CI, Jenkins and Argo CD, and each is tested end to end. Given a
    spec, a team can quickly wire up any of these jobs:

    - building the golden image inside production;
    - promoting it to an export store;
    - fetching it into non-production;
    - provisioning copies, as a ready pool or on demand, and announcing
      where they can be accessed;
    - releasing copies;
    - pruning old images;
    - rolling back to an earlier image;
    - re-locking the catalog after a schema change.

    See [CI/CD integration](ci-cd.md).

11. **Agent skills.** A coding agent can set up any part of a pipeline, or a
    whole pipeline at once, in a team's own repositories. See
    [agent skills](agent-skills.md).

## Development

This requirement describes how the people who work on Datadouble test it.

12. **Local development with containers.** Every engine that has a server
    runs in a container with sample data. Salesforce cannot run in a
    container, so recorded API interactions cover it instead, along with a
    fake org: a SQLite file served over the same HTTP APIs. See
    [local development](local-development.md).

## Non-functional

These rules apply to every part of Datadouble, whatever it does.

- **Credentials stay out of files and logs.** Credentials come from
  environment variables or files. They never appear in configuration files,
  artifacts, manifests or logs.
- **Every run explains itself.** Any run's output can be explained from its
  manifest, which records the source versions, the subset rules, hashes of
  the anonymization strategies and the verification results.
- **Real personal data stays where it was extracted.** It never leaves that
  environment. Only a golden image with a passing verification report
  crosses into non-production.
- **Fail loudly.** Datadouble never merges configuration silently, and never
  warns and carries on about anything that could put real personal data into
  a copy.
