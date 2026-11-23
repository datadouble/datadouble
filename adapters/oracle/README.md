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

## Objects

The catalog lists the schemas' views and materialized views, functions,
procedures and packages (specification and body as one object), triggers,
types (with their bodies) and sequences; names Oracle generates, such as
identity columns' sequences, are left out. A sequence's fingerprint covers
its settings, not its next number. Dependencies come from `ALL_DEPENDENCIES`,
which Oracle keeps for PL/SQL too, so every edge is exact. Scheduler jobs are
not listed.

Tables list what they need too: their columns' object types and the
functions their virtual columns call come from `ALL_DEPENDENCIES` and are
exact. Oracle records nothing for a column default that draws from a
sequence (`DEFAULT orders_seq.NEXTVAL`), so defaults are parsed for the
objects they name, and those edges are marked inferred. Package planning
follows these edges to work out what an image needs.

The Oracle adapter cannot build standalone images yet: a plan with a
standalone group fails, naming it. Package planning still lists what each
group's image needs.

The `ALL_*` views show only what the connecting user owns or has been
granted. Connecting as each schema's owner, as by default, sees everything.
Another user needs privileges on the objects (or `SELECT ANY DICTIONARY`):
`ALL_OBJECTS` follows grants, and `ALL_SOURCE` shows trigger, package and
type bodies only to their owner or to a user with `EXECUTE` on them, or an
`ANY` privilege. A user without them sees objects as missing (strict mode
reports them removed) or their text as empty, so check before locking.
