# datadouble-postgres

The PostgreSQL engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of the source with an exported snapshot,
builds working databases as throwaway PostgreSQL clusters in scratch space,
and packages golden images as a data directory (`physical`) or a `pg_dump`
archive (`logical`). Clusters, restores and `pg_dump` run in the official
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
    - {user: app, password: app-password}          # privileges: all
    - {user: reporting, password: {env: REPORTING_PW}, privileges: read}
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

## Extensions

Every extension installed in the source database, except `plpgsql`, is a
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
  connection: {dsn: {env: SHOP_DSN}}
  image: postgis/postgis:18-3.6          # the source uses PostGIS
  server:
    shared_preload_libraries: [pg_stat_statements]
  omit: [aws_s3, aws_commons]            # RDS-only; nothing kept uses them
```

## Server settings

`source.server` takes `shared_preload_libraries`, a list of library names.
Nothing else.
