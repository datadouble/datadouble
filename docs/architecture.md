# Architecture

## Pipeline

```
plan → extract → load → transform → package → verify → store → promote → fetch → replicate → announce
```

plus three operations on existing images and copies: `release`, `prune` and
`activate`.

| Stage | Input | Output |
|---|---|---|
| plan | Subset spec (native or Jailer `.jm`), live catalog, catalog lock | Execution plan: per-table extraction queries, resolved excludes, strict-mode result, size estimate |
| extract | Plan | Raw extract artifacts, each source's read from one consistent snapshot of its own |
| load | Extract artifacts | One working database per image group (scratch space, never shared storage); `before_load` hooks run first |
| transform | Working database | Anonymized working database; `after_transform` hooks run last |
| package | Anonymized working databases | One golden image per group and engine, in each requested format, plus manifests |
| verify | Golden image, restored to scratch space | Verification report; a failing report stops the pipeline |
| store | Verified golden image | Image and its image summary written to one or more storage backends |
| promote | Stored golden image | The image copied to the export store and a channel advanced; refused unless verification passed |
| fetch | Image in the export store | The image unpacked into non-production staging and a staging channel advanced; skipped if nothing newer |
| replicate | Staged image | Copies, as volumes or running instances, in pool or on-demand mode |
| announce | Copy descriptions | Consumers told where each clone can be accessed: a volume, or connection parameters |

| Operation | Effect |
|---|---|
| release | Destroys copies by ID, owner or expiry; recycles pool copies of old generations |
| prune | Applies retention to images in stores and staging; never removes a channel's target |
| activate | Points a channel at a chosen generation (rollback) |

Not every run uses every stage. Stages group into jobs (build, promote, fetch,
provision, release, prune, activate) that run in different places: everything up
to `store` runs inside production, and only a verified golden image leaves it.
See [CI/CD integration](ci-cd.md).

## Golden images

### Formats

A golden image comes in one or both of two formats:

- **physical:** the engine's own on-disk form, fastest to clone and start, but
  tied to the server version
- **logical:** a portable export, restorable into other server versions

```yaml
package:
  formats: [physical, logical]   # at least one
```

The CLI can override it for one run (`--format logical`), which sets the same
field in the JSON request. The field is part of the build's input hash and is
recorded in the manifest. When the spec leaves it out, each engine produces
every format it supports. Listing a format an engine can't produce fails the
plan and names the formats that engine supports; there is no silent fallback.

| Engine | physical | logical |
|---|---|---|
| PostgreSQL, MySQL | Data directory | Native dump |
| SQL Server | Detached data and log files | Scripted schema and bulk-load rows |
| Oracle | Unplugged pluggable database | `DBMS_METADATA` schema and bulk-load rows |
| MongoDB | WiredTiger data directory | `mongodump` |
| Redis | RDB file | Not offered |
| Elasticsearch, OpenSearch | Snapshot repository | NDJSON export |
| Salesforce | Not offered | Records and metadata |
| Filesystem | The file tree | Not offered |

### Accounts in physical images

A physical image carries only the accounts the build leaves, and the build
removes its own before packaging (decision 74). A consumer that serves a volume
copy with the engine's official image can't set a password through the image's
environment variables, since those apply only when a data directory is first
made, so nothing could log in over TCP. `package.accounts` adds accounts for
that case:

```yaml
package:
  accounts:
    - user: app                                                   # privileges: all
      password: app-password
    - user: reporting
      password:
        env: REPORTING_PW
      privileges: read
```

- **Physical images carry them, and so every copy of one.** Volume copies
  and instance copies (a server the replicate adapter runs on a volume copy,
  decision 62) both have the accounts. Logical images never do. A build that
  makes no physical image refuses the setting, as does an engine adapter that
  can't create accounts.
