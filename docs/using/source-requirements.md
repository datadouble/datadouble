# Extensions, plugins and modules

Most production databases use something beyond the server itself: a
PostgreSQL extension, a MySQL plugin, a Redis module, a search plugin. Datadouble
calls these the source's **requirements**. A golden image is built in a
working database running the vendor's image, so that image must have every
requirement the source uses, at the version the source runs.

PostgreSQL reports its extensions today. The other engines follow (see the
[v1 plan](../v1-plan.md)), and this page grows with them.

## What Datadouble does

1. **`datadouble lock` records them.** The catalog lock lists each
   requirement with its version, next to the tables.
2. **Strict mode watches them.** If someone installs, removes or upgrades an
   extension on the source, the next build stops until you check the image
   and re-lock, just as it would for a new table.
3. **The build checks the image before reading the source.** If the working
   image lacks one, the build fails at once with `requirement_unmet`, naming
   each one, before it opens the snapshot.
4. **The copies run the source's versions.** A source on `hstore` 1.4 gets
   `hstore` 1.4 in its copies, even if the image's default is newer.

## When a build fails with `requirement_unmet`

```json
{
  "outcome": "failure",
  "error": "requirement_unmet",
  "message": "the working image lacks 1 requirement(s) of the source; name an image that has them in source.image, or leave them out with source.omit",
  "details": [
    {"location": "source.image", "problem": "extension postgis is not in the working image"}
  ]
}
```

Pick one of these, in this order:

1. **Use an image that has it.** Set `source.image` to an image built on the
   source's server release with the add-on installed, such as
   `postgis/postgis:18-3.6`. The adapter's README lists images for common
   add-ons. If your source runs a release no public image matches, build your
   own image from the vendor's.
2. **Set the server settings it needs.** Some add-ons load at server start;
   for PostgreSQL, list them in `source.server.shared_preload_libraries`.
3. **Leave it out, if your copies don't need it.** `source.omit` names
   requirements to leave out. Datadouble never leaves one out on its own, and
   anything that uses an omitted requirement, such as a column of its type,
   must be excluded too, or the build fails. Managed services' own extensions,
   such as `aws_s3` on Amazon RDS, are the usual case.

```yaml
source:
  engine: postgres
  connection: {dsn: {env: SHOP_DSN}}
  image: postgis/postgis:18-3.6
  server:
    shared_preload_libraries: [pg_stat_statements]
  omit: [aws_s3, aws_commons]
```

After changing any of these, re-lock with `datadouble lock` if the source
changed, and run `datadouble plan` to check the spec.

## Anonymizing columns of an add-on's type

A column whose type comes from an add-on, such as PostGIS `geometry`,
pgvector `vector` or `hstore`, has the kind `other`. Strict mode makes you
decide what to do with it like any other column. Built-in strategies such as
`faker` don't apply to it; use `null`, `expression` or an `after_transform`
hook, or keep it if it holds no personal data.

## Configuration tables

Some extensions keep their own settings in tables, such as `pg_cron`'s
`cron.job`. Those rows travel with the schema and can hold job commands with
credentials in them. Check them before you trust a copy.
