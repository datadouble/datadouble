# datadouble-mysql

The MySQL engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of one or more databases (`START TRANSACTION
WITH CONSISTENT SNAPSHOT`), writes the chosen rows as bulk-load files and the
schema with `mysqldump`, builds working databases as throwaway MySQL servers,
and packages golden images as a data directory (`physical`) or a `mysqldump`
script (`logical`). Servers, restores and `mysqldump` run in the official MySQL
image of exactly the source's version, through the spec's container runtime.

```yaml
source:
  engine: mysql
  connection:
    url:                         # mysql://user:password@host:3306/database, by reference
      env: SHOP_MYSQL_URL
```

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

Each becomes `'user'@'%'`. `all` grants `ALL ON *.*` with the grant option.
`read` grants `SELECT, SHOW VIEW` on each of the image's databases at
packaging time, not on `*.*`, so it can't read the `mysql` schema's password
hashes; it can still change its own password. An existing user of that name,
such as `root`, gets the password and exactly the declared privileges, and
`root@localhost` keeps logging in over the socket without one. MySQL rewrites
the passwords out of the statements it logs. The build logs in with each
account over TCP, and checks that a wrong password is refused, before storing
the image.

Names longer than 32 characters fail the plan, as do `datadouble-build` (the
build user) and `datadouble` (the user instance copies reset).

## Databases

The URL's database is the one read, unless `connection.databases` lists
others. Each table is named `database.table`, and a spec may leave the
database out wherever only one table fits (`orders` for `shop.orders`).

```yaml
source:
  engine: mysql
  connection:
    url:                           # the database in it is optional with databases
      env: SHOP_MYSQL_URL
    databases: [shop, "billing_*"] # names or globs; ["*"] for every database
```

- **Choosing databases.** Each entry is a name or a glob. A name must exist
  and a glob must match at least one database the URL's user can see, so a
  typing mistake fails instead of quietly reading less. `["*"]` reads every
  database except MySQL's own (`mysql`, `information_schema`,
  `performance_schema`, `sys`), which cannot be named either. With a glob, a
  database created later is read too, and strict mode stops the build at its
  tables until they are locked.
- **One snapshot.** MySQL's consistent snapshot covers every InnoDB table on
  the server, so every database is read at the same point in time, and
  foreign keys, views, triggers and routines across databases are copied as
  they are. A foreign key into a database that is not read keeps its target,
  so the plan asks for a fallback, and the copies drop it. A view over a
  database that is not read could not be created in a copy, so it is left out,
  as a view over an excluded table is; the extract's `index.json` lists every
  view left out (`omitted_views`).
- **The copies hold every database,** under its own name, in one server:
  working databases, both image formats and every copy.
- **The default database.** Spec SQL that names no table (hooks and
  assertions) runs with a default database: the URL's, when it is read, or
  else the first database read in name order. `before_load` hooks run in it
  too: it is created, with the source's character set, before them. Copies
  announce it as their `database`. Qualify names in such SQL
  (`billing.invoices`) to reach the others. Seeds, `when` clauses and
  expression rules run against their own table, so unqualified column names
  in them need nothing.

## Plugins

Plugins the data needs, storage engines and full-text parsers installed from
a library, are requirements named by their library, such as
`plugin mypluglib.so`. Other plugins (authentication, password policy,
audit, replication) change how a server behaves, not what it can load, and
are not. Before reading the source, a build lists the libraries in the
working image's plugin directory and fails with `requirement_unmet` if one
is missing; the version is the server's, so it is not compared. `load`
installs each plugin in the working server, and the data directory keeps it,
so later servers and copies of a physical image load it too. A plugin from
another distribution, such as Percona's MyRocks, needs that distribution's
image in `source.image`.
