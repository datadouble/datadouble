# datadouble-mssql

The SQL Server engine adapter for [Datadouble](https://datadouble.tightlinesoftware.com/).

It reads one consistent snapshot of a database (snapshot isolation), scripts
the schema from the catalog views, writes the chosen rows as bulk-load files,
builds working databases as throwaway SQL Server instances, and packages golden
images as the database's data and log files (`physical`) or its schema and rows
(`logical`). Instances run in Microsoft's SQL Server image of the source's
version, through the spec's container runtime.

```yaml
source:
  engine: mssql
  connection:
    url:                         # mssql://user:password@host:1433/database, by reference
      env: SHOP_MSSQL_URL
    accept_eula: true            # you accept the SQL Server licence for working databases
```

## Objects

The catalog lists views, procedures, functions, DML triggers and sequences
from `sys.objects`, and user-defined alias and table types from
`sys.types`. Dependencies come from `sys.sql_expression_dependencies`, which
SQL Server keeps for every module, and are exact. A name SQL Server could
not bind when the module was created (deferred name resolution) is matched
to a table or object of that schema and name, and that edge is marked
inferred; an exact edge to the same target wins. Three-part names in the
source's own database count.

Reading the catalog needs SQL Server 2017 or later, and the `VIEW
DEFINITION` permission on the database (or on each schema): SQL Server's
metadata visibility shows a user only the objects it has some permission
on, and module text only with `VIEW DEFINITION`. Without it, an object
looks empty (its definition reads as nothing) or missing (strict mode
reports it removed), so grant it before locking.

## Requirements

The adapter reports none. The add-ons a SQL Server database could need in its
copies are CLR assemblies and Full-Text Search, which Microsoft's Linux image
leaves out, and the adapter refuses the tables that use them (columns of a CLR
type, full-text indexes) as not supported yet. Exclude those tables to build.
