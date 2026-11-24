# Extensions, plugins and modules

Most production databases use something beyond the bare server: a PostgreSQL
extension, a MySQL plugin, a Redis module, a search plugin. A golden image has
to carry these too, or it won't work. This page explains how Datadouble finds
them, what it checks, and what to do when a build stops because one is
missing.

It covers, in order:

1. what Datadouble counts as an add-on, engine by engine;
2. what Datadouble does with them during a build;
3. how to fix a build that fails with `requirement_unmet`;
4. two things to watch for afterwards: columns of an add-on's type, and
   add-ons that keep settings in tables.

## Why add-ons matter

Datadouble builds a golden image inside a **working database**: a throwaway
server it starts from the engine vendor's container image. It loads the
copied rows into that server, anonymizes them there, and packages the result.

So the working database must be able to hold everything the copied data uses.
If the source has a `geometry` column from PostGIS, the working server needs
PostGIS too. And it needs the version the source runs, since a different
version can store or behave differently.

Datadouble calls the add-ons a source uses its **requirements**. The rule is:
the image the working database runs must have every requirement the source
uses, at the version the source runs.

## What counts as a requirement

Each engine has its own kind of add-on. This table shows what Datadouble
records for each engine, and how it lists what the working image has:

| Engine | Requirements | Listed in the working image by |
|---|---|---|
| PostgreSQL | Extensions, at the source's version | `pg_available_extension_versions` |
| MySQL | Storage engine and full-text parser plugins, by library | The plugin directory |
| Redis | Modules, at the source's version | `MODULE LIST` on a server that loads the image's modules |
| Elasticsearch, OpenSearch | Plugins, at the source's version | `elasticsearch-plugin list` or `opensearch-plugin list` |
| Oracle | Options: the index types of domain indexes, such as Oracle Text's `CTXSYS.CONTEXT` | `DBA_INDEXTYPES` |
| Salesforce | Installed packages, by namespace, at the installed version | Not checked: copies are local databases that need none |
| SQL Server, MongoDB | None: nothing they copy needs an add-on | |

Three engines work differently:

- **SQL Server** doesn't copy tables that would need an add-on, such as
  tables with CLR types or full-text indexes. Extract refuses them and names
  each one, and you exclude them from the spec.
- **MongoDB** loads nothing into its server, so it has no requirements.
- **Salesforce** copies hold a package's objects and fields as ordinary
  tables and columns. So Datadouble records the installed packages in the
  lock, where strict mode notices one installed, removed or upgraded. But the
  build does not check them.

Each adapter's README says what it reports, and which images provide common
add-ons.

## What Datadouble does

Datadouble tracks requirements the same way it tracks tables, and checks them
before it touches the source's data:

1. **`datadouble lock` records them.** The catalog lock lists each
   requirement with its version, next to the tables.
2. **Strict mode watches them.** Suppose someone installs, removes or upgrades
   an extension on the source. The next build stops until you check the image
   and re-lock, just as it would for a new table.
3. **The build checks the image before reading the source.** If the working
   image lacks a requirement, the build fails at once with
   `requirement_unmet`. The error names each missing requirement, and the
   build stops before it opens the snapshot.
4. **The copies run the source's versions.** A source on `hstore` 1.4 gets
   `hstore` 1.4 in its copies, even if the image's default is newer.

## When a build fails with `requirement_unmet`

A failed check looks like this:

```json
{
  "outcome": "failure",
  "error": "requirement_unmet",
  "message": "the working image lacks 1 requirement(s) of the sources; name an image that has them in sources.<name>.image, or leave them out with sources.<name>.omit",
  "details": [
    {"location": "sources.app.image", "problem": "extension postgis is not in the working image"}
  ]
}
```

Each entry in `details` names one missing requirement. Its `location` is
`sources.<name>.image` for the source that uses it, such as
`sources.app.image`.

There are three ways to fix it. Try them in this order.

### 1. Use an image that has it

Set `sources.<name>.image` to an image built on the source's server release
with the add-on installed, such as `postgis/postgis:18-3.6`. The adapter's
README lists images for common add-ons.

If your source runs a release that no public image matches, build your own
image from the vendor's.

### 2. Set the server settings it needs

Some add-ons must be loaded when the server starts. For PostgreSQL, list them
in `sources.<name>.server.shared_preload_libraries`.

### 3. Leave it out, if your copies don't need it

`sources.<name>.omit` names requirements to leave out of the copies. The usual
case is a managed service's own extensions, such as `aws_s3` on Amazon RDS,
which mean nothing outside that service.

Two rules apply:

- Datadouble never leaves a requirement out on its own. You have to name it.
- Anything that uses an omitted requirement, such as a column of its type,
  must be excluded too. Otherwise the build fails.

### All three together

This source uses an image with PostGIS, preloads `pg_stat_statements`, and
leaves out the Amazon RDS extensions:

```yaml
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: SHOP_DSN
    image: postgis/postgis:18-3.6
    server:
      shared_preload_libraries: [pg_stat_statements]
    omit: [aws_s3, aws_commons]
```

After changing any of these, re-lock with `datadouble lock` if the source
changed. Then run `datadouble plan` to check the spec.

### With several sources

A spec can have several sources, but one working server holds them all. So
that one server must provide every source's requirements, and the sources
must agree about them:

- A requirement two sources use must be at the same version in both.
- One source may leave a requirement out only if every source that uses it
  leaves it out too.

If the sources disagree, the plan fails and names both sources.

## Anonymizing columns of an add-on's type

Some columns have a type that comes from an add-on. Examples are PostGIS
`geometry`, pgvector `vector`, `hstore`, and a RedisJSON document. Datadouble
gives these columns the kind `other`.

Strict mode makes you decide what to do with such a column, like any other.
But built-in strategies such as `faker` don't apply to it. Your choices are:

- `null`, if the column allows nulls;
- `expression`;
- an `after_transform` hook;
- keeping it as it is, if it holds no personal data.

## Configuration tables

Some extensions keep their own settings in tables. For example, `pg_cron`
keeps its jobs in `cron.job`.

Datadouble leaves out every table an extension owns, so these tables are not
in the catalog, and their rows are not copied. A working database creates the
extension fresh, so its configuration tables start empty. A copy of a database
that relies on scheduled jobs arrives with none.
[#117](https://github.com/datadouble/datadouble/issues/117) tracks copying
these tables like any other.
