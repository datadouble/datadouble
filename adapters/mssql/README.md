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
    url: {env: SHOP_MSSQL_URL}   # mssql://user:password@host:1433/database, by reference
    accept_eula: true            # you accept the SQL Server licence for working databases
```
