# Anonymization

Anonymization runs in the `transform` stage, against the working database, before
`package` and `verify`. Rules are declared per column (or per field path or key
pattern, for engines without columns) in the spec.

## Strategies

| Strategy | Effect | Example |
|---|---|---|
| faker | Replace with a generated value of a given kind | `first_name`, `email`, `phone_number` |
| hash | Replace with a keyed hash, so equal inputs stay equal | Join keys held in non-key columns |
| null | Set to `NULL` | Free-text notes |
| format-preserving | Replace with a value of the same shape | Card numbers, national IDs |
| expression | Replace with an engine-native expression over the row, optionally only where a condition holds | `CONCAT('user-', id, '@example.com')` |

Faker values are **deterministic**: generation is seeded from the input value and
a per-run secret, so the same input gets the same fake value in every table and
joins on columns such as email still match. Rules can require uniqueness, and
the generator retries until a unique value is found or fails the run.

### Expressions

Many anonymization needs are easiest to state as an expression over the row
itself, in the engine's own language:

```yaml
anonymize:
  users:
    email:
      expression: "CONCAT('user-', id, '@example.com')"
      where: "email NOT LIKE '%@company.com'"
    last_login_ip:
      null: {}
```

Datadouble turns each table's rules into one native update in the working
database (for SQL engines, a single `UPDATE` per table), so expressions run at
the speed of the database, not of a client loop.

Expressions are column rules rather than free-form hooks because Datadouble then
knows which columns each rule rewrites. That matters in three places:

- The manifest records a strategy hash per column, so changing one rule re-runs
  only from `transform`.
- Resolving a strict-mode failure by adding an anonymization rule works the same
  way for expressions as for any other strategy.
- The anonymization report lists, per column, which rule covered it.

### Engines without SQL

The expression strategy uses whatever server-side expression language the engine
has. An adapter that has none doesn't offer the strategy, and the plan fails if a
spec uses it there.

| Engine | Expression language | Applied with |
|---|---|---|
| PostgreSQL, MySQL, SQL Server, Oracle | SQL, in the engine's dialect | `UPDATE … SET … WHERE …` |
| MongoDB | Aggregation expressions (`$concat`, `$toString`, …) | `updateMany` with an update pipeline |
| Elasticsearch, OpenSearch | Painless | `_update_by_query` |
| Redis | Lua | A script run per matching key |
| Salesforce | None (the working copy is not a server Datadouble can run code in) | Not offered; use the other strategies |
| Filesystem | Shell | The adapter's cleanup commands |

## Hooks

Some changes aren't column-shaped: multi-table updates, deleting rows, inserting
seed data such as test accounts. Those go in hooks:

- `before_load`: server settings, extensions
- `after_transform`: runs after all column rules, and before `package` and
  `verify`, so its effects are verified too

An `after_transform` hook that rewrites personal data declares which columns it
rewrites, so the anonymization report and strict-mode resolution can count it:

```yaml
hooks:
  after_transform:
    - sql: |
        UPDATE orders o SET shipping_address = a.anonymized
        FROM address_map a WHERE a.id = o.address_id
      rewrites: [orders.shipping_address]
```

A hook's declared columns are taken on trust; `verify` is what checks the
result.

## Values kept on purpose

Rules may leave some values real, as the `where` in the expression example
keeps company addresses. `pii-scan` would flag those, so the spec lists them as
allowed patterns per column:

```yaml
verify:
  pii_scan:
    allow:
      users.email: ["%@company.com"]
```

Allowed patterns are recorded in the manifest, and a value matching a detector
that isn't covered by an allow pattern still fails verification.
