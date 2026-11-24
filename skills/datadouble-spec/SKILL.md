---
name: datadouble-spec
description: Write or change a Datadouble spec, the YAML that says what to copy from a source and how to make it safe - source, subset, excludes, anonymization, hooks, verification and image groups - and check it with datadouble plan. Use for a new spec, a strict-mode failure, re-locking the catalog after a schema change, a Jailer model to import, putting a connection's password, key or other credential into a spec (never pass the secret itself, not even to this skill), or questions about what a spec does.
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
- **Leave Kubernetes namespaces out for CI/CD jobs.** The `kubernetes`
  runtime's and `k8s-secret`'s `namespace` default to the one `kubectl`
  would use: in a cluster, the namespace of Datadouble's own pod; outside
  one (an empty `KUBECONFIG` counts as unset), the kubeconfig context's (the
  adapter's own `kubeconfig` and `context`), or `default` when the context
  names none. A context `kubectl` cannot read fails rather than falling back.
  Each side's jobs run in its own namespace, so a spec without one serves
  both. Name one when the person wants pods or Secrets somewhere else, or
  when people also run `claim` from their own machines and consumers must
  find every copy's Secret in one namespace: a laptop's copies otherwise go
  to its context's namespace. `release` deletes each Secret where it was
  written, provided every machine using the spec reaches the same cluster
  and the releasing identity may delete Secrets in each of those
  namespaces. `ci init` refuses a runtime namespace that is not the side's
  `--runner`.

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
- **A MySQL connection URL's password can't hold a line break** (`%0A`,
  `%0D`); the connection is refused naming the URL, never the value. Tell
  the person to choose another password; never ask to see it.
- **Several databases from one server** go in one spec with
  `sources.<name>.connection.databases`, names or globs, for MySQL and PostgreSQL
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

## Sources and several servers

- **Every spec names its sources.** `sources` is a map from a name to a
  source (`sources: {app: {engine: postgres, connection: ...}}`), even with
  one server; there is no `source` key. Name each source after what it holds
  (`app`, `warehouse`), in lowercase letters, digits, `-` and `_`; the names
  appear in plans and manifests, and in error messages as
  `sources.<name>....`.
- **Several servers of one engine** are several sources, copied into one
  image (PostgreSQL and MySQL; other engines read one source per spec). They
  must share an engine and a compatibility line (`17` for PostgreSQL,
  `8.4` for MySQL, where 8.0 is another line), give the same `image` and
  `server`, hold no database name in common (empty databases count), and
  agree on requirements: the same version everywhere, and an `omit` in every
  source that uses it or in none. The plan fails naming whichever is not so. Each source has its own credentials, as its own
  secret references.
- **Such an image is not one point in time.** Each server is read under its
  own snapshot; say so when the person asks about consistency. A virtual
  reference between sources is followed with the keys strategy and listed in
  the plan's `between_sources`. A child may name a parent the other
  server's snapshot does not hold yet, and then verification fails and stops
  the build, unless the reference has an `on_missing` fallback; explain that
  trade-off and let the person choose. PostgreSQL refuses such references,
  as it does between its databases. Foreign tables between servers are not
  handled yet.

## When strict mode fails

`strict` is a mapping with a level per kind; `data: columns` is the
default, and kinds left out keep their defaults. Never write a bare level
such as `strict: tables`: the schema refuses it.

A new table or column fails the plan with each object and the ways to
resolve it: anonymize it, exclude it, or accept it in the lock once someone
has checked it holds no personal data. Show the person those options and let
them choose (rule 4). `datadouble lock` records the current catalog after
they have.

A known column whose type or nullability changed fails at `data: columns`
and warns at `tables` and `off`. If the change means its anonymization rule
no longer fits (a `faker` column that is no longer text, a `null` rule on a
column that can no longer be null), it fails at every level, saying so:
change the rule, not the level. Removed tables and columns and changed keys
always fail; re-lock once the spec suits the new schema.

## Re-locking

`datadouble lock` reads the source, so it runs only inside the production
boundary. It lists what changed since the lock it replaces: tables, columns,
keys, requirements and objects (functions, views, triggers...) added,
removed or changed (`changes` in `--json`; `null` for a first lock, where
everything is new, and when the earlier lock cannot be read, which
`unreadable` explains).

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

## When functions, views or triggers change

The lock also lists the source's functions, procedures, views, triggers,
types, sequences and events, each with a fingerprint and what it depends
on. A new, changed or removed one warns by default (`strict.functions`,
`views`, `triggers`, `types`, `sequences`, `events`: `off`, `warn` or
`fail`). A warning is for the person to review: a changed trigger or
function can change what the copies hold. Tell them which objects changed;
once they have checked, `datadouble lock` records the new definitions.

A removed object that another one still needs fails at every level, naming
both, such as a function a trigger calls. So does a removed object a
packaged table needs (its default's sequence, a column's type) or `objects:`
declares, and a new or changed object that leads to an excluded table from
an image (a new trigger calling a function that reads one). Edges marked `inferred` were
parsed from a body rather than recorded by the engine; a removal only they
point at warns instead, saying what may still need it. In MySQL a view's
exact edges include the tables its stored functions read, so a failure can
name a view whose text never mentions the removed table; point the person
at the functions it calls. Fix the source or the excludes; never lower a
level to get past it (rule 4).