- **Instance copies reset one user.** An instance copy's server sets its
  generated password on the engine's user for instances when it starts:
  `datadouble` on MySQL, which Datadouble therefore reserves, and `postgres`
  on PostgreSQL. A declared `postgres` account keeps its password on volume
  copies only; on an instance copy it has the copy's generated password.
- **Passwords may be plain text,** since copies hold no personal data; a secret
  reference works too. `run` resolves every reference when it starts, before
  it reads the source (`plan` alone does not), and the image summary
  (`accounts`) and build manifest name the users and privileges, never the
  passwords.
- **`all`** gives every privilege with the grant option (a PostgreSQL
  superuser). An `all` account on a copy someone can reach can run commands in
  the consumer's container, for example PostgreSQL's `COPY ... TO PROGRAM`.
- **`read`** gets `SELECT` on the data in the image: MySQL `SELECT, SHOW VIEW`
  on each of the image's databases; PostgreSQL `CONNECT` on each database and,
  in every schema but the system ones, `USAGE` and `SELECT` on every table,
  view and sequence. Nothing reaches other accounts' password hashes. It is
  not strictly read-only: on PostgreSQL it also has what the image grants
  `PUBLIC`, which Datadouble leaves as the engine made it, including temporary
  tables, running functions and, before version 15, creating objects in the `public` schema; on
  either engine it can change its own password.
- **Names an engine can't use fail the plan:** too long, reserved by the
  engine, Datadouble's own users, and names that must keep `all` (PostgreSQL's
  `postgres`, its bootstrap superuser). Each adapter declares these in
  `Capabilities.accounts`, and duplicate names fail too.
- **Local logins need no password on PostgreSQL.** The image's `pg_hba.conf`
  keeps the `trust` lines `initdb` wrote for the Unix socket and the loopback
  address, ahead of the password rule the accounts add. Anything inside the
  consumer's container, a sidecar sharing its network, or a `kubectl
  port-forward` to it logs in as `postgres` without a password.
- The engine adapter writes the accounts into the image's own data directory
  after packaging it (the `Accounts` protocol), resetting the password and
  privileges of an existing user of the same name. It then logs in with each
  account over the network before the image is stored; MySQL also checks that
  a random wrong password is refused, and PostgreSQL checks each role in
  `pg_authid`, since its working clusters trust loopback logins. The package
  stage's inputs hold a keyed digest of each account (an HMAC under the run's
  anonymization key), so a retried or resumed build packages again when a
  password changes. MySQL and PostgreSQL support accounts so far; see their
  adapters' READMEs.

### Several images from one build

A spec can split its output into named groups, each becoming its own image:

```yaml
images:
  all:                                         # a combined image
    include: ["*"]
  billing:
    include: ["billing", "billing_audit"]
  content:
    include: ["cms.*"]
    requires: [billing]
```

With no `images:` section there is one group containing everything.

1. **One extract.** The plan, the consistent snapshot and the extract are
   shared by every group, so the source is read once however many images come
   out.
2. **One working database per group.** `load` builds each group's working
   database from the shared extract; `transform`, `package` and `verify` then
   run per group.
3. **Images that agree.** Anonymization is seeded once per run, so a value gets
   the same replacement in every group, and all images from one build share a
   generation. Consumers that need several groups together take the same
   generation of each, or a group that already contains them; images are never
   merged after the fact.
4. **References between groups are checked at plan time.** A reference from
   one group into another fails the plan unless the edge has a fallback (`null`
   or `stub`) or the group declares `requires:` the other, in which case
   `replicate` provisions the required images alongside it.
5. **Independent life cycles.** Each image has its own manifest, identity and
   channels, so it can be promoted, fetched, rolled back and pruned on its own.

### Images that work on their own

Subset planning decides which rows the extract reads. **Package planning**
runs after it, once per group, and works out what the group's image needs
to work: its **closure**. It starts from the group's tables, the objects the
spec and the group declare in `objects:` (code outside Datadouble expects
them), the objects that come with the group's tables (triggers on them,
views over them alone, triggers on those views) and, for a standalone group, the objects its
`include` patterns match, and follows, transitively and across schemas and
databases:

- references from a table up to its parents where the reference is
  followed, and down to its children where the spec follows them
  (`follow.down`); a fallback such as `on_missing: null` means the parent
  may be missing, so it is not followed;
- what a table's definition uses: the sequence or function in a column's
  default, a column's user-defined type, a function a check constraint
  calls, a table a row-level security policy reads (see
  [source objects](#source-objects));
- every object's dependency edges, exact and inferred.

```yaml
objects:                         # every image: what applications expect
  - function: public.uuid_generate_v4
