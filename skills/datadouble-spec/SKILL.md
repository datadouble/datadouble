---
name: datadouble-spec
description: Write or change a Datadouble spec, the YAML that says what to copy from a source and how to make it safe - source, subset, excludes, anonymization, hooks and verification - and check it with datadouble plan. Use for a new spec, a strict-mode failure, re-locking the catalog after a schema change, a Jailer model to import, putting a connection's password, key or other credential into a spec (never pass the secret itself, not even to this skill), or questions about what a spec does.
---

# The Datadouble spec

Follow `../RULES.md` throughout.

## Start from what exists

- `datadouble schema` prints the JSON Schema every spec is checked against;
  read it rather than guessing field names.
- A Jailer extraction model translates with `datadouble import-jailer --spec
  <spec> --model <model.jm> --datamodel <dir> --output <file>`; it writes the
  native settings and the catalog lock, and lists what it could not carry
  over.
- Secrets are references: `{env: NAME}` or `{file: /path}`. Never write a
  value into the spec. That includes a Kubernetes adapter's `kubeconfig`
  (`k8s-pv`, `k8s-secret`, the `kubernetes` runtime), whose `{env: NAME}`
  holds the kubeconfig's content, never the runner's own kubeconfig file;
  jobs running in the cluster they drive need none. The one exception is a
  copy account's password, below.

## Check it, without production data

`datadouble plan --spec <spec>` reads only the catalog and checks the spec
against it and its lock (`catalog.lock` beside the spec). Planning reads the
source, so it runs only where the operator marked the machine inside the
production boundary. Outside it, generate the `build` job (with
`datadouble-build`) and let its first run plan on the production runner; the
person can share the plan's output, which names tables and columns but never
values. The first lock comes the same way, from the `lock` job (see
re-locking below).

## Table names and databases

- **Names match from the right.** A table's full name is its parts joined
  with dots (`shop.public.users` in PostgreSQL: database, schema, table;
  `shop.users` in MySQL). A spec may leave out leading parts (`users`,
  `public.users`) wherever only one table fits. When a plan fails with
  "is ambiguous: it matches ...", write enough of the name to pick one of
  the tables it lists; do not guess which one the person meant.
- **Globs stay within a part.** `audit.*` is every table in namespaces called
  `audit`; `shop.*.*` is every table in PostgreSQL's database `shop`.
- **Several databases from one server** go in one spec with
  `source.connection.databases`, names or globs, for MySQL and PostgreSQL
  (`["*"]` for all). The copies then hold every database. Read the adapter's
  README before choosing: MySQL reads them all under one snapshot, references
  between them included, and leaves out views over databases it does not
  read; PostgreSQL reads each under its own and refuses virtual references
  between them. Excluding every table of a database still leaves it, empty,
  in the copies; to drop it, leave it out of `databases`. Hooks and
  assertions run in the default database (the connection's own when it is
  read, otherwise the first in name order); in MySQL, qualify table names in
  them to reach the others, while PostgreSQL's cannot reach other databases
  at all.
- **A new database a glob picks up** shows up as new tables in strict mode,
  handled like any other new table below.

## When strict mode fails

`strict: columns` is the default. A new table or column fails the plan with
each object and the ways to resolve it: anonymize it, exclude it, or accept
it in the lock once someone has checked it holds no personal data. Show the
person those options and let them choose (rule 4). `datadouble lock` records
the current catalog after they have.

## Re-locking

`datadouble lock` reads the source, so it runs only inside the production
boundary. It lists what changed since the lock it replaces: tables, columns,
keys and requirements added, removed or changed (`changes` in `--json`;
`null` for a first lock, where everything is new, and when the earlier lock
cannot be read, which `unreadable` explains).

- **No shell inside the boundary?** Re-lock through the CI `lock` job.
  Generate it with the production jobs (`--job lock` in `datadouble-build`'s
  `ci init`), and have the person start it by hand. It publishes the new
  `catalog.lock` and `changes.txt` as the job's artifact (on Argo CD, in the
  job's log) and never commits.
- **The person reviews the diff, not you.** Each new or changed column in
  `changes.txt` must be checked for personal data before the lock is
  committed; one that holds any gets an anonymization rule or an exclude in
  the spec first. Committing the lock without that check accepts every new
  column as safe, which is what strict mode exists to prevent (rule 4).
  Offer to walk through the list with them, then let them commit it beside
  the spec.
- A lock holds names, types and versions, never values, so the person may
  share it or its changes with you.

## When tables have no rows selected

A kept table that no seed, `follow: all` or followed edge reaches fails the
plan by default (`subset.unselected: fail`). Each one is a choice about what
the copy holds, so show the person the options and let them pick:

- seed it, or seed many at once with a glob (`"*": "TRUE"` in SQL engines);
- follow a reference that reaches it;
- make it `structure_only`, or exclude it;
- `unselected: all` to copy every row of such tables (any engine), or
  `unselected: none` to copy them without rows, with a warning.

