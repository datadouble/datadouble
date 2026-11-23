# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Accounts copies carry, for logins over TCP**
  ([#70](https://github.com/datadouble/datadouble/issues/70)).
  `package.accounts` lists users, each with a password (plain text, since
  copies hold no personal data, or a secret reference) and `privileges`:
  `all` (the default; every privilege with the grant option, a PostgreSQL
  superuser) or `read` (`SELECT` on the image's data, plus what PostgreSQL's
  `PUBLIC` already has; never other accounts' password hashes). Physical
  images, and so every copy of them, get them: MySQL `'user'@'%'`,
  PostgreSQL a role with a SCRAM password and a password rule in
  `pg_hba.conf`. A consumer serving a volume copy with the engine's own image
  can then connect over the network, which it could not before: those images
  set their passwords only when a data directory is first made. Keep in mind
  that an `all` account on a reachable copy can run commands in the
  consumer's container, and that PostgreSQL images still trust local and
  loopback logins. The build logs in with each account before it stores the
  image; the image summary and manifest name the users, never the passwords;
  `ci init` passes the passwords' references to the build job; and a resumed
  or retried build packages again when a password changes. Names an engine
  can't use, duplicates, unset secrets, logical-only builds and engines
  without accounts fail at the start of the run, before extract.
- **`fetch` can read promoted images from a web server**
  ([#66](https://github.com/datadouble/datadouble/issues/66)). The new,
  read-only `http` store (`datadouble-http`) reads an export store that a web
  server serves, for non-production that reaches production's files only
  over HTTPS: `fetch.from: {http: {url: ..., auth: ..., ca_bundle: ...}}`,
  with basic auth or a bearer token given as secret references. `promote`
  still writes with the filesystem or S3 store, into the directory the server
  serves, and now also writes an index of each group's engine versions and
  generations (`<spec>/<group>/index.json`) on every export store;
  `prune --target export` keeps it current. Writing the index lists each
  export store, so `promote`'s credentials for an S3 export store need
  `s3:ListBucket`. Web servers cannot list files, so the `http` store reads
  the index instead. Writing through it fails,
  naming the store, and its errors name the URL path and HTTP status, never
  response bodies or credentials. Promote again into an existing export store
  to give it an index before reading it over HTTP.
- **A manual `lock` CI job, and lock changes**
  ([#67](https://github.com/datadouble/datadouble/issues/67)). `ci init --job
  lock` writes a job on the production side, on the build's runner with only the
  sources' and `notify`'s secrets and no schedule, for every Tier 1 system. It
  runs `datadouble lock --output datadouble-lock` and publishes the new lock and
  its changes as the job's artifact (on Argo CD, in the job's log), and never
  commits: someone checks the changes for personal data and commits the lock.
  `datadouble lock` now lists `changes` against the lock it replaces (tables,
  columns, keys and requirements added, removed or changed), in its JSON
  response and its summary, and `--output DIR` writes the new lock and
  `changes.txt` there instead of replacing the lock.
- **Generated CI jobs notify on failure and warning**
  ([#69](https://github.com/datadouble/datadouble/issues/69)). A spec's `notify`
  section lists the outcomes (`when: [failure, warning]`, failures only by
  default) and the targets. A `webhook`, whose URL is a secret reference, gets a
  JSON POST from Datadouble itself on every system: the spec, job, outcome,
  generation, error code and the run's page, never data values or secrets; posts
  follow the environment's proxy settings, and credentials in the URL need
  https. `email` uses the system's own mail: Jenkinsfiles mail from `post`,
  GitLab and GitHub need the setup `ci init` lists in `instructions`, and Argo
  CD refuses it. A notification that fails is a warning in the job's log and
  never changes its outcome. It lives in the spec so running `ci init` again
  keeps it; run `ci init` again after adding it, so every job gets the webhook's
  variable. Generated jobs now name their job to the native step (`--ci-job`).
- **CI steps can use another Docker command and socket**
  ([#71](https://github.com/datadouble/datadouble/issues/71)). The GitHub
  action, the GitLab job template and the Jenkins step read
  `DATADOUBLE_DOCKER` (the command that runs the image, `docker` by default)
  and `DATADOUBLE_DOCKER_SOCKET` (the daemon's socket,
  `/var/run/docker.sock` by default), so runners with Docker installed
  outside `PATH` or another Docker-compatible daemon on another socket work
  without editing generated files. Set them once in a runner's environment,
  or pass `ci init --docker` and `--docker-socket` to write them into the
  generated configuration. Without either, a `unix://` `DOCKER_HOST` names
  the socket. The image is started on that daemon and sees its socket at
  `/var/run/docker.sock`. Rootless Docker is not supported, and Argo CD's
  jobs, which run without Docker, refuse both options.
- **The Kubernetes adapters take their kubeconfig as a secret reference**
  ([#68](https://github.com/datadouble/datadouble/issues/68)). `k8s-pv`,
  `k8s-secret` and the `kubernetes` runtime accept `kubeconfig: {env: NAME}`
  (or `{file: /path}` for a mounted secret file), the kubeconfig's content,
  and an optional `context`, so a CI job can drive a cluster other than the
  one it runs in. Datadouble writes the kubeconfig to a private temporary
  file for each `kubectl` call and removes it afterwards, even after a
  failure or a cancelled job. Its values of 16 characters or more are
  redacted from `kubectl`'s output and are all CI systems mask. `ci init`
  lists the variable among the secrets of the jobs that use it. Exec
  credential plugins are not in the image: use token or certificate
  credentials. Without the setting, nothing changes.
- **A cancelled job cleans up.** `datadouble` turns SIGTERM, which CI
  systems send to cancel a job, into a normal exit, so temporary
  credentials and containers are removed.
- **Jenkins jobs can run as Kubernetes pods**
  ([#72](https://github.com/datadouble/datadouble/issues/72)). `ci init
  --system jenkins --agent kubernetes` writes Jenkinsfiles whose agent is a
  pod of the Kubernetes plugin, for installations whose agents have no
  Docker and for specs on the `kubernetes` runtime. As for Argo CD,
  `--runner` names the side's namespace, whose `datadouble` claim the pod
  mounts; the new `--cloud` names the plugin's cloud. Keep the two sides in
  separate namespaces, ideally clusters. The pinned Datadouble image runs in
  the pod as the engine's user, and the step runs `datadouble` in it. Jobs
  that start engines need the spec's `kubernetes` runtime in that namespace
  and claim, with every path under its mount. The boundary mark is only the
  namespace's `datadouble-boundary` ConfigMap, read from a mounted file.
  `instructions` list the plugin, the claim and the RBAC. Agents with Docker
  stay the default; other systems refuse `--agent kubernetes` for now.
- **Jenkins Kubernetes agents are tested end to end**
  ([#84](https://github.com/datadouble/datadouble/issues/84)). A CI/CD
  conformance job, `make test-ci SYSTEM=jenkins-kubernetes`, runs every
  generated job of both sides as agent pods on a kind cluster, with the pods
  as written, the service accounts and Roles `instructions` describe, and
  engine pods sharing the claim. A namespace without the
  `datadouble-boundary` ConfigMap is refused `build`, `promote` and `lock`
  though Jenkins' own environment sets the mark. One spec serves both
  sides, each in its own namespace, and each side's engine pods start in
  its namespace.
- **Several servers of one engine in one spec**
  ([#77](https://github.com/datadouble/datadouble/issues/77)). A spec's
  `sources` names one source per server; PostgreSQL and MySQL copy every
  source's databases into one image and one working server per group, and other
  engines read one source per spec. The sources must share an engine and a
  compatibility line (`17` for PostgreSQL, `8.4` for MySQL; the working server
  runs the newest of their versions), give the same `image` and `server`, hold
  no database name in common (empty databases included), and agree on
  requirements: one used at two versions, or omitted by one source and used by
  another, fails the plan naming both. Each source is read under its own
  snapshot, opened together, and the image's manifest records each source's
  snapshot and when it was opened (`sources`), since such an image is not one
  point in time. A virtual reference between sources is read with the keys
  strategy and listed in the plan and manifest (`between_sources`).
  Anonymization is keyed once per run, so equal values get equal replacements in
  every source. Each source's credentials are its own secret references, and `ci
  init` gives the build job all of them.
- **Several databases from one server in one source**
  ([#76](https://github.com/datadouble/datadouble/issues/76)). MySQL and
  PostgreSQL take `source.connection.databases`, a list of names or globs
  (`["*"]` for every database), and read them all into one catalog; working
  databases, images and copies hold every one under its own name. MySQL reads
  them under its one consistent snapshot, so foreign keys, views and triggers
  between databases are copied as they are; views over a database that is
  not read are left out. PostgreSQL cannot share a
  snapshot between databases, so it reads each under its own, opened
  together, records each in the manifest (`snapshots`), and fails the plan on
  a virtual reference between two databases. A database a glob picks up
  later is new tables to strict mode. Hooks and assertions run in a default
  database: the connection's own when it is read, otherwise the first read.
  The MySQL URL's database is now optional when `databases` is given.
- **Functions, views, triggers and other objects in the catalog**
  ([#78](https://github.com/datadouble/datadouble/issues/78)). PostgreSQL,
  MySQL, SQL Server and Oracle report their functions and procedures
  (Oracle's packages too), views, triggers, types, sequences and events,
  each with a fingerprint of its definition and the tables and objects it
  depends on, named like tables. A MySQL source's objects come from every
  database it reads, with edges across them; a PostgreSQL source's are read
  per database, under that database's snapshot. Edges come from the
  engine's own records where it keeps them
  (`pg_depend`, `sys.sql_expression_dependencies`, `ALL_DEPENDENCIES`,
  MySQL's view usage tables); the rest are parsed from bodies and marked
  inferred. The lock records them, `datadouble lock` lists objects added,
  removed or changed in its `changes`, and strict mode warns on a new, changed
  or removed one by default, per kind under `strict`. Removing an object
  that a remaining one depends on, such as a function a trigger calls,
  fails at every level, naming both; a removal that only inferred edges
  point at warns. Definitions are fingerprinted as written, apart from line
  endings and trailing spaces. Other engines report none, and their locks
  stay valid. **Re-lock PostgreSQL, MySQL, SQL Server and Oracle sources**
  with `datadouble lock`: older locks have no objects, so every object warns
  as new. An object that changes between planning and extracting stops the
  extract, as a changed table does. The MySQL adapter now needs MySQL
  8.0.13 or later and says so.
- **Package planning: each image works on its own with `standalone: true`**
  ([#79](https://github.com/datadouble/datadouble/issues/79)). After the
  subset plan, the plan works out per group what its image needs: from its
  tables, the objects that come with them (triggers, views over them alone)
  and the objects the spec or the group declares in the new `objects:` list
  (`{function: public.uuid_generate_v4}`), it follows followed references,
  what tables need and every object's dependencies, across schemas and
  databases. A group with `standalone: true` pulls what it reaches into its
  image and holds exactly that closure: tables with the rows its rows
  reference, and objects created from their definitions in dependency
  order, no others. `rows:` on the group overrides a pulled-in table's rows
  (`referenced`, `all` for every source row, or `none`), keyed by a table or
  glob or by a reference; a table reached only through objects comes
  without rows, with a warning. Pulled-in rows are read in the same snapshot
  into the group's own extract, so other groups' images are unaffected. The
  plan lists each group's declared objects, the objects its image needs and
  what it pulls in with the path that reached it; an object that needs an
  excluded table, or one the catalog does not list, fails the plan naming
  the path, or warns when an inferred edge is on it. Strict mode also fails
  on a removed object a packaged table or declared object still needs, and
  on a new or changed object on a path to an excluded table. PostgreSQL,
  MySQL and SQL Server build standalone images; Oracle and engines without
  objects refuse them by name for now.
- **Image groups' `include` patterns place objects too**
  ([#79](https://github.com/datadouble/datadouble/issues/79)). A group that
  is not standalone and needs a function, view or other object another
  group's patterns match now fails the plan, as a reference into another
  group's table does; add `requires:` for that group, or make the group
  standalone. Objects that come with a group's own tables, such as triggers
  on them, and its declared objects are its own. A declared object that
  needs a table the group and the groups it requires lack fails the plan.
  A standalone group cannot have `requires:`.
- **Tables list the objects they need.** The catalog records the sequences and
  functions tables' column defaults, generated or computed columns, check
  constraints (and on PostgreSQL index expressions and row-level security
  policies, with the tables those policies read) use, and their columns'
  user-defined types: exactly from `pg_depend` and SQL Server's dependency
  views, from `ALL_DEPENDENCIES` on Oracle (whose sequence defaults are parsed,
  so inferred), and none on MySQL, which has no such objects. A table that needs
  nothing hashes as before, so locks stay valid until a table that needs an
  object is locked.
- **Salesforce:** an engine adapter (`datadouble-salesforce`) that reads an
  org through its REST, Tooling and Bulk API 2.0 interfaces, with an
  instance URL and access token by reference. Each object is a table and
  `Id` its key; references to one object are foreign keys, and polymorphic
  ones are declared as virtual foreign keys. Seeds, `when` clauses and
  assertions are SOQL. Working copies, anonymization and checks run locally
  in SQLite, and images are logical: each object's records as JSON lines.
  Installed packages are reported as `package` requirements, which the copies
  do not need. A fake org, a SQLite file served over the same APIs
  (`connection: {fake: path}`), runs the adapter without an org, and
  `make record-salesforce` records a cassette of its calls from an org that
  stands in for a source, scrubbed of credentials and the org's identity;
  `make seed-salesforce` loads it with invented records that look real.
  Fields that field-level security hides from the org user fail the plan,
  naming each one, unless `source.connection.hidden` lists them.
- **Requirements for Oracle.** The index types of the schemas' domain
  indexes, such as Oracle Text's `CTXSYS.CONTEXT`, are options the working
  image must provide. Omitting one leaves its indexes out of the copies. SQL
  Server and MongoDB report none, as their READMEs explain.
- **Requirements for MySQL, Redis, Elasticsearch and OpenSearch.** MySQL
  reports storage engine and full-text parser plugins and installs them in
  working servers; Redis reports modules; Elasticsearch and OpenSearch
  report plugins. Listing Elasticsearch plugins needs the `monitor` cluster
  privilege.
- **Source requirements.** The catalog lists the add-ons a source uses
  beyond its server, starting with PostgreSQL extensions, at the source's
  versions. The lock records them and strict mode treats a change like a
  table change. A build checks that the working image has each one before
  it reads the source, and fails with `requirement_unmet` naming every gap.
  PostgreSQL copies create extensions at the source's versions.
  `source.omit` leaves named ones out, and PostgreSQL's `source.server`
  takes `shared_preload_libraries`.

- **Using Datadouble documentation.** The site gains a quickstart, a spec
  reference generated from the JSON Schema, a CLI reference generated from
  the CLI, and guides to the CI/CD libraries, the agent skills and the
  example repository. The spec's JSON Schema now describes each field.
- **Agent skills.** A Claude Code plugin (this repository is its marketplace)
  with skills that set up a whole pipeline, a spec, file trees, and each job,
  following shared rules: configuration only from `ci init`, no weakened
  safety settings, no secrets handled and no vendor-hosted runners for
  production jobs. `/plugin install datadouble --marketplace
  datadouble/datadouble`.
- **CI/CD jobs, starting with GitHub Actions.** `datadouble ci init` writes a
  side of the boundary's jobs for a spec (`build`, `promote`, `fetch`,
  `provision`, `release`, `prune`, `activate`), with schedules, chaining,
  concurrency and per-job secrets referenced by name. The GitHub Actions
  adapter (`datadouble-github-actions`) writes a workflow per job using the
  action at `ci/github-actions`, which runs the Datadouble image on a
  self-hosted runner. `ci init` refuses GitHub's own runners for `build` and
  `promote`.
- **GitLab CI.** The GitLab adapter (`datadouble-gitlab`) writes a file of
  jobs per side of the boundary, including a job template pinned by tag and
  SHA-256, with jobs chosen by `DATADOUBLE_JOB` from pipeline schedules or
  manual runs, and on-demand copies in merge request environments that are
  released when the request is merged or closed. Warnings are GitLab's
  allowed failures (exit code 3), and outputs a dotenv report. `ci init`
  lists the setup GitLab's configuration cannot hold, such as schedules.
- **Jenkins.** The Jenkins adapter (`datadouble-jenkins`) writes a declarative
  Jenkinsfile per job, which loads the shared library in `ci/jenkins` at the
  release's tag and calls its `datadouble` step. Schedules are UTC `cron`
  triggers, `promote` follows `build` through an `upstream` trigger, secrets
  are bound credentials, and a warning marks the build unstable. On-demand
  copies take an `OWNER` parameter, and a `RETRY_KEY` parameter resumes a
  failed build.
- **Argo CD and Kubernetes.** A Helm chart (`ci/kubernetes/chart`) runs
  Datadouble's jobs as CronJobs, and the Argo CD adapter
  (`datadouble-argocd`) writes an Application per side of the boundary that
  installs it with the jobs, the spec and its lock as values. A following
  job runs as a later step of its leader's pod, on-demand copies are PreSync
  and PostDelete hooks on a request's Application, and the boundary mark is a
  ConfigMap the cluster's operator creates.
- **Reporting to the CI system.** `--ci SYSTEM` (or `ci` in a JSON request)
  masks the secret values Datadouble resolves in the system's logs and
  reports the response as step outputs, a job summary and the system's own
  success, warning or failure state.
- **Retries resume.** `run --retry-key KEY` resumes the generation an earlier
  attempt with the same key started, so a retried CI run picks up where it
  failed.
- **Hosted runners are refused at run time.** Reading a source and `promote`
  fail on a machine a CI/CD adapter recognizes as its vendor's hosted runner,
  such as GitHub's, even when it is marked inside the boundary.
- **The production boundary mark.** Reading a source and `promote` run only
  where `DATADOUBLE_BOUNDARY=inside` is set, so a runner must be marked as
  inside the production boundary before it can read production data.
- **Files:** a spec's `files` section copies the file tree its rows refer to,
  as a second image beside the database's, through the `files` engine adapter
  (`datadouble-files`). Each top-level directory is selected with `find`
  tests or excluded, the files the chosen rows name are copied too
  (`paths_from`, a template over a table's columns with an optional pattern
  for URLs), `cleanup` commands run on the copy, and the PII scan reads
  every file's text.
- **OpenSearch:** an engine adapter (`datadouble-opensearch`) for OpenSearch
  2.4 or later, sharing the Elasticsearch adapter's code: the same tables,
  subsets, images and rules, with OpenSearch's image, security plugin and
  point-in-time API.
- **Elasticsearch:** an engine adapter (`datadouble-elasticsearch`) for
  Elasticsearch 7.17 or later. Each index is a table keyed by `_id`, with its
  mapping's field paths as columns, and the extract reads every index through
  one point in time. Images are a snapshot repository (`physical`) or NDJSON
  with each index's mappings and settings (`logical`). Expression rules are
  Painless scripts. A copied document holding a field the catalog does not
  list fails the extract.
- **Redis:** an engine adapter (`datadouble-redis`). Each key pattern is a
  table (`user:{}`, `user:{}:sessions`), with `_key` holding the values its
  `{}` stand for, so references in key names and in values are declared like
  any other. The extract reads only the chosen keys; images are the
  `dump.rdb` a working server saves. Key segments that look like data fail
  the catalog until a pattern is declared for them, so they never become
  table names.
- **The `datadouble` package and command.** `datadouble version` reports the
  installed version, `--json` prints the JSON response instead of a summary,
  `--dry-run` prints the JSON request without running it, and `datadouble api`
  reads a JSON request from standard input. Every later command follows the
  same pattern, because the command line only builds JSON requests.
- **A container image, `ghcr.io/datadouble/datadouble`,** signed with cosign
  keyless signing and published with SBOM and build provenance attestations.
  It carries Datadouble, its adapters, the `docker` command, `kubectl` and
  cosign, and no engine software.
- **A documentation site** at <https://datadouble.tightlinesoftware.com/>,
  generated from the design documents.
- **Python support:** 3.13 or later, tested on 3.13, 3.14 and 3.15. The
  container image runs Python 3.15.
- **Specs** in YAML or JSON, validated against one JSON Schema
  (`datadouble schema`), with secrets referred to by environment variable or
  file, never written into the spec.
- **`datadouble lock`** writes `catalog.lock`, and **strict mode** compares the
  live catalog with it at plan time: `columns` (the default) fails on new
  tables and columns, `tables` on new tables, `off` only warns. Removed or
  changed objects always fail.
- **Excludes and structure-only tables** by shell-style patterns, resolved once
  and recorded.
- **`datadouble plan`**: the subset planner, with starting filters (`seeds`),
  followed child edges, virtual references (optionally conditional), per-edge
  fallbacks (`follow: all`, `null`, `stub`, `drop`), image groups with
  `requires:`, and both strategies: nested queries pushed to the source, or
  key collection with the closure computed by Datadouble. Every problem in a
  spec is reported at once.
- **`datadouble run`** for the build stages (`plan`, `extract`, `load`,
  `transform`, `package`, `verify`, `store`), with a manifest per stage and
  `--resume GENERATION` to continue a stopped run from its last completed stage.
- **Anonymization:** deterministic faker values, keyed hashes, null,
  format-preserving replacement and engine-native expressions, applied in one
  native update per table; `before_load` and `after_transform` hooks.
- **Verification** of every image format after a scratch restore: row counts,
  references, a PII scan and the spec's assertions.
- **Golden images** packed per format with their build manifest, stored with an
  image summary and the verification report.
- **Engine software in the vendor's images.** Working databases, restores and
  tools such as `pg_dump` run in containers from the engine vendor's official
  image of the source's version, or the image in `source.image`, started by a
  container runtime named in the spec's `runtime:`: `docker` for any
  Docker-compatible daemon, or `kubernetes` for pods sharing the work
  directory's PersistentVolumeClaim. Working servers accept only a password
  generated for each start. Failures are reported as `container_failed`.
- **`datadouble-postgres`**, the PostgreSQL engine adapter: one exported
  snapshot for keys, rows and schema; working databases as throwaway clusters
  in the official PostgreSQL image; physical (data directory) and logical
  (`pg_dump`) images.
- **`datadouble-mongodb`**, the MongoDB engine adapter: collections and dotted
  field paths as tables and columns, from sampling and `$jsonSchema`
  validators; extract in one snapshot session with the keys strategy; seeds,
  conditions and assertions in MongoDB's own query and aggregation language;
  physical (data directory) and logical (`mongodump` archive) images.
- **`datadouble-mysql`**, the MySQL engine adapter: one consistent-snapshot
  transaction for keys and rows, the schema from `mysqldump`, working
  databases as throwaway servers in the official MySQL image, and physical
  (data directory) and logical (`mysqldump`) images.
- **`datadouble-mssql`**, the SQL Server engine adapter: keys, schema and rows
  read in one snapshot-isolation transaction, the schema scripted from the
  catalog views, working databases as throwaway instances in Microsoft's SQL
  Server image, and physical (data and log files) and logical (schema and
  rows) images. Working databases need `source.server.accept_eula: true` and
  Datadouble running as uid and gid 10001.
- **`source.server`:** settings for an engine's working servers, checked by
  its adapter.
- **`datadouble-oracle`**, the Oracle engine adapter: one or more schemas
  scripted with `DBMS_METADATA` and read in one read-only transaction,
  working databases as pluggable databases in throwaway Oracle Database Free
  containers, and physical (an unplugged pluggable database) and logical
  (schema and rows) images. Datadouble runs as 54321:54321 for Oracle.
- **Engine users:** Datadouble runs as the `uid:gid` an engine's copies are
  served as (999:999 for the official PostgreSQL, MySQL and MongoDB images,
  10001:10001 for SQL Server, 54321:54321 for Oracle), so physical images belong
  to their server on every replicate adapter; builds, `fetch` and `replicate`
  refuse to run as anyone else and say which user to run as.
- **Jailer subsets:** a spec's `subset.jailer` names a Jailer extraction model
  and data model, read and translated on every run; `datadouble import-jailer`
  writes the translation as native settings. On Sakila, Jailer and Datadouble
  choose the same rows.
- **Stores:** a filesystem store in the core package, and **`datadouble-s3`**
  for S3 and S3-compatible services.
- **`datadouble-conformance`**, the suite every engine adapter must pass.
- **Image signing** with cosign in the `store` stage, by key reference or
  keyless, and signature checks in `promote` and `fetch`.
- **`datadouble promote`**: copies verified, signed images built with the
  required strict-mode level into the export stores and advances a channel,
  refusing any image that fails a check.
- **`datadouble fetch`** and **`datadouble activate`**: staging in
  non-production, skipping generations already staged, keeping the newest
  for rollback.
- **`datadouble replicate`**, **`claim`**, **`release`** and **`copies`**:
  pool copies recycled one for one so the pool never empties, and on-demand
  copies that belong to an owner and expire. Replicate adapters: `filesystem`
  (core), **`datadouble-docker`** (`docker-volume`: volumes, or instances
  running the engine's server with a generated password),
  **`datadouble-btrfs`** (`btrfs`) and **`datadouble-kubernetes`** (`k8s-pv`).
- **`datadouble announce`**: `dotenv` and `ci-outputs` (core), `k8s-secret`
  (`datadouble-kubernetes`) and **`datadouble-vault`** (`vault`).
- **`datadouble prune`** for the build's stores, the export stores and
  staging, never removing a channel's target, with `--dry-run`.

### Changed

- CI pulls Docker Hub images through Google's mirror, `mirror.gcr.io`, so
  runs no longer fail on Docker Hub's anonymous rate limit (`429 Too Many
  Requests`). Jobs still pull from Docker Hub when the mirror can't serve an
  image (decision 138,
  [#106](https://github.com/datadouble/datadouble/issues/106)).
- Zensical, which builds the documentation site, is required as `>=0.1.1`
  instead of an exact version; `uv.lock` holds the exact one (decision 136).
- **The filesystem store writes files `0640` instead of `0600`, set by a
  new `mode` setting**
  ([#102](https://github.com/datadouble/datadouble/issues/102), decision 134).
  `mode` is an octal string such as `"0644"`; every file the store writes
  gets it whatever the umask, and every directory it creates gets it plus
  the search bit wherever it has the read bit (`0640` gives `0750`).
  Directories that already exist keep their mode. A web server in front of
  an export store (the `http` store) can now read what `promote` wrote when
  it runs in the group of `promote`'s user, or as anyone with `"0644"`.
  Modes that let anyone but the owner write, or leave the owner unable to
  read and write, are refused, naming `mode`. The channel files `fetch`
  writes into staging stay `0600`; the rest of staging follows the umask,
  as before.
- **BREAKING: `k8s-secret` writes Secrets into Datadouble's own namespace
  unless `namespace` is set**
  ([#103](https://github.com/datadouble/datadouble/issues/103), decision
  135). The old default of `default` is gone: left out, `namespace` resolves
  as the `kubernetes` runtime's does, with the adapter's own `kubeconfig` and
  `context`: in a cluster, the namespace of Datadouble's own pod; outside
  one, the kubeconfig context's namespace, or `default`. `announce` reports
  and `release` deletes Secrets in that namespace, so one spec serves both
  sides, and the Secrets go where the Helm chart's Role and Jenkins
  `instructions` already allow them. The copy registry now records, per
  `announce` entry, where each copy was announced (including what an
  adapter wrote before failing part way), and `release` withdraws each
  Secret from the namespace it was written to, not the one the releasing
  process resolves: every process using the spec must reach the same
  cluster, and the releasing identity needs `delete` on Secrets in each of
  those namespaces. Withdrawing now tries every target of every announce
  entry, even when an entry cannot be made or fails with an `OSError`, then
  fails once naming each announcement left behind, including any recorded
  by an entry no longer in the spec, to remove by hand. An announcement
  that succeeds but cannot be recorded fails, naming its targets.
  A kubeconfig context `kubectl` cannot read now fails, for the runtime too,
  instead of quietly resolving to `default`; an empty `KUBECONFIG` counts as
  unset. Secrets announced into `default` under the old default were not
  recorded, so later releases look for them in the newly resolved namespace
  and leave them behind: keep `namespace: default` in the spec until those
  copies are released, or delete the `datadouble-<copy id>` Secrets in
  `default` by hand. Announce adapters' `withdraw` now takes the recorded
  targets as a second argument.
- **BREAKING: the `kubernetes` runtime's `namespace` is optional, and no
  longer defaults to `default`** (decision 133). Left out, engine pods start
  where `kubectl` would put them: in a cluster, the namespace of
  Datadouble's own pod, read from its service account; outside one, the
  kubeconfig context's namespace, or `default`. One spec now serves Jenkins
  Kubernetes agents and Argo CD jobs on both sides, each in its own
  namespace. A spec that names a namespace is refused by `ci init` for a
  side whose `--runner` differs, for jobs that start engines. `instructions`
  give a Jenkins Kubernetes cloud the Role the Kubernetes plugin documents:
  pods (with `patch` and `update`), `pods/exec`, `pods/log` and watching
  events.
- **BREAKING: a spec's `source` is now `sources`, a map of named sources**
  ([#77](https://github.com/datadouble/datadouble/issues/77)). Write
  `sources: {app: {engine: ..., connection: ...}}` instead of
  `source: {engine: ..., connection: ...}`, even for one server; the schema
  refuses `source`, saying so. Settings move with the source
  (`sources.<name>.image`, `.server`, `.omit`, `.connection`), and messages
  name them that way. Each source's extract is in its own directory
  (`extract/data/<name>`), and the image manifest's `source_server_version`,
  `snapshot`, `snapshots` and `snapshot_seconds` moved into `sources`, by
  source. Engine adapters' `WorkingDatabases.load` takes a tuple of extract
  directories, one per source, and source sessions report the databases they
  read (`namespaces()`). `RequirementError.problems` pairs each problem with
  the source that uses the requirement.
- **BREAKING: PostgreSQL tables are named `database.schema.table`, and spec
  names match from the right**
  ([#76](https://github.com/datadouble/datadouble/issues/76)). A spec may
  still write `public.users` or `users` wherever only one table fits; a name
  that fits several, even only by case, now fails the plan naming each
  instead of taking the first. Globs match part by part, so `*` no longer
  crosses a dot: `shop.*.*` is every table in PostgreSQL's database `shop`.
  Every catalog lock records a table's namespaces as a list now, so
  re-run `datadouble lock` for every spec; PostgreSQL locks also gain the
  database's name, and plans, manifests and messages show three-part names.

- **BREAKING: `strict` is a mapping of levels per kind**
  ([#78](https://github.com/datadouble/datadouble/issues/78)). Today's
  levels move under `data` (`strict: {data: tables}`), beside `functions`,
  `views`, `triggers`, `types`, `sequences` and `events` (`off`, `warn` or
  `fail`, default `warn`); kinds left out take their defaults, and a bare
  level is refused. On the command line `--strict KIND=LEVEL`, repeatable,
  replaces `--strict LEVEL` and `--no-strict`; JSON requests take
  `"strict": {"data": "tables"}`. `promote.strict` takes the same mapping,
  and image summaries record the levels per kind. A known column whose type,
  kind or nullability changed now fails only at `data: columns` and warns at
  `tables` and `off`, unless the change breaks its anonymization rule, which
  fails at every level, saying so. Removed tables and columns and changed
  keys still always fail.
- Oracle builds run every stage on one Oracle container, plugging each
  stage's database in and out, instead of starting a container per stage;
  each start takes about 35 seconds before any work.
- Engine adapters' working databases have a `close()`, which a run calls
  when its builds end, even after a failure. An adapter that keeps nothing
  running between calls implements it as doing nothing.
- On-demand `replicate` makes copies up to the spec's `size` for an owner,
  rather than `size` more each time, so repeating a request makes none.

### Fixed

- **Error messages hide only data values, and keep the objects they name**
  ([#107](https://github.com/datadouble/datadouble/issues/107)). Engine errors
  hid every quoted string, so `null value in column "…" of relation "…"`
  didn't say which column. Each engine adapter now lists the message shapes
  its engine uses for data values (`Key (email)=(…) already exists`,
  `Duplicate entry '…' for key 'users.email'`, `The duplicate key value is
  (…)`, ORA-01722's string) and hides only those, keeping quoted table,
  column, constraint, role and file names; a quoted string it cannot classify
  is still hidden, and so is every quoted string in a message a trigger or
  procedure raised. A MySQL connection URL whose password holds a line break
  is now refused, since `mysqldump`'s option file can't carry it. When an
  engine tool exits with status 125, 126 or 127, the output comes from `docker
  run` or `podman run` failing before the tool started, or from a command that
  could not run or was not found, and is shown whole, so the message names the
  image that could not be pulled (`Unable to find image "postgres:18.7-trixie"
  locally`).
- Argo CD: a failed follower (`promote` after `build`, a pool's `provision`
  after `fetch`) made the Job run its pod again, so the leader ran a second
  time; a build whose promote was refused built, and notified, twice. A
  follower's failure now fails the Job at once (`podFailurePolicy`), and
  its own CronJob runs it again
  ([#69](https://github.com/datadouble/datadouble/issues/69)).
- **The Helm chart no longer changes the work claim's ownership on every
  start** ([#72](https://github.com/datadouble/datadouble/issues/72)). Its
  pods' `fsGroup` now applies only when the claim's root has another group
  (`fsGroupChangePolicy: OnRootMismatch`), so a terabyte-scale work
  directory is not walked each time a job starts.
- **BREAKING: a kept table that no seed or followed reference selects now
  fails the plan** ([#65](https://github.com/datadouble/datadouble/issues/65)).
  Until now such a table was copied without rows and nothing said so: a spec
  with no seeds built empty images that passed verification. The plan names
  each table and how to resolve it. Seed keys now take globs, as excludes do,
  so `seeds: {"*": "TRUE"}` copies every row of every table in SQL engines;
  `subset.unselected: all` does the same in any engine, and
  `subset.unselected: none` keeps the old behaviour with a warning per table.
  Specs whose subsets leave tables unreached on purpose need
  `unselected: none`. Jailer subsets keep Jailer's own behaviour, as if they
  said `unselected: none`, and `import-jailer` writes that setting.
- **BREAKING: images are filed by the engine's compatibility line**
  ([#74](https://github.com/datadouble/datadouble/issues/74)). The image
  identity, store path and copies' `engine-version` label carried the version's
  first number, so MySQL 8.0 and 8.4 images shared the line `8` though 8.0
  cannot open what 8.4 wrote. MySQL, MongoDB, Redis, Elasticsearch and
  OpenSearch lines now have two numbers (`mysql-8.4`); other engines' lines are
  unchanged. Images in existing stores keep their old line until pruned, and
  newer lines sort after them. `fetch.engine_version` names a line: change an
  existing `"8"` to `"8.4"`, or fetch keeps serving the old line. The image
  summary now records the exact server version, and `k8s-pv` copies carry it
  as `datadouble.dev/server-version`. Images signed before this change no
  longer verify, since their summaries lack the field: rebuild them.
- Redis working servers and copies did not load the image's bundled
  modules, so a source with JSON, search, time series or Bloom keys could
  not load. They now load every module in the image's module directory, and
  a key of a module's type gets a column strict mode asks about.
- OpenSearch's query insights plugin writes a `top_queries-*` index each
  day, which the catalog counted as a consumer's index, so strict mode and
  the point-in-time check failed builds that crossed one being made. The
  catalog now leaves out indices whose mapping marks them as the plugin's.

- A catalog lock Datadouble could not write ended with a generic file error;
  the error now names the lock file.

- The Datadouble image could not load the S3 store adapter, which imported a
  typing-only package at run time. Every command that loads the installed
  adapters failed in the image.
- A work directory, filesystem store or staging directory Datadouble could
  not write ended the command with a Python traceback. Each is now an error
  response (`run_failed`, `store_failed`, `staging_failed`), and any other
  file that fails is `file_failed`, without the file's name.