images:
  billing:
    include: ["billing.*"]
    standalone: true             # pull in what billing needs from elsewhere
    objects:
      - view: reporting.daily
```

A **standalone** group pulls into its image everything its closure reaches
that the group does not include: objects with their definitions, and tables
with their rows. It cannot also have `requires:`, since its image works
alone. Other groups pull nothing in and build as they always have: their
references into other groups need `requires:` or a fallback, and so do the
objects they need that only another group's `include` patterns match
(patterns place objects as they place tables, matched from the right).
Objects that come with a group's own tables, such as a trigger on one, and
the objects it declares are its own whatever the patterns say. Since such a
group pulls nothing in, a declared object that needs a table neither it nor
a group it requires holds fails the plan, naming the table. Objects declared
at the top of the spec are declared for every group, so a top-level view
over one group's tables makes every other group that is not standalone
include those tables or require that group; declare such an object in the
group that holds its tables instead.

Which rows a pulled-in table brings is set per group with `rows:`, keyed by
a table name or glob, or by a reference written `child.column -> parent`,
which sets the rows of the table that reference reaches. For a table that
both kinds of key name, the reference key wins; two keys of the same kind
that disagree about a table fail the plan:

```yaml
images:
  billing:
    include: ["billing.*"]
    standalone: true
    rows:
      shop.customers: all                    # every row of the source table
      "invoices.approved_by -> staff": none  # the table this reaches: structure only
```

- **`referenced`** (the default for a table a reference reaches): the rows
  the image's rows reference, and the rows referencing them where the spec
  follows references down, transitively.
- **`all`**: every row of the source table, and the rows those reference in
  turn. Other groups' images are unaffected. When the group's own tables
  gain rows this way, their own references are followed as well, so every
  reference in the image keeps its parent. Tables the subset keeps without
  rows never gain any: a gained row whose `on_missing: drop` reference
  points at one is left out, as the shared extract leaves it out, and so
  are rows referencing a row left out so.
- **`none`**: the table's structure only. References into it are left out
  of the image, as references to another group are. A table reached only
  through objects, such as one a trigger's function writes to, defaults to
  `none`, with a warning, since no row of the image references it.

The rows come from the same snapshot as the shared extract: with several
sources, each table's rows and keys from its own source's snapshot, as for
references between sources. After the shared extract, each standalone group
that pulls tables in reads them again, keys first as the keys strategy does
(whichever strategy chose the shared rows), into an extract of its own per
source beside the shared ones (`groups/<group>/<source>`). Under
the nested strategy that first means one keys pass over the whole subset,
since nested selections cannot be read on their own: the cost nested
otherwise avoids, paid only when a standalone group pulls tables in. That
extract also holds any of the group's own tables that gained rows a
pulled-in table references, so every reference in the image has its parent.
`load` takes the tables listed there from it and the group's other tables
from the shared extract.

The plan lists, per group, its declared objects, every object its image
needs in dependency order, and for a standalone group each table (with its
rows) and object pulled in, with the path that reached it. Anything an image
cannot have fails the plan naming the object and that path: an object that
needs an excluded table (copies leave such an object out), or an object the
catalog does not list, such as one in a database the spec does not read.
When an inferred edge is on the path it warns instead, since inferred edges
never fail a plan on their own; a pulled-in routine whose edges were
inferred warns too, so a person checks it.

`load` builds a standalone image as exactly its closure: its own and
pulled-in tables, its objects created from their definitions in dependency
order, and none of the source's other objects. The PostgreSQL, MySQL and SQL
Server adapters build standalone images. Other adapters refuse a standalone
group by name when it pulls anything in or the source has objects: Oracle
for now, and the engines without objects (MongoDB, Redis, Elasticsearch,
OpenSearch, Salesforce), whose closures hold tables only.

Strict mode asks package planning what a difference from the lock breaks
(decision 130): a removed object that a packaged table or a declared object
still needs, and a new or changed object on a path to something an image
cannot have, fail the plan at every level.

Groups match on whatever namespace an engine has, like every name in a spec
([table names](subsetting.md#table-names)): databases and schemas for
relational engines and MongoDB, database numbers or key prefixes for Redis,
index patterns for Elasticsearch and OpenSearch, object sets for Salesforce, and
top-level directories for the filesystem adapter. A spec's `files` section is
built beside its sources and produces images of its own, one per group, all
sharing the generation.

### Several sources

A spec names its sources in `sources`, a map from a name to a source, even
when there is only one:

```yaml
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: APP_DSN
      databases: [orders, billing]
  warehouse:
    engine: postgres
    connection:
      dsn:
        env: WAREHOUSE_DSN
      databases: [warehouse]