Old locks have no objects, so every object warns as new until the person
re-locks. Many objects suddenly removed or emptied usually means the
connecting user lost privileges, not that the source changed: SQL Server
needs `VIEW DEFINITION`, and Oracle shows bodies and objects only to their
owner or to users granted them (see the adapters' READMEs). Say so rather
than re-locking.

## Images that must work alone

`datadouble plan` works out, per image group, what the image needs to work:
its closure. It follows followed references up to parents (and down where
`follow.down` says), what tables need (a default's sequence, a column's
type), the triggers on the group's tables and views over them, and every
object's dependencies, across schemas and databases.

- **`standalone: true`** on a group pulls what its closure reaches into its
  image: objects, and tables with the rows the group's rows reference. The
  image then holds exactly its closure. Such a group cannot have
  `requires:`. Suggest it when a person wants one schema or database of a
  larger source as an image of its own. The plan lists what each group
  pulls in (its `pulled` list, each with the path that reached it).
  PostgreSQL, MySQL and SQL Server build standalone images; Oracle and the
  engines without objects refuse them by name.
- **`rows:`** on a standalone group overrides which rows a pulled-in table
  brings: `referenced` (the default), `all` (every source row) or `none`
  (structure only). Keys are a table name or glob, or a reference
  `child.column -> parent`, which wins. A table reached only through
  objects comes without rows and the plan warns: ask the person whether the
  image needs its rows before setting `rows` for it.
- **Groups place objects too.** `include` patterns match functions, views
  and other objects like tables. A group that is not standalone and needs an
  object only another group includes fails the plan; the fix is `requires:`
  for that group, or `standalone: true`. Triggers on a group's own tables,
  views over them and its declared objects are its own. A declared object
  that needs a table the group lacks fails too: include the table, or
  require the group holding it.
- **A standalone PostgreSQL build that fails dropping a function** names
  what uses it, such as an aggregate or operator, which the catalog does not
  list. Suggest declaring the function in the group's `objects`.
- **`objects:`**, at the top of the spec (every group) or in a group,
  declares what code outside Datadouble expects in the images, by kind and
  name. A top-level object needs its tables in every group that is not
  standalone, so put one over a single group's tables in that group:
  `{function: public.uuid_generate_v4}`, `{view: reporting.daily}`. Ask the
  person what their applications call that the tables do not show; add only
  what they name. A name that does not resolve, or fits several objects,
  fails naming them; add argument types (`"public.total(integer)"`) or
  qualify it.
- **A failure names the path**, such as `table billing.invoices -> trigger
  ... -> function audit.write -> table audit.log, which is excluded`. The
  fix is the person's: stop excluding what the image needs, change the
  source, or leave the group out of what needs it. Never add excludes or
  drop objects to make it pass (rule 4). A path through an `inferred` edge
  only warns: tell the person to check it.

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
- **Suggest an image first.** Propose a `sources.<name>.image` built on the source's
  server release that has the add-on, such as `postgis/postgis:18-3.6`. Some
  add-ons also need `sources.<name>.server` settings; for PostgreSQL,
  `shared_preload_libraries`.
- **Leaving one out is the person's decision.** `sources.<name>.omit` drops a
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

Who hears about a generated CI job that fails, warns or recovers is the spec's
`notify` section, not a `ci init` option: `ci init` is run again whenever
the spec, the version or the options change, and an option left off a later
run would silently drop notifications. Never edit generated files to add
them (rule 2).

```yaml
notify:
  when: [failure, warning, recovery]   # default: [failure]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
    - email:
        to: [data-team@example.com]
```

- **A webhook's URL is a secret reference** (rule 6): it usually carries a
  token. Datadouble posts to it after each job's command, on every system,
  a JSON body with `spec`, `job`, `outcome`, `recovered`, `generation`,
  `error` (the error code), `run_url` and `text`. It never holds data values or secrets.
  `text` is one readable line built from the other fields, such as
  `Datadouble shop: build failed (store_failed) - <run_url>`.
  Credentials in the URL need https://, and posts use the runner's proxy
  settings (`HTTPS_PROXY`, `NO_PROXY`).
- **A webhook to a chat room takes `format: chat`**, under `webhook:` next
  to `url:`. Datadouble then posts only `{"text": ...}`, which Google Chat,
  Slack, Mattermost and Rocket.Chat incoming webhooks accept with no
  service in between. Google Chat refuses any other key, so a Google Chat
  URL always needs `chat`. Leave the default, `format: json`, for a
  receiver written for Datadouble.
- **`recovery` is a success after a run that failed, warned or was
  stopped.** A webhook gets one post for it (`outcome: success`,
  `recovered: true`, `text` saying the job recovered) and nothing for
  later successes. Each job records its last outcome for this, in the work
  directory on the production side and in `fetch.staging` on the other,
  holding only the outcome. So with `recovery` and a webhook, `ci init`
  needs `--work` for every production job (even `promote` or `lock`
  alone) and the `fetch` section on the other side; every production job
  then mounts the work directory. The first run after adding `recovery`
  has no record and posts no recovery, and a run stopped before
  Datadouble started can't be recorded.
- **Email is the CI system's own:** Jenkins mails from the Jenkinsfile,
  with `fixed` for `recovery` (a success after a failed or unstable build,
  not an aborted one); GitLab and GitHub need the manual setup `ci init`
  lists in `instructions`, and GitHub can only mail whoever started a run
  and never mails a recovery; GitLab's Pipeline status emails never mail a
  recovery either, and its Fixed pipeline notification reaches only the
  pipeline's author; Argo CD refuses email, so suggest a webhook to a
  service that mails the team.
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
