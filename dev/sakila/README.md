# Sakila fixture

The Sakila sample database's rows, in a bulk-load format shared by every
relational engine's test database (`dev/mysql`, `dev/mssql`, `dev/oracle`).
The rows are committed so test data never changes underneath the tests; each
engine's own schema comes from the same upstream collection.

- **Source:** [jOOQ's Sakila collection](https://github.com/jOOQ/sakila) at
  commit `e089a5b1ec9af0df7a9c6a5d47d49fa1736a4e84`,
  `mysql-sakila-db/mysql-sakila-insert-data.sql`.
- **Conversion:** `uv run scripts/sakila_bulk.py <insert script> dev/sakila/data`.
  Each `data/<table>.tsv` has a header line of column names, then one row per
  line: tab-separated fields, `\N` for NULL, backslash escapes for backslash,
  tab, newline and carriage return. `data/tables.txt` lists the tables in load
  order with their row counts.
- **Licence:** BSD, from the original authors, the porters and jOOQ; see
  [LICENSE.txt](LICENSE.txt). These files are test fixtures only and are not
  part of any Datadouble package or image.
- **Delivery:** the Compose `fixtures` service copies them into a volume the
  database containers mount read-only, so they are in neither the database
  images nor the test runner image.