images:
  everything:
    include: ["*"]
```

Some applications keep related databases on several servers of one engine; a
spec with a source per server copies them together, into one image and one
working server per group (decision 129).

1. **One engine, one working server.** Every source has the same engine, and
   gives the same `image` and `server` settings, or none. The plan fails when
   the sources run different compatibility lines (as much of the version as
   the engine says names a line, as an image's identity names it: `17` for
   PostgreSQL, `8.4` for MySQL; decision 120), and otherwise the working server runs the
   newest of their versions. Engines whose working server can hold several
   sources' extracts say so (the `several_sources` capability: PostgreSQL and
   MySQL); for the others, a second source fails the plan.
2. **Names do not collide.** Every source's databases go into the one working
   server under their own names, so a database (an outermost namespace: a
   table, for engines without one) that more than one source reads fails the
   plan, naming it, even one without tables: each session reports the
   databases it reads (`SourceSession.namespaces`). Specs then name tables
   exactly as they would with one source.
3. **Requirements agree.** One working server provides every source's
   requirements once: a requirement two sources use at different versions
   fails the plan, naming both, and a source may `omit` a requirement only if
   every source that uses it does. Each source's kept requirements are in the
   plan, and a missing one is reported against the source that uses it
   (`sources.<name>.image`).
4. **A snapshot per source, not one point in time.** No snapshot spans
   servers. Each source is read under its own, and the run opens them one
   after another before reading anything, so they are as close together as
   the sources allow. The image's manifest records each source's snapshot and
   when it was opened (`sources`), so nobody mistakes a multi-server image for
   one point in time.
5. **References between sources are listed.** A virtual reference whose child
   and parent are in different sources is followed like any other, but no
   single query reads two servers, so the plan uses the keys strategy (asking
   for `nested` fails) and lists each such reference in `between_sources`, as
   the image's manifest does too. A child read in one snapshot may name a
   parent the other snapshot does not hold yet: verification's reference
   check then fails, which stops the build, unless the reference has an
   `on_missing` fallback (`null`, `stub` or `drop`) to apply instead of
   following it. Engines that cannot join databases (PostgreSQL) refuse
   virtual references between them, and so between their sources. Foreign
   tables and other links between servers are not listed yet; links to other
   servers ([#80](https://github.com/datadouble/datadouble/issues/80)) will
   catalog them and copy their rows from the source they point at.
6. **Everything else is as for one source.** Images, groups, anonymization
   and verification see one catalog, the sources' catalogs merged, and the
   catalog lock records it. Anonymization is keyed once per run, so equal
   values get equal replacements in every source. Each source's credentials
   are its own secret references, and CI jobs get all of them.
7. **How the core drives it.** The core opens a session per source through the
   engine adapter's `connect`, plans against the merged catalog, and hands
   each source's snapshot a view of the plan holding only its tables
   (`Plan.of_source`), whose catalog fingerprint is that source's. Strategy B
   reads each table's keys through its own source's snapshot. Each source's
   extract goes to its own directory, and `WorkingDatabases.load` takes them
   all, in the spec's order: the adapter puts every source's databases in
   the one working server, and runs hooks in the first source's default
   database.

Name sources after what they hold (`app`, `warehouse`, `reporting`): the names
appear in the plan, the manifest and the extract's directories, and follow the
rules for spec names (lowercase letters, digits, `-` and `_`).

## Adapters

Each concern is an adapter type with a narrow interface. The core never contains
engine-specific logic; engine quirks stay inside the adapter.

| Adapter type | Responsibility | Examples |
|---|---|---|
| Engine (dump/load) | Introspect the catalog, open a consistent snapshot, extract, load, package | postgres, mysql, mssql, oracle, mongodb, redis, salesforce |
| Subset | Turn a subset spec into per-table extraction queries | FK-graph planner, Jailer `.jm` front end |
| Anonymize | Rewrite values column by column (see [anonymization](anonymization.md)) | faker, hash, null, format-preserving, expression (e.g. SQL) |
| Verify | Check a restored golden image | fk-integrity, row-counts, pii-scan |
| Storage | Read and write immutable artifacts; a store that cannot list keys is read through the index `promote` writes | local, s3, http (read-only, for `fetch`), gcs, artifact repository, nfs |
| Replicate | Turn a staged image into copies (volumes or instances), and destroy them | filesystem, docker-volume, btrfs, k8s-pv; later nfs-copy, csi-snapshot, zfs |
| Announce | Let consumers know where clones can be accessed, as volumes or connection parameters; never writes data | k8s-secret, ci-outputs, vault, dotenv (a `k8s-pv` copy announces itself) |
| CI/CD | Package, generate and bridge jobs for a CI/CD system; no pipeline logic | github-actions, gitlab, jenkins, argocd |

Adapters declare their capabilities (supported formats, tool versions, whether
they can share a snapshot across parallel workers) and the planner adapts to them.
Where a mature tool already exists, an adapter wraps it rather than reimplementing
it: native dump tools. Salesforce has none that fits a subset of one
consistent read, so its adapter calls the org's own APIs, and SFDMU remains a
candidate for loading copies back into an org (decision 111).

Adapters are discovered through standard plugin entry points, so third-party
adapters can be installed from anywhere.

## Artifacts, manifests and resumability

Every stage writes an artifact plus a manifest recording:

- Hashes of the stage's inputs and configuration
- Source server versions and tool versions
- Subset rules, resolved excludes, strict-mode level
- Anonymization strategy hashes
- Verification results
- Timings, row counts and sizes

Chaining is a single command:

```
datadouble run plan,extract,load,transform,package,verify,store --spec prod.yml
```

A run belongs to a generation, and keeps its stages' artifacts and manifests
under `<work>/<spec>/<generation>/`. Resuming a generation re-runs only the
stages whose input hashes changed and skips the rest, reusing their artifacts.
That gives resume-after-failure and cheap partial re-runs (for example, changing
only the anonymization strategy re-runs from `transform`):

```
datadouble run plan,extract,load,transform,package,verify,store --spec prod.yml --resume 0193a7c21b4e9f30
```

The generation also fixes the run's anchor time (`as_of`, when the spec gives
none) and its anonymization key, so a resumed run produces the same values.
The work directory holds real personal data from `extract` on: it is production
scratch space, like the working databases.

Working space and storage are separate concerns. Live database files are only
ever written to scratch space suited to them (local disk or strongly consistent
volumes). Working databases are throwaway servers the engine adapter starts there,
one per image group. An adapter may keep one server for a whole run and serve
each stage's database on it in turn, as Oracle does, whose start takes most of
a minute; the run closes its working databases when its builds end, even after
a failure (decision 110). Storage adapters only handle finished, immutable
artifacts.

### Engine software runs in the vendor's images

Datadouble installs no engine software (decision 72). Working databases,
restores for verification, and tools such as `pg_dump` or `mongorestore` run
in containers from the engine vendor's official image: by default the image of
exactly the source's version, so a PostgreSQL 16.4 source is built with
`postgres:16.4-trixie`: a dump from a newer minor release can hold what an
older server rejects. A source running a release whose image is not published
yet fails to pull, and names an image in `sources.<name>.image`.
`sources.<name>.image` names another, for example one with the extensions the
source uses, or a copy in a private registry. A spec with several sources
gives every source the same `image` and `server`, since one working server
holds them all; by default it runs the newest of their versions.
`sources.<name>.server` holds settings for the working servers, which the engine
adapter checks; SQL Server's needs `accept_eula: true`, the operator's
acceptance of Microsoft's licence, and takes an `edition` (decision 83).
PostgreSQL's takes `shared_preload_libraries`, for extensions that must load
at server start.

Errors from engines name the objects involved but never data values (decisions
54 and 139). Each engine adapter lists the message shapes its engine uses to
report data values, such as PostgreSQL's `Key (email)=(…) already exists` or
MySQL's `Duplicate entry '…' for key`, and those values show as `…`. A quoted
string after a word that names an object (`table "orders"`, `constraint
'pk_orders'`) is kept; any other quoted string is hidden, so a message no
adapter has classified stays safe. A message a user raised (a trigger's `RAISE
EXCEPTION`, MySQL's `SIGNAL`, SQL Server's `THROW`, Oracle's
`RAISE_APPLICATION_ERROR`) can put any value anywhere, so every quoted string
in it is hidden. MySQL lets a trigger raise any error number, so there a
message keeps names only when it is the server's own text for its number. When
a tool exits with status 125, 126 or 127 its output is shown whole: `docker
run` and `podman run` exit with 125 when they fail before the tool starts, and
126 or 127 mean the command could not be run or was not found. That output
comes from the container runtime and holds no data, image names included:

```text
pg_dump exited with status 125: Unable to find image "postgres:18.7-trixie" locally; docker: Error response from daemon: ...
```

### Source requirements

A source often depends on more than its server: PostgreSQL extensions, MySQL
plugins, Redis modules, search plugins, Salesforce managed packages. A
working image that lacks one cannot load the data (decision 107).

- **The engine adapter lists them.** Introspection reports each as a
  `Requirement` in the catalog: a kind (`extension`, `plugin`, `module`,
  `package`, `option`), a name and the version the source runs.
- **The lock records them.** Strict mode treats a new requirement like a new
  table, and a removed one, or one at another version, like a changed table
  (see [strict mode](excludes-and-strict-mode.md#levels)).
- **The build checks the image first.** Before it opens the snapshot, the
  build asks the working image what it provides (the adapter's
  `available()`) and fails with `requirement_unmet`, listing every
  requirement it lacks or lacks at the source's version. The fix is an image
  that has them, in `sources.<name>.image`. An adapter whose working copies need no
  add-ons to hold the data answers None, and the build skips the check:
  Salesforce's copies are local databases, which keep a managed package's
  objects and fields as ordinary tables and columns (decision 111).
- **The working database runs the source's versions.** PostgreSQL creates
  each extension at the source's version, not the image's default.
- **Leaving one out is explicit.** `sources.<name>.omit` names requirements the
  copies go without. Anything that uses an omitted one must be excluded too,
  or the build fails; nothing is dropped silently. Naming a requirement the
  source does not use fails the plan.
- **The build record lists them.** Each image's manifest holds the
  requirements its copies need and the ones it omitted.

Each adapter's README lists what it reports and the images that provide
common add-ons.

### Source objects

Functions, procedures, views, triggers, types, sequences and events can
change what a copy holds or how it behaves, so the catalog lists them too
(decision 130).

- **The engine adapter lists them.** Each is a `SourceObject` in the
  catalog: its kind, its namespaces and name like a table's
  (`shop.public.total` in PostgreSQL), with the argument types of a routine
  that can be overloaded, a fingerprint of its definition, and
  dependency edges to the tables and objects it needs.
- **Tables list what they need** among the objects: the sequences and
  functions their column defaults and check constraints use, and their
  columns' user-defined types, as far as each engine records them (each
  adapter's README says which). Package planning follows these edges.
- **Edges are exact where the engine records them** (PostgreSQL's
  `pg_depend`, SQL Server's `sys.sql_expression_dependencies`, Oracle's
  `ALL_DEPENDENCIES`, MySQL's view usage tables). Where it does not, the
  adapter parses the definition for names (best effort, with the core's
  `datadouble.objects` helper) and marks those edges `inferred`, so a
  person knows which to check. The plan's catalog keeps the marks, and only
  exact edges can fail a plan.
- **The lock records them**, and strict mode compares them per kind (see
  [strict mode](excludes-and-strict-mode.md#objects)). A catalog without
  objects hashes as before, so locks of engines that have none stay valid.
- **Engines without such objects report none:** MongoDB, Redis,
  Elasticsearch, OpenSearch, Salesforce and file trees.

Datadouble runs as the `uid:gid` the engine's copies are served as (decision
86): 999:999 for the official PostgreSQL, MySQL and MongoDB images,
10001:10001 for SQL Server and 54321:54321 for Oracle. A physical image's
files then belong to the server that will open them, on every replicate adapter,
and the engine's containers and Datadouble can read what the other writes.
Datadouble's image runs under any uid (`docker run --user 999:999`, or a
pod's `runAsUser` and `runAsGroup`); building, fetching and replicating as
anyone else fails with the user to run as.

SQL Server is tagged by release and cumulative update rather than build
(`mcr.microsoft.com/mssql/server:2022-CU28-ubuntu-22.04`). Microsoft ships no
schema tool in an image, so the adapter scripts the schema from the catalog
views (decision 85).

A **container runtime** adapter starts those containers, chosen by `runtime:`
in the spec (decision 73):

```yaml
runtime:
  docker: {}                     # any Docker-compatible daemon, through the docker command
