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
    url:                          # oracle://user:password@host:1521/service, by reference
      env: SHOP_ORACLE_URL
    schemas: [SHOP]               # the schemas to copy; the connecting user's by default
```

## Options

The schemas' domain indexes are the requirements: each index type they use
from outside the copied schemas, such as Oracle Text's `CTXSYS.CONTEXT` or
Spatial's `MDSYS.SPATIAL_INDEX_V2`, is an option, listed in the lock as
`option CTXSYS.CONTEXT`. The working image provides those in
`DBA_INDEXTYPES`. Oracle Database Free's full image includes Oracle Text; its
`-lite` image leaves components out. `source.omit` names an
index type to leave out, and the copies then have no indexes of that type;
the rows are the same.
