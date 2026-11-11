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

```yaml
anonymize:
  customer:
    email: {faker: email, unique: true}
    last_name: {faker: last_name}
    password: {hash: {length: 40}}          # hex, or a 32-bit integer in integer columns
    card_number: {format_preserving: {luhn: true}}
    notes: {null: {}}
secrets:
  anonymization_key: {env: ANONYMIZATION_KEY}   # optional; a random key per generation otherwise
```

The faker kinds are `first_name`, `last_name`, `name`, `email`, `user_name`,
`phone_number`, `street_address`, `city`, `postcode`, `company`, `ipv4`, `url`,
`text` and `date_of_birth`. Generated emails and URLs use the reserved
`example.com`, `example.net` and `example.org` domains, phone numbers the
fictional `555-01xx` range, and IP addresses the `192.0.2.0/24` documentation
range, so the PII scan does not mistake them for real data.

The faker, hash and format-preserving strategies work value by value: each
distinct value of the column is read once and mapped in the Datadouble process,
and the engine applies the mapping. Rules on primary or foreign key columns are
refused for now (decision 52).

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

For MongoDB, a rule's `expression` is an aggregation expression and its `where`
a query filter, both in Extended JSON:

```yaml
anonymize:
  comments:
    text:
      expression: '{"$concat": ["comment ", {"$toString": "$_id"}]}'
      where: '{"flagged": {"$ne": true}}'
```

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

Engines say whether they run hooks, and the plan fails if a spec gives hooks to
one that doesn't. MongoDB doesn't (decision 68): its column rules cover what
hooks would, and a script language there would be a second anonymizer that
Datadouble cannot see into.

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

The scan reads every text column of the restored image and looks for emails
outside the reserved example domains, card numbers that pass the Luhn check,
US Social Security numbers in their valid ranges, and formatted phone numbers
outside the fictional range. Its report gives counts per column and kind, never
the values found (decision 55).