```

```yaml
runtime:
  kubernetes:                    # pods, through kubectl
    claim: datadouble-work       # the PersistentVolumeClaim holding the work directory
    mount: /work                 # where that claim is mounted in Datadouble's own pod
    kubeconfig:                          # optional: another cluster, by secret reference
      env: BUILD_KUBECONFIG
    context: build               # optional: the kubeconfig's context
    namespace: datadouble        # optional
```

Without `namespace`, pods start where `kubectl` would put them unasked:
inside a cluster (no `kubeconfig` in the settings, no `KUBECONFIG`, and the
pod's service account namespace file), the namespace of Datadouble's own pod;
otherwise the kubeconfig context's namespace, or `default`. So one spec
serves jobs that run as pods in each side's own namespace (decision 133).

- **Shared paths.** Every container sees the paths it works on at the same
  absolute path as Datadouble. With Docker they are bind-mounted, so the work
  directory must exist at the same path on the daemon's host; on Kubernetes,
  Datadouble runs as a pod with the work directory on a claim, and every
  engine pod mounts the same claim at the same path.
- **One user.** Containers run as Datadouble's own user and group, so either
  side can read and remove the other's files.
- **Which cluster.** The `kubernetes` runtime, like the `k8s-pv` and
  `k8s-secret` adapters, uses the in-cluster service account or the ambient
  configuration, unless its `kubeconfig` setting, a secret reference to a
  kubeconfig's content, names another cluster. Datadouble writes it to a
  private temporary file for each `kubectl` call and removes it afterwards
  (decision 124).
- **Credentials.** A working server accepts a password generated for that start,
  passed through the runtime's secret channel (the docker command's
  environment, or a Secret object) and never a command line. Tools that need
  the server run inside its container. Credentials made for the build are
  removed before an image is packaged (decision 74).
- **Leftovers.** Containers are labelled with the stage directory they serve;
  before a stage runs again the core removes what an interrupted attempt left
  behind (decision 75), and a resumed run removes any left under its
  generation when it ends, since a killed attempt's last container can start
  after its stage was cleared.

## Interfaces

**JSON is primary.** Every operation is a JSON request with a JSON response:

```json
{"command": "run", "stages": ["plan", "extract"], "spec": "prod.yml", "strict": {"data": "columns"}}
```

**The CLI is a thin layer** that builds those requests:

```
datadouble run plan,extract --spec prod.yml --strict data=columns
datadouble run plan,extract --spec prod.yml --dry-run --json   # print the request only
datadouble plan --spec prod.yml --estimate --json               # machine-readable plan
```

Because the CLI only builds requests, the two interfaces cannot drift apart.

## Repository layout

One repository, with each adapter published as its own package so users install
only the drivers they need:

```
datadouble/
  core/            # planner, manifest, JSON API, CLI → package "datadouble"
  adapters/
    postgres/      # → "datadouble-postgres", own dependencies and version
    mysql/
    mssql/
    oracle/
    mongodb/
    redis/
    salesforce/
  ci/
    github-actions/  # container actions and reusable workflows
    gitlab/          # CI/CD Catalog components
    jenkins/         # shared library
    kubernetes/      # container image, Job templates, Helm chart (Argo CD)
  .claude-plugin/  # Claude Code marketplace and plugin manifests
  skills/          # agent skills, one directory per skill
  conformance/     # shared test suites (engine, CI/CD, agent skills)
  dev/             # container profiles and sample data per engine and CI system
  docs/
