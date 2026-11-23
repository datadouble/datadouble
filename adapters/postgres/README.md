# datadouble-postgres

The PostgreSQL engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one or more databases, each under an exported snapshot, builds
working databases as throwaway PostgreSQL clusters in scratch space, and
packages golden images as a data directory (`physical`) or `pg_dump` archives
(`logical`). Clusters, restores and `pg_dump` run in the official
PostgreSQL image of the source's exact version, through the spec's container
runtime; a source that uses extensions names an image that has them in
`source.image`.

## Accounts

A physical image carries no account that logs in over the network, so a
server started on a copy with the official image accepts only local
connections. `package.accounts` adds some:

```yaml
package:
  accounts:
    - user: app                                    # privileges: all
      password: app-password
    - user: reporting
      password:
        env: REPORTING_PW
      privileges: read
```

Each becomes a role that logs in. `all` is a superuser. `read` is a plain
role with `CONNECT` on each database and, in every schema but the system
ones, `USAGE` and `SELECT` on every table, view and sequence; it can't read
`pg_authid` or its password hashes. It also keeps what `PUBLIC` has, which
Datadouble doesn't change: temporary tables, running functions, changing its
own password, and before PostgreSQL 15 creating objects in `public`.

The image's `pg_hba.conf` gains `host all all all scram-sha-256`. An existing
role of that name gets the password and attributes reset. Datadouble computes
a SCRAM verifier and sends only that, so no statement the server could log
holds a password. The build checks each role in `pg_authid` and logs in with
it over TCP before storing the image.

The image's `pg_hba.conf` still trusts local and loopback connections, as
`initdb` wrote it, ahead of that rule, so anything inside the consumer's
container, a sidecar sharing its network, or a `kubectl port-forward` to it
logs in as `postgres` without a password.

An instance copy's server sets the copy's generated password on `postgres`
when it starts, so a declared `postgres` account's password holds on volume
copies only.

The plan refuses names longer than 63 bytes, names starting with `pg_`, the
names PostgreSQL reserves (`public`, `none`, `current_user`, `session_user`,
`current_role`), and `postgres` with `privileges: read`, since the bootstrap
superuser can't stop being one.

## Databases

The connection string's database is the one read, unless
`connection.databases` lists others. Tables are named `database.schema.table`
even when only one database is read, and a spec may leave out leading parts
wherever only one table fits: `public.users` or `users` for
`shop.public.users`. A name that fits tables in several databases fails the
plan, naming each; write enough of it to choose one.

```yaml
source:
  engine: postgres
  connection:
    dsn:                             # which database it names matters only as the default
      env: SHOP_DSN
    databases: [shop, "billing_*"]   # names or globs; ["*"] for every database
```

- **Choosing databases.** Each entry is a name or a glob. A name must exist
  and a glob must match at least one database the user may connect to, so a
  typing mistake fails instead of quietly reading less. `["*"]` reads every
  database the user can connect to except templates; `postgres` is an
  ordinary database here, since some services keep an application's data in
  it. With a glob, a database created later is read too, and strict mode
  stops the build at its tables until they are locked.
- **A snapshot per database.** PostgreSQL cannot share a snapshot between
  databases, so each is read under its own, all opened together as the
  extract starts. PostgreSQL has no references between databases, so each
  database is consistent on its own; a virtual reference between two
  databases fails the plan, since no query could follow it. The manifest
  records each database's snapshot under `snapshots`.
- **The copies hold every database,** under its own name, in one cluster:
  working databases, physical images, and logical images (one `pg_dump`
  archive per database).
  Excluding every table of a database (`shop.*.*`) still reads it under its
  snapshot and leaves it empty in the copies; leave it out of `databases` to
  leave it out altogether.
- **The default database.** Spec SQL that names no table (hooks and
  assertions) runs in a default database: the connection string's, when it
  is read, or else the first database read in name order. Copies announce it
  as their `database`. Seeds, `when` clauses and expression rules run in their
  own table's database.
- **Extensions are per database.** Each database's extensions are created in
  its copy at its versions, and the lock lists every one.
Sources must be PostgreSQL 13 or later: reading which triggers a partition
inherits from its parent needs `pg_trigger.tgparentid`.

## Extensions

Every extension installed in a database read, except `plpgsql`, is a
requirement at the version the source runs (`pg_extension.extversion`).

- **The image must have it.** Before reading the snapshot, a build lists the
  extension versions the working image offers
  (`pg_available_extension_versions`) and fails with `requirement_unmet` if
  one is missing. The official image has everything in contrib (`hstore`,
  `pg_trgm`, `citext`, `pgcrypto`, `uuid-ossp`, `postgres_fdw` and the rest).
  Other extensions need an image that adds them, such as
  `pgvector/pgvector:0.8.7-pg18-trixie` for `vector`,
  `postgis/postgis:18-3.6` for PostGIS or `timescale/timescaledb:latest-pg18`
  (pin a release) for TimescaleDB. Pick one built on the source's PostgreSQL
  release.
- **The working database runs the source's version.** `load` creates each
  extension at the source's version, so a source on `hstore` 1.4 gets 1.4
  even when the image's default is 1.8. Many third-party extensions install
  only their newest version (pgvector 0.8.7 cannot create 0.8.2), so pick
  the image release that matches the source's extension version.
- **Some extensions load at server start.** Name them in
  `source.server.shared_preload_libraries`. Working clusters start with the
  setting, and a physical image keeps it in `postgresql.conf`, so its copies
  load the libraries too.
- **A managed service's own extensions** (`aws_s3`, `aws_commons`,
  `rds_tools`, `google_ml_integration`, `azure`) are in no public image.
  Leave them out with `source.omit`, and exclude whatever uses them.
- **Logical images** restore with `pg_restore`, which creates extensions at
  the restoring server's default version.

```yaml
source:
  engine: postgres
  connection:
    dsn:
      env: SHOP_DSN
  image: postgis/postgis:18-3.6          # the source uses PostGIS
  server:
    shared_preload_libraries: [pg_stat_statements]
  omit: [aws_s3, aws_commons]            # RDS-only; nothing kept uses them
```

## Server settings

`source.server` takes `shared_preload_libraries`, a list of library names.
Nothing else.

## Objects

The catalog lists functions and procedures (by name and argument types),
views and materialized views, triggers, enum, domain, composite and range
types, and sequences, outside the system schemas and not owned by an
extension, in every database read. Objects are named like tables,
`database.schema.object`; a trigger's last part is `table.trigger`
(`shop.public.users.audit`), since trigger names are only unique per table.
Each database's objects are read under its own snapshot, and since
PostgreSQL has nothing that refers across databases, their edges stay within
it. Dependencies come from `pg_depend`, which is
exact for views, materialized views, triggers, types, sequences and
routines with SQL-standard bodies (`BEGIN ATOMIC`). Other routine bodies,
such as PL/pgSQL, are parsed for the tables and objects they name, and those
edges are marked inferred: a statement built with `EXECUTE` at run time is
not seen.

Tables list what they need too, exactly from `pg_depend`: the sequences and
functions their column defaults and generated columns use, the functions
their check constraints call, and their columns' enum, domain, composite and
range types. Package planning follows these edges to work out what an
image needs.

A standalone group's database holds exactly its closure. The schema is
restored as for any group, the pulled-in tables are kept with the rows of
the group's own extract, and then every object the closure lacks is
dropped, dependents first and without `CASCADE`: a dependency `pg_depend`
records but the catalog missed stops the load with PostgreSQL's error
rather than removing more. Foreign keys into a pulled-in table without rows
are left out. Schemas stay, even when nothing in them is kept.
