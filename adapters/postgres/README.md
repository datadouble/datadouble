# datadouble-postgres

The PostgreSQL engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of the source with an exported snapshot,
builds working databases as throwaway PostgreSQL clusters in scratch space,
and packages golden images as a data directory (`physical`) or a `pg_dump`
archive (`logical`). Clusters, restores and `pg_dump` run in the official
PostgreSQL image of the source's exact version, through the spec's container
runtime; a source that uses extensions names an image that has them in
`source.image`.

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
