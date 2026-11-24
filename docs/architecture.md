# Architecture

This page explains how Datadouble turns a production system into safe copies,
and how its code is put together to do that. It starts with the problem,
because the problem explains most of the design. Then it follows a run
through its stages, looks closely at what a build reads and what it makes,
and ends with the pieces that implement it all.

The page covers, in order:

1. [The problem](#the-problem): the four facts that shape the design.
2. [The pipeline](#pipeline): the stages a run goes through, and where each
   one runs.
3. [What a build reads](#sources): sources, their add-ons and their database
   objects, and specs that read several servers.
4. [What a build makes](#golden-images): golden images, their formats, how
   one build makes several images, and how an image can work on its own.
5. [How a run is carried out](#running): artifacts and resuming, working
   space, the containers engine software runs in, and how errors stay free
   of data.
6. [The pieces](#pieces): adapters, interfaces, the repository's layout,
   typing and credentials.

## The problem {#the-problem}

Datadouble copies part of a production system, makes it safe, and hands out
copies of the result. Four facts about that job shape everything on this
page:

- **Sources are large.** A source may hold terabytes. Datadouble cannot copy
  it all first and cut it down later, so it filters while it reads, and reads
  everything from one consistent point in time.
- **Real personal data must stay in production.** The raw data is only safe
  inside production. So the work splits in two: everything that touches real
  data runs inside production, and only a verified, anonymized result
  crosses out.
- **Teams run many engines.** The same ideas must work for PostgreSQL,
  MongoDB, Elasticsearch, Salesforce and a directory of files. So a small core
  holds the general logic, and adapters hold everything specific to one
  engine or one tool.
- **Builds are long, and they fail.** A build can take hours. So it runs as a
  series of stages, and each stage saves its output. A failed or changed run
  starts again from the first stage that needs to, not from the beginning.

## The pipeline {#pipeline}

A run moves data through a fixed series of **stages**. Each stage takes the
previous stage's output and writes its own. The full series is:

```
plan → extract → load → transform → package → verify → store → promote → fetch → replicate → announce
```

A few more **operations** act on images and copies that already exist:
`claim`, `release`, `prune` and `activate`.

### Terms used on this page {#terms}

A few words come up in every section below:

- A **spec** is the YAML file that describes a build: what to read, which
  rows to keep, how to anonymize them, and what images to make.
- A **source** is a system the spec reads, such as one PostgreSQL server.
- The **catalog** is what Datadouble learns about a source when it plans:
  its tables, columns and keys, and its other objects. The **catalog lock**
  (`catalog.lock`) is a reviewed copy of the catalog, committed next to the
  spec. Strict mode compares the live catalog against it.
- An **image group** is a named part of the output. Each group becomes its
  own golden image. A spec with no groups has one group holding everything.
- A **working database** is a throwaway server, started in production scratch
  space, where the extracted data is loaded and anonymized.
- A **golden image** is the anonymized, verified result for one group, ready
  to copy.
- A **generation** is the unique ID a build gets. Every image from one build
  shares it.
- A **channel** is a named pointer, such as `latest`, to one generation. It
  can be moved forward, or back to roll back.
- A **copy** is what consumers use: a volume, or a running server, made from
  a golden image.

### Stages {#stages}

This table lists what each stage takes in and what it produces:

| Stage | Input | Output |
|---|---|---|
| plan | Subset spec (native or Jailer `.jm`), live catalog, catalog lock | Plan: each table's rule and edges, resolved excludes, strict-mode report, the image groups and their closures |
| extract | Plan | Raw extract artifacts, each source's read from one consistent snapshot of its own |
| load | Extract artifacts | One working database per image group (scratch space, never shared storage); `before_load` hooks run first |
| transform | Working database | Anonymized working database; `after_transform` hooks run last |
| package | Anonymized working databases | One golden image per group and engine, in each requested format, plus manifests |
| verify | Golden image, restored to scratch space | Verification report; a failing report stops the pipeline |
| store | Verified golden image | Image and its image summary written to one or more storage backends |
| promote | Stored golden image | The image copied to the export store and a channel advanced; refused unless verification passed and the signature is trusted |
| fetch | Image in the export store | The image unpacked into non-production staging and a staging channel advanced; skipped if that generation is already staged |
| replicate | Staged image | Copies, as volumes or running instances, in pool or on-demand mode; in pool mode, fresh copies replace unclaimed copies of other generations |
| announce | Copy descriptions | Consumers told where each clone can be accessed: a volume, or connection parameters |

The operations work on what earlier runs left behind:

| Operation | Effect |
|---|---|
| claim | Takes an unclaimed copy from the pool for an owner |
| release | Destroys copies by ID, owner or expiry, and any copy that was never finished |
| prune | Applies retention to images in stores and staging; never removes a channel's target |
| activate | Points a channel at a chosen generation (rollback) |

### Jobs and the production boundary {#jobs}

Not every run uses every stage. Stages group into **jobs**: build, promote,
fetch, provision, release, prune, activate and lock (which re-locks the
catalog). The jobs run in different
places. Everything up to `store` runs inside production, and only a verified
golden image leaves it, through `promote`. [CI/CD integration](ci-cd.md)
describes each job and the boundary in detail.

The rest of this page looks inside the production half. The next section
starts where a build starts: with the sources it reads.

## What a build reads: sources {#sources}

A spec names its sources in `sources`, a map from a name to a source. It does
so even when there is only one:

```yaml
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: APP_DSN
      databases: [orders, billing]
```

Name sources after what they hold (`app`, `warehouse`, `reporting`). The
names appear in the plan, the manifest and the extract's directories. They
follow the rules for spec names: lowercase letters, digits, `-` and `_`.

When the plan stage reads a source, it learns more than the tables and
columns. A source also depends on add-ons, such as PostgreSQL extensions,
and holds code and other objects, such as functions and triggers. A copy
that lacks either may not load or may not behave like production. The next
two sections cover these, and the third covers specs that read more than one
server.

### Source requirements {#source-requirements}

A source often depends on more than its server: PostgreSQL extensions, MySQL
plugins, Redis modules, search plugins, Salesforce managed packages. A
working image that lacks one cannot load the data (decision 107). Datadouble
calls each such add-on a **requirement**, and handles them like this:

- **The engine adapter lists them.** Introspection reports each one as a
  `Requirement` in the catalog. A requirement has a kind (`extension`,
  `plugin`, `module`, `package` or `option`), a name, and the version the
  source runs.
- **The lock records them.** Strict mode treats a new requirement like a new
  table. It treats a removed requirement, or one at another version, like a
  removed table: the plan fails at every level (see
  [strict mode](excludes-and-strict-mode.md#levels)).
- **The build checks the image first.** Before it opens the snapshot, the
  build asks the working image what it provides (the adapter's
  `available()`). If anything is missing, the build fails with
  `requirement_unmet` and lists every requirement the image lacks, or lacks
  at the source's version. The fix is an image that has them, named in
  `sources.<name>.image`.
- **Some adapters skip the check.** An adapter whose working copies need no
  add-ons to hold the data answers `None` from `available()`, and the build
  skips the check. Salesforce is one: its copies are local databases, which
  keep a managed package's objects and fields as ordinary tables and columns
  (decision 111).
- **The working database runs the source's versions.** PostgreSQL creates
  each extension at the source's version, not the image's default.
- **Leaving one out is explicit.** `sources.<name>.omit` names requirements
  the copies go without. Anything that uses an omitted requirement must be
  excluded too, or the build fails. Nothing is dropped silently. Naming a
  requirement the source does not use fails the plan.
- **The build record lists them.** Each image's manifest holds the
  requirements its copies need and the ones it omitted.

Each adapter's README lists what it reports, and the images that provide
common add-ons.

### Source objects {#source-objects}

Tables are not the only thing in a database that matters to a copy.
Functions, procedures, views, triggers, types, sequences and events can
change what a copy holds or how it behaves. So the catalog lists them too
(decision 130). Datadouble calls them **source objects**:

- **The engine adapter lists them.** Each one is a `SourceObject` in the
  catalog, with:

    - its kind;
    - its namespaces and name, like a table's (`shop.public.total` in
      PostgreSQL), plus the argument types of a routine that can be
      overloaded;
    - a fingerprint of its definition;
    - dependency edges to the tables and objects it needs.

- **Tables list the objects they need.** These are the sequences and
  functions their column defaults and check constraints use, and their
  columns' user-defined types, as far as each engine records them. Each
  adapter's README says which. Package planning follows these edges (see
  [images that work on their own](#images-that-work-on-their-own)).
- **Edges are exact where the engine records them.** The engines that do
  are PostgreSQL (`pg_depend`), SQL Server
  (`sys.sql_expression_dependencies`), Oracle (`ALL_DEPENDENCIES`) and MySQL
  (its view usage tables). Where the engine does not record an edge, the
  adapter parses the definition for names, as a best effort, with the core's
  `datadouble.objects` helper. It marks those edges `inferred`, so a person
  knows which ones to check. The plan's catalog keeps the marks, and only
  exact edges can fail a plan.
- **The lock records them,** and strict mode compares them kind by kind (see
  [strict mode](excludes-and-strict-mode.md#objects)). A catalog with no
  objects leaves them out of its fingerprint altogether, as one with no
  requirements leaves those out.
- **Engines without such objects report none.** These are MongoDB, Redis,
  Elasticsearch, OpenSearch, Salesforce and file trees.

### Several sources {#several-sources}

Some applications keep related databases on several servers of one engine.
A spec can copy them together by giving each server its own source. They go
into one image per group, built on one working server per group (decision
129):

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

Putting several servers into one working server raises a few questions. The
rules below answer them in turn.

1. **One engine, one working server.** Every source has the same engine.
   Every source also gives the same `image` and `server` settings, or none
   (these choose the working server's container image and settings; see
   [engine software](#engine-software-runs-in-the-vendors-images)).

    - The sources must run the same **compatibility line**: the part of the
      version that the engine says names a line, as an image's identity names
      it. That is `17` for PostgreSQL and `8.4` for MySQL (decision 120). If
      the lines differ, the plan fails.
    - Otherwise the working server runs the newest of the sources' versions.
    - An engine whose working server can hold several sources' extracts says
      so with the `several_sources` capability. PostgreSQL and MySQL do. For
      other engines, a second source fails the plan.

2. **Names do not collide.** Every source's databases go into the one working
   server under their own names. So a database that more than one source
   reads fails the plan, naming it. Here "database" means an outermost
   namespace, or a table for engines that have none. This holds even for a
   database without tables: each session reports the databases it reads
   (`SourceSession.namespaces`). Because names never collide, specs name
   tables exactly as they would with one source.
3. **Requirements agree.** One working server provides every source's
   [requirements](#source-requirements) once.

    - A requirement that two sources use at different versions fails the
      plan, naming both.
    - A source may `omit` a requirement only if every source that uses it
      does.
    - Each source's kept requirements are in the plan. A missing one is
      reported against the source that uses it (`sources.<name>.image`).

4. **A snapshot per source, not one point in time.** No snapshot spans
   servers. Each source is read under its own snapshot. The run opens them one
   after another before it reads anything, so they are as close together as
   the sources allow. The image's manifest records each source's snapshot and
   when it was opened (`sources`), so nobody mistakes a multi-server image
   for one point in time.
5. **References between sources are listed.** A virtual reference whose child
   and parent are in different sources is followed like any other. But no
   single query reads two servers, so:

    - the plan uses the keys strategy for it (strategy B in
      [subsetting](subsetting.md#two-execution-strategies)), and asking for
      `nested` fails;
    - the plan lists each such reference in `between_sources`, and so does
      the image's manifest.

    The two snapshots are taken at slightly different times. A child read in
    one snapshot may name a parent that the other snapshot does not hold yet.
    Verification's reference check then fails, which stops the build. The
    exception is a reference with an `on_missing` fallback (`null`, `stub` or
    `drop`), which is applied instead of following it.

    Engines that cannot join databases (PostgreSQL) refuse virtual references
    between databases, and so between their sources. Foreign tables and other
    links between servers are not listed yet. Links to other servers
    ([#80](https://github.com/datadouble/datadouble/issues/80)) will catalog
    them and copy their rows from the source they point at.

6. **Everything else is as for one source.** Images, groups, anonymization
   and verification see one catalog: the sources' catalogs merged. The
   catalog lock records that merged catalog. Anonymization is keyed once per
   run, so equal values get equal replacements in every source. Each
   source's credentials are its own secret references, and CI jobs get all
   of them.
7. **How the core drives it.**

    - The core opens a session per source through the engine adapter's
      `connect`, and plans against the merged catalog.
    - It hands each source's snapshot a view of the plan that holds only that
      source's tables (`Plan.of_source`). That view's catalog fingerprint is
      the source's own.
    - Strategy B reads each table's keys through its own source's snapshot.
    - Each source's extract goes to its own directory.
    - `WorkingDatabases.load` takes all the extracts, in the spec's order.
      The adapter puts every source's databases in the one working server,
      and runs hooks in the first source's default database.

That covers what a build reads. The next section turns to what it makes.

## What a build makes: golden images {#golden-images}

The package stage turns each anonymized working database into a **golden
image**: the finished result for one image group, ready to store, promote
and copy. This section covers four things about golden images, from the
simplest to the most involved:

- the formats an image comes in;
- how one build makes several images;
- how an image can include everything it needs to work on its own;
- the login accounts a physical image carries.

### Formats {#formats}

A golden image comes in one or both of two formats:

- **physical:** the engine's own on-disk form. It is the fastest to clone and
  start, but it is tied to the server version.
- **logical:** a portable export, which can be restored into other server
  versions.

The spec chooses with `package.formats`, which must list at least one:

```yaml
package:
  formats: [physical, logical]
```

- When the spec leaves the field out, each engine produces every format it
  supports.
- The CLI can override the field for one run (`--format logical`). That sets
  the same field in the JSON request.
- The field is part of the build's input hash, and the manifest records it.
- Listing a format an engine can't produce fails the plan, and the error
  names the formats that engine supports. There is no silent fallback.

Each engine produces these:

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

### Several images from one build {#several-images-from-one-build}

Different consumers often want different parts of a source. One team needs
only billing, another needs everything. A spec can split its output into
named image groups, and each group becomes its own image:

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

With no `images:` section there is one group containing everything. Several
groups share the expensive work and keep their images consistent with each
other:

1. **One extract.** Every group shares the plan, the consistent snapshot and
   the extract. The source is read once, however many images come out.
2. **One working database per group.** `load` builds each group's working
   database from the shared extract. Then `transform`, `package` and `verify`
   run once per group.
3. **Images that agree.** Anonymization is seeded once per run, so a value
   gets the same replacement in every group. All images from one build share
   a generation. Consumers that need several groups together take the same
   generation of each, or a group that already contains them. Images are
   never merged after the fact.
4. **References between groups are checked at plan time.** A reference from
   one group into another fails the plan, unless:

    - the edge's fallback is `on_missing: null`, or
    - the group declares that it `requires:` the other group. `replicate`
      then provisions the required images alongside it.

    The `stub` and `drop` fallbacks work only within a group. A reference
    that uses one to leave its group fails the plan.

5. **Independent life cycles.** Each image has its own manifest, identity and
   channels. So each can be promoted, fetched, rolled back and pruned on its
   own.

Groups match names on whatever namespace an engine has, like every name in a
spec (see [table names](subsetting.md#table-names)):

| Engine | Groups match on |
|---|---|
| Relational engines, MongoDB | Databases and schemas |
| Redis | Database numbers and key patterns |
| Elasticsearch, OpenSearch | Index patterns |
| Salesforce | Object sets |
| Filesystem adapter | Top-level directories |

A spec's `files` section is built beside its sources. It produces images of
its own, one per group, all sharing the generation.

### Images that work on their own {#images-that-work-on-their-own}

Splitting a source into groups raises a problem. A group's tables rarely work
alone. A trigger on `billing.invoices` may call a function in another schema.
A column default may use a sequence the group doesn't include. A foreign key
may point at a table in another group. If the image leaves those out, it
restores into a database that fails the first time someone inserts a row.

This section explains how Datadouble finds what an image needs, and how a
**standalone** group pulls all of it in.

#### Package planning and the closure {#closure}

[Subset planning](subsetting.md#subset-planning-and-package-planning) decides
which rows the extract reads, for the whole spec. **Package planning** runs
after it, once per group. It works out everything the group's image needs to
work. The set of things it finds is the group's **closure**.

Package planning starts from:

- the group's own tables;
- the objects that come with those tables: triggers on them, views that read
  only them, and triggers on those views;
- the objects the spec and the group declare under `objects:`, because code
  outside Datadouble expects them to be there;
- for a standalone group, the objects its `include` patterns match.

From there it follows dependencies, one after another, across schemas and
databases, until nothing new turns up. It follows:

- **References between tables.** It goes up from a table to its parents
  where the reference is followed, and down to its children where the spec
  follows them (`follow.down`). A fallback such as `on_missing: null` means
  the parent may be missing, so that reference is not followed.
- **What a table's definition uses:** the sequence or function in a column's
  default, a column's user-defined type, a function a check constraint
  calls, and a table a row-level security policy reads (see
  [source objects](#source-objects)).
- **Every object's dependency edges,** both exact and inferred.

Here is a spec that declares objects at both levels and makes one group
standalone:

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

#### Standalone groups and the others {#standalone-groups}

What a group does with its closure depends on whether it is standalone.

A **standalone** group pulls into its image everything its closure reaches
that the group does not already include. Objects come with their
definitions, and tables come with their rows. A standalone group cannot also
have `requires:`, since its image works alone.

Other groups pull nothing in:

- Their references into other groups need `requires:` or a fallback.
- So do the objects they need that only another group's `include` patterns
  match. Patterns place objects the same way they place tables, matched from
  the right.
- Objects that come with a group's own tables, such as a trigger on one, are
  always that group's own, whatever the patterns say. So are the objects the
  group declares.
- Since such a group pulls nothing in, a declared object that needs a table
  that neither the group nor a group it requires holds fails the plan. The
  error names the table.

!!! warning "Objects declared at the top of the spec apply to every group"
    A top-level view over one group's tables therefore makes every other
    group that is not standalone include those tables or require that group.
    Declare such an object in the group that holds its tables instead.

#### Choosing the rows of pulled-in tables {#rows}

When a standalone group pulls a table in, it has to decide which of that
table's rows to bring. The group sets this with `rows:`. Each key is one of:

- a table name or glob;
- a reference, written `child.column -> parent`, which sets the rows of the
  table that reference reaches.

If both kinds of key name the same table, the reference key wins. Two keys
of the same kind that disagree about a table fail the plan.

```yaml
images:
  billing:
    include: ["billing.*"]
    standalone: true
    rows:
      shop.customers: all                    # every row of the source table
      "invoices.approved_by -> staff": none  # the table this reaches: structure only
```

Each value is one of three settings:

- **`referenced`** is the default for a table a reference reaches. It brings
  the rows the image's rows reference, plus the rows that reference them
  where the spec follows references down, transitively.
- **`all`** brings every row of the source table, and the rows those
  reference in turn. Other groups' images are unaffected.

    - The new rows may reference rows of the group's own tables that the
      group did not have yet. When the group's own tables gain rows this
      way, their references are followed as well, so every reference in the
      image keeps its parent.
    - Tables the subset keeps without rows never gain any. A gained row whose
      `on_missing: drop` reference points at such a table is left out, as
      the shared extract leaves it out. So are rows that reference a row
      left out this way.

- **`none`** brings the table's structure only. References into it are left
  out of the image, as references to another group are.

A table reached only through objects, such as a table a trigger's function
writes to, defaults to `none`, with a warning, since no row of the image
references it.

#### Where the rows come from {#pulled-in-rows}

Pulled-in rows come from the same snapshot as the shared extract. With
several sources, each table's rows and keys come from its own source's
snapshot, as for [references between sources](#several-sources).

The shared extract runs first. After it, each standalone group that pulls
tables in reads those tables again:

- It reads keys first, as the keys strategy does, whichever strategy chose
  the shared rows.
- It writes the rows into an extract of its own per source, beside the
  shared ones (`groups/<group>/<source>`).
- That extract also holds any of the group's own tables that gained rows
  which a pulled-in table references, so every reference in the image has
  its parent.

Under the nested strategy, this first means one keys pass over the whole
subset, since nested selections cannot be read on their own. That is the
cost the nested strategy otherwise avoids, and it is paid only when a
standalone group pulls tables in.

`load` takes the tables listed in the group's own extract from it, and the
group's other tables from the shared extract.

#### What the plan shows, and what fails it {#closure-in-the-plan}

For each group, the plan lists:

- its declared objects;
- every object its image needs, in dependency order;
- for a standalone group, each table (with its rows) and each object pulled
  in, with the path that reached it.

Anything an image cannot have fails the plan. The error names the object and
the path that reached it. Two common cases are:

- an object that needs an excluded table (copies leave such an object out);
- an object the catalog does not list, such as one in a database the spec
  does not read.

When an inferred edge is on the path, the plan warns instead of failing,
since inferred edges never fail a plan on their own. A pulled-in routine
whose edges were inferred warns too, so a person checks it.

#### How the image is built {#building-standalone-images}

`load` builds a standalone image as exactly its closure:

- its own tables and the tables it pulled in;
- its objects, created from their definitions in dependency order;
- none of the source's other objects.

The PostgreSQL, MySQL and SQL Server adapters build standalone images. Other
adapters refuse a standalone group, by name, when it pulls anything in or
the source has objects. That means Oracle, and the engines without
objects (MongoDB, Redis, Elasticsearch, OpenSearch and Salesforce), whose
closures hold tables only.

#### Strict mode and the closure {#closure-and-strict-mode}

Strict mode asks package planning what a difference from the lock breaks
(decision 130). These fail the plan at every strict-mode level:

- a removed object that a packaged table or a declared object still needs;
- a new or changed object on a path to something an image cannot have.

### Accounts in physical images {#accounts-in-physical-images}

A copy is only useful if something can log in to it. That is harder than it
sounds for physical images.

A physical image carries only the accounts the build leaves in it, and the
build removes its own accounts before packaging (decision 74). Now picture a
consumer who serves a volume copy with the engine's official image. That
image's environment variables can't set a password, because they apply only
when a data directory is first created. So nothing could log in over TCP.

`package.accounts` solves this by adding accounts to the image:

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

Each account has a `user`, a `password` and `privileges`, which defaults to
`all`. The rest of this section covers which copies get the accounts, what
each privilege level allows, and how the adapter writes and checks them.

#### Which copies have them

- **Physical images carry the accounts, and so does every copy of one.**
  Volume copies have them. So do instance copies, which are servers the
  replicate adapter runs on a volume copy (decision 62).
- **Logical images never carry them.** A build that makes no physical image
  refuses the setting. So does an engine adapter that can't create accounts.
- **Instance copies reset one user.** When an instance copy's server starts,
  it sets its generated password on the engine's user for instances. That
  user is `datadouble` on MySQL, which Datadouble therefore reserves, and
  `postgres` on PostgreSQL. A declared `postgres` account keeps its password
  on volume copies only. On an instance copy it has the copy's generated
  password.

#### Passwords

- **Passwords may be plain text,** since copies hold no personal data. A
  secret reference works too.
- **`run` resolves every reference when it starts,** before it reads the
  source. `plan` on its own does not.
- **Passwords are never recorded.** The image summary (`accounts`) and the
  build manifest name the users and privileges, never the passwords.

#### Privileges

| Level | What it grants |
|---|---|
| `all` | Every privilege, with the grant option. On PostgreSQL, a superuser. |
| `read` | `SELECT` on the data in the image. |

!!! warning "An `all` account can reach the consumer's container"
    Anyone who can reach a copy and log in with an `all` account can run
    commands in the consumer's container, for example with PostgreSQL's
    `COPY ... TO PROGRAM`.

In detail, `read` grants:

- on MySQL, `SELECT, SHOW VIEW` on each of the image's databases;
- on PostgreSQL, `CONNECT` on each database, and, in every schema but the
  system ones, `USAGE` and `SELECT` on every table, view and sequence.

Nothing in `read` reaches other accounts' password hashes. But `read` is not
strictly read-only:

- On PostgreSQL it also has whatever the image grants `PUBLIC`, which
  Datadouble leaves as the engine made it. That includes creating temporary
  tables, running functions and, before version 15, creating objects in the
  `public` schema.
- On either engine, it can change its own password.

#### Names that fail the plan

An engine can't use some names, and these fail the plan:

- names that are too long;
- names reserved by the engine;
- Datadouble's own users;
- names that must keep `all`, such as PostgreSQL's `postgres`, its bootstrap
  superuser.

Each adapter declares these in `Capabilities.accounts`. Duplicate names fail
the plan too.

#### Local logins on PostgreSQL

On PostgreSQL, local logins need no password. The image's `pg_hba.conf` keeps
the `trust` lines `initdb` wrote for the Unix socket and the loopback
address, ahead of the password rule the accounts add. So any of these logs in
as `postgres` without a password:

- anything inside the consumer's container;
- a sidecar sharing its network;
- a `kubectl port-forward` to it.

#### How the adapter writes and checks them

1. After packaging the image, the engine adapter writes the accounts into the
   image's own data directory (the `Accounts` protocol). If a user of the
   same name exists, its password and privileges are reset.
2. Before the image is stored, the adapter logs in with each account over
   the network.

    - MySQL also checks that a random wrong password is refused.
    - PostgreSQL checks each role in `pg_authid` instead, since its working
      clusters trust loopback logins.

3. The package stage's inputs hold a keyed digest of each account: an HMAC
   under the run's anonymization key. So a retried or resumed build packages
   again when a password changes.

MySQL and PostgreSQL support accounts. Their adapters' READMEs give the
details.

## How a run is carried out {#running}

The sections so far describe what a build reads and makes. This section
describes how a run does the work: how it saves progress, where it keeps
live databases, which software it runs them in, and how it keeps data out
of its error messages.

### Artifacts, manifests and resumability {#artifacts-manifests-and-resumability}

Every stage writes an **artifact**, its output, plus a **manifest** that
records:

- hashes of the stage's inputs and configuration;
- source server versions and tool versions;
- subset rules, resolved excludes and the strict-mode level;
- anonymization strategy hashes;
- verification results;
- timings, row counts and sizes.

Because each stage saves its output, one command can chain several stages:

```
datadouble run plan,extract,load,transform,package,verify,store --spec prod.yml
```

A run belongs to a generation. It keeps its stages' artifacts and manifests
under `<work>/<spec>/<generation>/`. Resuming a generation re-runs only the
stages whose input hashes changed. It skips the rest and reuses their
artifacts. That gives two things: resuming after a failure, and cheap
partial re-runs. For example, changing only the anonymization strategy
re-runs from `transform`:

```
datadouble run plan,extract,load,transform,package,verify,store --spec prod.yml --resume 0193a7c21b4e9f30
```

The generation also fixes two values, so a resumed run produces the same
values as the first attempt:

- the run's anchor time (`as_of`), when the spec gives none;
- its anonymization key, when the spec names none in
  `secrets.anonymization_key`.

!!! warning "The work directory holds real personal data"
    From `extract` on, the work directory holds real personal data. It is
    production scratch space, like the working databases.

### Working space and storage {#working-space-and-storage}

Datadouble keeps two kinds of space apart:

- **Scratch space** holds live database files. They are only ever written
  to scratch space suited to them: local disk or strongly consistent
  volumes.
- **Storage** holds finished, immutable artifacts. Storage adapters handle
  nothing else.

Working databases are throwaway servers the engine adapter starts in scratch
space, one per image group. An adapter may keep one server for a whole run
and serve each stage's database on it in turn. Oracle does this, since its
server takes most of a minute to start. The run closes its working databases
when its builds end, even after a failure (decision 110).

### Engine software runs in the vendor's images {#engine-software-runs-in-the-vendors-images}

Datadouble installs no engine software (decision 72). Instead, these run in
containers from the engine vendor's official image:

- working databases;
- restores for verification;
- tools such as `pg_dump` or `mongorestore`.

#### Which image

By default, Datadouble uses the image of exactly the source's version. A
PostgreSQL 16.4 source is built with `postgres:16.4-trixie`. The exact
version matters, because a dump from a newer minor release can hold what an
older server rejects.

`sources.<name>.image` names a different image. Use it for:

- an image with the extensions the source uses;
- a copy of the image in a private registry;
- a source running a release whose image is not published yet. Without a
  named image, pulling fails.

A spec with several sources gives every source the same `image` and
`server`, since one working server holds them all. By default that server
runs the newest of their versions.

SQL Server images are tagged by release and cumulative update rather than
by build (`mcr.microsoft.com/mssql/server:2022-CU28-ubuntu-22.04`). Microsoft
ships no schema tool in an image, so the adapter scripts the schema from the
catalog views (decision 85).

#### Server settings

`sources.<name>.server` holds settings for the working servers, and the
engine adapter checks them:

- SQL Server's needs `accept_eula: true`, the operator's acceptance of
  Microsoft's licence, and takes an `edition` (decision 83).
- PostgreSQL's takes `shared_preload_libraries`, for extensions that must
  load at server start.

#### Which user Datadouble runs as

Datadouble runs as the `uid:gid` the engine's copies are served as (decision
86):

| Engine | `uid:gid` |
|---|---|
| PostgreSQL, MySQL, MongoDB, Redis (official images) | 999:999 |
| Elasticsearch | 1000:0 |
| OpenSearch | 1000:1000 |
| SQL Server | 10001:10001 |
| Oracle | 54321:54321 |
| Salesforce, files | Whoever runs Datadouble, since no server opens their copies |

This way a physical image's files belong to the server that will open them,
on every replicate adapter. It also means the engine's containers and
Datadouble can each read what the other writes. Datadouble's image runs
under any uid (`docker run --user 999:999`, or a pod's `runAsUser` and
`runAsGroup`). Building, fetching or replicating as anyone else fails, and
the error names the user to run as.

### Container runtimes {#container-runtimes}

The previous section said which images engine software runs in. A
**container runtime** adapter is what starts those containers. The spec
chooses one with `runtime:` (decision 73).

The `docker` runtime drives any Docker-compatible daemon through the
`docker` command:

```yaml
runtime:
  docker: {}
```

The `kubernetes` runtime starts pods through `kubectl`:

```yaml
runtime:
  kubernetes:
    claim: datadouble-work       # the PersistentVolumeClaim holding the work directory
    mount: /work                 # where that claim is mounted in Datadouble's own pod
    kubeconfig:                  # optional: another cluster, by secret reference
      env: BUILD_KUBECONFIG
    context: build               # optional: the kubeconfig's context
    namespace: datadouble        # optional
```

Without `namespace`, pods start where `kubectl` would put them if not told
otherwise:

- Inside a cluster, the namespace of Datadouble's own pod. "Inside a
  cluster" means no `kubeconfig` in the settings, no `KUBECONFIG`, and the
  pod's service account namespace file present.
- Otherwise, the kubeconfig context's namespace, or `default`.

So one spec serves jobs that run as pods in each side's own namespace
(decision 133).

Both runtimes follow the same rules:

- **Shared paths.** Every container sees the paths it works on at the same
  absolute path as Datadouble does.

    - With Docker, those paths are bind-mounted, so the work directory must
      exist at the same path on the daemon's host.
    - On Kubernetes, Datadouble runs as a pod with the work directory on a
      claim, and every engine pod mounts the same claim at the same path.

- **One user.** Containers run as Datadouble's own user and group, so either
  side can read and remove the other's files.
- **Which cluster.** The `kubernetes` runtime, like the `k8s-pv` and
  `k8s-secret` adapters, uses the in-cluster service account or the ambient
  configuration. Its `kubeconfig` setting, a secret reference to a
  kubeconfig's content, names another cluster instead. Datadouble writes
  that content to a private temporary file for each `kubectl` call, and
  removes it afterwards (decision 124).
- **Credentials.** A working server accepts a password generated for that
  start. The password goes through the runtime's secret channel (the
  `docker` command's environment, or a Secret object), never a command line.
  Tools that need the server run inside its container. Credentials made for
  the build are removed before an image is packaged (decision 74).
- **Leftovers.** Containers are labelled with the stage directory they
  serve. Before a stage runs again, the core removes whatever an interrupted
  attempt left behind (decision 75). A resumed run also removes any
  containers left under its generation when it ends, since a killed
  attempt's last container can start after its stage was cleared.

### Errors never show data values {#errors}

Engine error messages often quote the data that caused them. Those values
come from production, so Datadouble must not show them. Errors from engines
name the objects involved, but never data values (decisions 54 and 139).
Here is how that works:

- **Known message shapes.** Each engine adapter lists the message shapes its
  engine uses to report data values, such as PostgreSQL's
  `Key (email)=(…) already exists` or MySQL's
  `Duplicate entry '…' for key`. Those values show as `…`.
- **Unknown messages.** A quoted string after a word that names an object
  (`table "orders"`, `constraint 'pk_orders'`) is kept. Any other quoted
  string is hidden. So a message no adapter has classified stays safe.
- **Messages a user raised.** A trigger's `RAISE EXCEPTION`, MySQL's
  `SIGNAL`, SQL Server's `THROW` and Oracle's `RAISE_APPLICATION_ERROR` can
  put any value anywhere. So every quoted string in such a message is
  hidden.
- **MySQL error numbers.** MySQL lets a trigger raise any error number. So on
  MySQL, a message keeps names only when it is the server's own text for its
  number.

Errors from the container runtime are the one exception. When a tool exits
with status 125, 126 or 127, its output is shown whole:

- `docker run` and `podman run` exit with 125 when they fail before the tool
  starts;
- 126 means the command could not be run, and 127 means it was not found.

That output comes from the container runtime and holds no data, so image
names stay visible:

```text
pg_dump exited with status 125: Unable to find image "postgres:18.7-trixie" locally; docker: Error response from daemon: ...
```

## The pieces {#pieces}

The sections above follow a run. This last part looks at the code that
carries it out: the adapters that hold engine-specific work, the interfaces
people and programs drive Datadouble through, how the repository is laid
out, and the rules for types and credentials.

### Adapters {#adapters}

The core holds the general logic and never contains engine-specific logic.
Everything that depends on one engine, one storage system or one CI/CD
system lives in an **adapter**: a plugin with a narrow interface. Engine
quirks stay inside the adapter.

| Adapter type | Responsibility | Adapters |
|---|---|---|
| Engine (dump/load) | Introspect the catalog, open a consistent snapshot, extract, load, package | postgres, mysql, mssql, oracle, mongodb, redis, elasticsearch, opensearch, salesforce, files |
| Storage | Read and write immutable artifacts; a store that cannot list keys is read through the index `promote` writes | filesystem, s3, http (read-only, for `fetch`) |
| Replicate | Turn a staged image into copies (volumes or instances), and destroy them | filesystem, docker-volume, btrfs, k8s-pv |
| Announce | Let consumers know where clones can be accessed, as volumes or connection parameters; never writes data | k8s-secret, ci-outputs, vault, dotenv (a `k8s-pv` copy announces itself) |
| Container runtime | Start the containers engine software runs in ([described above](#container-runtimes)) | docker, kubernetes |
| CI/CD | Package, generate and bridge jobs for a CI/CD system; no pipeline logic | github-actions, gitlab, jenkins, argocd |

Adapters are discovered through standard Python plugin entry points, one
group per type (`datadouble.engines`, `datadouble.stores`,
`datadouble.replicators`, `datadouble.announcers`, `datadouble.runtimes` and
`datadouble.ci`). So third-party adapters can be installed from anywhere.

Three concerns work the same way for every engine, so they live in the core
rather than in adapters:

- **Subsetting:** the planner that turns a subset spec into per-table
  extraction queries, and its Jailer `.jm` front end.
- **Anonymization:** the strategies that rewrite values column by column:
  faker, hash, null, format-preserving and expression (an engine-native
  language such as SQL). See [anonymization](anonymization.md).
- **Verification:** the checks run on a restored golden image: restore, row
  counts, references, PII scan and assertions.

An engine adapter declares its **capabilities**, and the planner adapts to
them. Capabilities include the formats the adapter produces, whether the
engine can evaluate nested selections (strategy A), whether it has an
expression language, how much of a version names a compatibility line,
whether it can give images accounts, build standalone images, or hold
several sources on one working server.

Where a mature tool already exists, an adapter wraps it rather than
reimplementing it. For most engines that tool is the native dump tool.
Salesforce has no tool that fits a subset of one consistent read, so its
adapter calls the org's own APIs (decision 111).

### Interfaces {#interfaces}

**JSON is primary.** Every operation is a JSON request with a JSON response:

```json
{"command": "run", "stages": ["plan", "extract"], "spec": "prod.yml", "strict": {"data": "columns"}}
```

**The CLI is a thin layer** that builds those requests:

```
datadouble run plan,extract --spec prod.yml --strict data=columns
datadouble --dry-run run plan,extract --spec prod.yml   # print the request only
datadouble --json plan --spec prod.yml                  # the whole plan, as JSON
```

`--dry-run` and `--json` are global options, so they come before the
command.

Because the CLI only builds requests, the two interfaces cannot drift apart.

### Repository layout {#repository-layout}

Everything lives in one repository. Each adapter is published as its own
package, so users install only the drivers they need:

```
datadouble/
  core/            # planner, manifest, JSON API, CLI → package "datadouble"
  adapters/
    postgres/      # → "datadouble-postgres", with its own dependencies
    mysql/
    mssql/
    oracle/
    mongodb/
    redis/
    elasticsearch/
    opensearch/
    salesforce/
    files/
    s3/            # store
    http/          # store, read-only
    btrfs/         # replicate
    docker/        # replicate and container runtime
    kubernetes/    # replicate, announce and container runtime
    vault/         # announce
  ci/
    github-actions/  # composite action, and the package that writes workflows
    gitlab/          # job template, and the package that writes jobs
    jenkins/         # shared library, and the package that writes Jenkinsfiles
    kubernetes/      # Argo CD adapter and Helm chart
  .claude-plugin/  # Claude Code marketplace and plugin manifests
  skills/          # agent skills, one directory per skill
  conformance/     # shared test suites (engine, CI/CD)
  evals/           # evaluations of the agent skills
  dev/             # container images and sample data per engine and CI system
  docs/
```

A user installs `datadouble` and then the adapter packages they need, such
as `datadouble-postgres`. Once the adapter interface is stable, adapters can
move to their own repositories without users noticing.

### Typing {#typing}

Datadouble is written in Python with strict static typing. CI enforces it
with `mypy --strict`, `pyright` in strict mode, and `ruff` with its
annotation rules. The aim is that no value is "stringly typed": a value's
type, not a string's contents, says what it is.

- **Parse at the boundary.** Specs, manifests and JSON requests are parsed
  once into typed, immutable models (Pydantic in strict mode). The same
  models generate the published JSON Schemas. Raw dicts never travel past
  the parser.
- **Closed sets are enums.** Stage names, strict-mode levels, outcomes, copy
  kinds and replicate modes are `StrEnum`s, not string literals.
- **Identifiers have types.** Table names, column paths, generations and
  channel names are distinct types (`NewType` or small value classes), so
  one can't be passed where another is expected.
- **A table name is a namespace path plus a name.** The path lists the
  namespaces outermost first. Each engine's path has a fixed length, and the
  core matches spec names against it from the right, without knowing what
  each part means:

    | Engine | Path | Name |
    |---|---|---|
    | PostgreSQL | `(shop, public)` | `users` |
    | MySQL | `(shop,)` | `users` |
    | Salesforce | nothing | `Account` |

- **Adapter interfaces are typed `Protocol`s,** and every package ships
  `py.typed`, so third-party adapters are checked against them too.
- **Escape hatches are counted.** `Any`, `cast` and `# type: ignore` need a
  comment explaining why, and CI counts them.

### Credentials {#credentials}

Configuration refers to secrets indirectly, by environment variable
(`env: NAME`) or by file (`file: PATH`). Resolved secrets are never written to manifests, artifacts
or logs.
