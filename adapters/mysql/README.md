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
    url: {env: SHOP_MYSQL_URL}   # mysql://user:password@host:3306/database, by reference
```