A Jailer subset already behaves as `none`; change the Jailer model instead.

## Extensions, plugins and modules

The lock lists the add-ons the source uses beyond its server (its
requirements: PostgreSQL extensions, MySQL plugins, Redis modules, search
plugins, Oracle index types), each at the source's version where it has one.
Strict mode fails the plan when one is new, removed or upgraded, and a build
fails with `requirement_unmet` when the working image lacks one.

- **Look before you ask.** The lock (`catalog.lock`) lists the requirements;
  the engine adapter's README lists images that provide common ones.
- **Suggest an image first.** Propose a `source.image` built on the source's
  server release that has the add-on, such as `postgis/postgis:18-3.6`. Some
  add-ons also need `source.server` settings; for PostgreSQL,
  `shared_preload_libraries`.
- **Leaving one out is the person's decision.** `source.omit` drops a
  requirement from the copies, and anything that uses it must be excluded
  too. Explain that, and add it only when the person asks (rule 4). Managed
  services' own extensions, such as `aws_s3` on Amazon RDS, are the usual
  case.
- **Columns of an add-on's type** (`geometry`, `vector`, `hstore`) have the
  kind `other`: `faker` does not apply, so suggest `null`, `expression` or
  keeping the column if it holds no personal data.

## Accounts for copies

`package.accounts` gives physical images accounts that log in over TCP, for
consumers that serve a volume copy with the engine's official image (whose
password variables apply only to a new data directory). Read the engine
adapter's README before adding one.

- **Physical images carry them, and so every copy of one,** volume and
  instance copies alike. Logical images never do. A build that makes only
  logical images refuses `package.accounts`, as does an engine whose adapter
  can't create accounts (MySQL and PostgreSQL can).
- **Passwords may be plain text,** since copies hold no personal data; this
  is the only password a spec may hold as text (rule 6). A team that prefers
  a reference uses `{env: NAME}` or `{file: /path}`, and `ci init` passes it
  to the build job. Every other credential stays a reference.
- **`privileges`:** `all` (the default) is every privilege with the grant
  option, a PostgreSQL superuser. `read` is `SELECT` on the image's data,
  without other accounts' password hashes, but not strictly read-only: it
  can change its own password, and on PostgreSQL it keeps what `PUBLIC` has
  (temporary tables, running functions, and before version 15 creating
  objects in `public`). Suggest `read` for reporting tools and anything that
  only reads, and say what it still allows.
- **Say what `all` means.** An `all` account on a copy others can reach can
  run commands in the consumer's container, for example PostgreSQL's
  `COPY ... TO PROGRAM`. Tell the person before adding one.
- **PostgreSQL images trust local logins.** Anything inside the consumer's
  container, a sidecar or a `kubectl port-forward` logs in as `postgres`
  without a password, accounts or not. An instance copy also resets
  `postgres`'s password to its generated one, so a declared `postgres`
  account's password holds on volume copies only.
- **Names the engine can't use fail the plan:** duplicates; MySQL names over
  32 characters, `datadouble` and `datadouble-build`; PostgreSQL names over
  63 bytes, starting with `pg_`, or reserved (`public`, `none`,
  `current_user`, `session_user`, `current_role`), and `postgres` with
  `privileges: read`. An existing user of the same name, such as `root` or
  `postgres`, gets the declared password.

## Notifications

Who hears about a generated CI job that fails or warns is the spec's
`notify` section, not a `ci init` option: `ci init` is run again whenever
the spec, the version or the options change, and an option left off a later
run would silently drop notifications. Never edit generated files to add
them (rule 2).

```yaml
notify:
  when: [failure, warning]          # default: [failure]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
    - email:
        to: [data-team@example.com]
```

- **A webhook's URL is a secret reference** (rule 6): it usually carries a
  token. Datadouble posts to it after each job's command, on every system,
  a JSON body with `spec`, `job`, `outcome`, `generation`, `error` (the
  error code) and `run_url`. It never holds data values or secrets.
  Credentials in the URL need https://, and posts use the runner's proxy
  settings (`HTTPS_PROXY`, `NO_PROXY`).
- **Email is the CI system's own:** Jenkins mails from the Jenkinsfile;
  GitLab and GitHub need the manual setup `ci init` lists in
  `instructions`, and GitHub can only mail whoever started a run; Argo CD
  refuses email, so suggest a webhook to a service that mails the team.
- A failed notification never changes a job's outcome, and a job that fails
  before Datadouble starts (an image that cannot be pulled) posts no
  webhook: the CI system's own failure notifications cover that.
- Each attempt posts, so a job the system runs again after a failure (a
  Kubernetes Job's `backoffLimit`) usually notifies twice. On Argo CD a
  failed follower (`promote` after `build`) fails the Job without running
  the pod again, so its leader is not built, or heard from, twice; tell the
  person to run the follower's own CronJob once its problem is fixed.
- After changing `notify`, run `ci init` again for both sides, so every job
  gets the webhook's variable, and tell the person to define it.