```

`pip install datadouble[postgres,mongodb]` is a convenience over installing the
adapter packages directly. Adapters can move to their own repositories after the
adapter interface stabilizes, without users noticing.

## Typing

Python with strict static typing, enforced in CI by `mypy --strict`, `pyright`
in strict mode, and `ruff` with its annotation rules. The aim is that no value
is "stringly typed":

- **Parse at the boundary.** Specs, manifests and JSON requests are parsed once
  into typed, immutable models (Pydantic in strict mode), which also generate
  the published JSON Schemas. Raw dicts never travel past the parser.
- **Closed sets are enums.** Stage names, strict-mode levels, outcomes, copy
  kinds and replicate modes are `StrEnum`s, not string literals.
- **Identifiers have types.** Table names, column paths, generations and channel
  names are distinct types (`NewType` or small value classes), so one can't be
  passed where another is expected. A table name is its namespace path,
  outermost first, and its name: `(shop, public)` and `users` for PostgreSQL,
  `(shop,)` and `users` for MySQL, nothing and `Account` for Salesforce. Each
  engine's path has a fixed length, and the core matches spec names against it
  from the right without knowing what each part means.
- **Adapter interfaces are typed `Protocol`s,** and every package ships
  `py.typed`, so third-party adapters are checked against them too.
- `Any`, `cast` and `# type: ignore` need a comment explaining why, and CI
  counts them.

## Credentials

Configuration refers to secrets indirectly (environment variables, Vault paths,
files). Resolved secrets are never written to manifests, artifacts or logs.
