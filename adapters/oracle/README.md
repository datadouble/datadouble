# datadouble-oracle

The Oracle engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of one or more schemas (a read-only
transaction), writes the schema with `DBMS_METADATA` and the chosen rows as
bulk-load files, builds working databases as pluggable databases in throwaway
Oracle Database Free containers, and packages golden images as an unplugged
pluggable database (`physical`) or the schema and rows (`logical`).

```yaml
source:
  engine: oracle
  connection:
    url: {env: SHOP_ORACLE_URL}   # oracle://user:password@host:1521/service, by reference
    schemas: [SHOP]               # the schemas to copy; the connecting user's by default
```
