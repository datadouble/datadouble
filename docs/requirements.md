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

2. **Pluggable adapters.** Each concern is handled by an adapter, so a team
   can swap one implementation for another. The concerns are:

    - **Dump and load:** native dump tools, ETL exports, CSV, APIs.
    - **Subsetting.**
    - **Anonymization:** Faker-style generators, hashing, nulling,
      format-preserving replacement, engine-native expressions such as SQL,
      and hooks for changes that aren't column-shaped. See
      [anonymization](anonymization.md).
    - **Verification:** referential integrity, row counts, PII scanning
      (looking for personally identifiable information).
    - **Storage,** for both inputs and golden-image outputs: local disk,
      object storage, artifact repositories, network shares.
    - **Replication,** which turns a golden image into many copies: Docker
      volumes, Kubernetes persistent volumes and CSI snapshots, copy-on-write
      filesystems, plain restore.

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
      The plan fails when the extract would contain a table or column the
      subset model does not know.
    - For functions, views, triggers and other objects, a change only warns,
      unless it breaks something.

    See [excludes and strict mode](excludes-and-strict-mode.md).

7. **Resumable, chainable stages.** One command can run several stages in a
   row. If a run fails, it resumes from the last stage that completed.

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
    - provisioning copies, as a ready pool or on demand;
    - announcing where the copies can be accessed;
    - releasing copies;
    - pruning old images;
    - rolling back to an earlier image.

    See [CI/CD integration](ci-cd.md).

11. **Agent skills.** A coding agent can set up any part of a pipeline, or a
    whole pipeline at once, in a team's own repositories. See
    [agent skills](agent-skills.md).

## Development

This requirement describes how the people who work on Datadouble test it.

12. **Local development with containers.** Every engine runs in a container
    with sample data. Salesforce cannot run in a container, so recorded API
    interactions and an in-memory fake cover it instead. See
    [local development](local-development.md).

## Non-functional

These rules apply to every part of Datadouble, whatever it does.

- **Credentials stay out of files and logs.** Credentials come from the
  environment, Vault or files. They never appear in configuration files,
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
