# Anonymization

The rows `extract` copies are real production data, personal data included.
Before anything leaves production, Datadouble rewrites that data so it no
longer describes real people. This is **anonymization**.

It runs in the `transform` stage, against the working database: the
throwaway database that `load` filled with the extracted rows. It comes before
`package` and `verify`, so the image is built from anonymized data, and
verification checks the result.

You declare rules per column in the spec. Engines without columns use a field
path or a key pattern instead. This page covers, in order:

1. the strategies a rule can use, and how they run;
2. expressions, the strategy that runs inside the engine, and how each engine
   supports it;
3. hooks, for changes that don't fit one column;
4. values you keep real on purpose, and how verification's personal data scan
   treats them.

The examples use the online shop from
[subsetting](subsetting.md#an-example-database).

## Strategies

Each rule picks one strategy:

| Strategy | Effect | Example |
|---|---|---|
| faker | Replace with a generated value of a given kind | `first_name`, `email`, `phone_number` |
| hash | Replace with a keyed hash, so equal inputs stay equal | Join keys held in non-key columns |
| null | Set to `NULL` | Free-text notes |
| format-preserving | Replace with a value of the same shape | Card numbers, national IDs |
| expression | Replace with an engine-native expression over the row, optionally only where a condition holds | `CONCAT('user-', id, '@example.com')` |

Here are rules for the shop's `customers` table:

```yaml
anonymize:
  customers:
    email:
      faker: email
      unique: true
    last_name:
      faker: last_name
    password:                               # hex, or a 32-bit integer in integer columns
      hash:
        length: 40
    card_number:
      format_preserving:
        luhn: true
    notes:
      null: {}
secrets:
  anonymization_key:                            # optional; a random key per generation otherwise
    env: ANONYMIZATION_KEY
```

The plan checks that each rule fits its column:

- `faker` needs a text column, except `date_of_birth`, which also takes date
  and timestamp columns;
- `hash` and `format_preserving` need a text or integer column;
- `null` needs a column that can be null.

Rules on primary key columns, or on columns a reference uses (declared or
virtual), are refused, whatever the strategy (decision 52).

### Faker values are deterministic

A naive faker would give `alice@example.com` a different fake address every
time it appears. Then a join on email between two tables would stop matching.

So Datadouble seeds each generated value from two things:

- the input value;
- a secret key: `secrets.anonymization_key` when the spec gives one, or else a
  random key per generation. A generation is one build of the spec, kept
  across resumes (see
  [architecture](architecture.md#artifacts-manifests-and-resumability)).

The same input therefore gets the same fake value in every table, and joins
on columns such as email still match.

A rule can require uniqueness (`unique: true`). The generator then retries
until it finds a unique value, or fails the run.

### Faker kinds

The faker kinds are:

- `first_name`, `last_name`, `name`, `user_name`;
- `email`, `phone_number`;
- `street_address`, `city`, `postcode`;
- `company`, `ipv4`, `url`;
- `text`, `date_of_birth`.

Generated values stay out of real ranges, so verification's personal data scan
does not mistake them for real data:

- emails and URLs use the reserved `example.com`, `example.net` and
  `example.org` domains;
- phone numbers use the fictional `555-01xx` range;
- IP addresses use the `192.0.2.0/24` documentation range.

### How value-by-value strategies run

The faker, hash and format-preserving strategies work value by value. Each
distinct value of the column is read once and mapped in the Datadouble
process. The engine then applies the mapping.

## Expressions

Many anonymization needs are easiest to state as an expression over the row
itself, in the engine's own language. Here, each customer's email becomes one
built from their ID, except for staff addresses, and the last login IP is
cleared:

```yaml
anonymize:
  customers:
    email:
      expression: "CONCAT('user-', id, '@example.com')"
      where: "email NOT LIKE '%@company.com'"
    last_login_ip:
      null: {}
```

### How rules become one update

On the SQL engines, Datadouble turns each table's rules into a single
`UPDATE` in the working database. Other engines use their own bulk updates
(see [engines without SQL](#engines-without-sql)). So expressions run at the
speed of the database, not of a client loop.

An update changes only the columns its rules name. On MySQL, that has some
consequences worth knowing:

- `ON UPDATE CURRENT_TIMESTAMP` columns keep their values.
- Triggers are set aside while the rules and hooks run.
- Hooks (below) are SQL as written. So a hook's `UPDATE` on MySQL stamps such
  columns unless it assigns them itself:
  `SET label = 'done', changed = changed`.
- MySQL applies a table's assignments in order. So an expression that reads
  another rule's column sees that column's new value. On PostgreSQL, it sees
  the original.

### Why expressions are column rules

Expressions could have been free-form scripts. They are column rules instead,
because then Datadouble knows which columns each rule rewrites. That matters
in two places:

- The `transform` stage's manifest records each covered column with a
  fingerprint of its rule. Changing a rule redoes `transform` and the stages
  after it, and reuses the extract and load.
- Resolving a strict-mode failure by adding an anonymization rule works the
  same way for expressions as for any other strategy (see
  [strict mode](excludes-and-strict-mode.md#failure-output)).

### Engines without SQL {#engines-without-sql}

The expression strategy uses whatever server-side expression language the
engine has. An adapter that has none doesn't offer the strategy, and the plan
fails if a spec uses it there.

| Engine | Expression language | Applied with |
|---|---|---|
| PostgreSQL, MySQL, SQL Server, Oracle | SQL, in the engine's dialect | `UPDATE … SET … WHERE …` |
| MongoDB | Aggregation expressions (`$concat`, `$toString`, …) | `updateMany` with an update pipeline |
| Elasticsearch, OpenSearch | Painless | `_update_by_query` |
| Redis | Lua | A script run per matching key |
| Salesforce | None (the working copy is not a server Datadouble can run code in) | Not offered; use the other strategies |
| Files (`files:`) | None | Not offered; `files.cleanup` runs shell commands in the copy instead |

For MongoDB, a rule's `expression` is an aggregation expression, and its
`where` is a query filter. Both are written in Extended JSON:

```yaml
anonymize:
  comments:
    text:
      expression: '{"$concat": ["comment ", {"$toString": "$_id"}]}'
      where: '{"flagged": {"$ne": true}}'
```

## Hooks {#hooks}

Column rules cover most needs. Some changes aren't column-shaped, though:

- updates that span several tables;
- deleting rows;
- inserting seed data, such as test accounts.

Those go in hooks. There are two hook points:

- `before_load`: for server settings and extensions;
- `after_transform`: runs after all column rules, and before `package` and
  `verify`, so its effects are verified too.

### Declaring what a hook rewrites

Datadouble cannot see inside a hook's SQL. So an `after_transform` hook that
rewrites personal data declares which columns it rewrites, under `rewrites`.
The plan checks that each one is a column in the catalog, and the `transform`
stage's manifest records them beside the columns the rules cover:

```yaml
hooks:
  after_transform:
    - sql: |
        UPDATE orders o SET shipping_address = a.anonymized
        FROM address_map a WHERE a.id = o.address_id
      rewrites: [orders.shipping_address]
```

A hook's declared columns are taken on trust. `verify` is what checks the
result.

### Engines that don't run hooks

Engines say whether they run hooks, and the plan fails if a spec gives hooks
to one that doesn't. MongoDB, Elasticsearch, OpenSearch, Redis and Salesforce
don't. For MongoDB, decision 68 gives the reason: its column rules cover what
hooks would, and a script language there would be a second anonymizer that
Datadouble cannot see into.

## Values kept on purpose

Anonymization is checked, not trusted. Verification's `pii-scan` check reads
every text column of the restored image and looks for:

- emails outside the reserved example domains;
- card numbers that pass the Luhn check;
- US Social Security numbers in their valid ranges;
- formatted phone numbers outside the fictional range.

Its report gives counts per column and kind, never the values found
(decision 55).

Sometimes rules leave values real on purpose. The `where` in the expression
example above keeps `@company.com` addresses. The scan would flag those, so
the spec lists them as allowed patterns, per column:

```yaml
verify:
  pii_scan:
    allow:
      customers.email: ["%@company.com"]
```

The patterns are SQL `LIKE` patterns, and each must match the whole value.
The plan records them (`pii_allow`). A value that matches a detector, and
that no allow pattern covers, still fails verification.
