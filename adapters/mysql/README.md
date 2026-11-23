# datadouble-mysql

The MySQL engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of a database (`START TRANSACTION WITH
CONSISTENT SNAPSHOT`), writes the chosen rows as bulk-load files and the schema
with `mysqldump`, builds working databases as throwaway MySQL servers, and
packages golden images as a data directory (`physical`) or a `mysqldump`
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
