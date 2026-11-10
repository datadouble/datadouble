# Excludes and strict mode

Two controls decide which tables a run may touch. Excludes are applied first;
strict mode then checks what is left against the subset model.

## Excludes

```yaml
exclude:
  - "dba_maint_*"        # unqualified: matches in any schema
  - "test_*"
  - "audit.*"            # an entire schema
  - "*.tmp_*"            # schema.table patterns
structure_only:          # keep the definition, copy no rows
  - "event_log"
```

- **Shell-style globs**, matched against catalog names. Case-insensitive by
  default, since engines fold identifier case differently (PostgreSQL to lower,
  Oracle to upper, SQL Server by collation). `case_sensitive: true` is available.
- **Resolved once, at plan time.** The plan and manifest record exactly which
  objects each pattern matched.
- **Same idea for other engines:** MongoDB collections, Salesforce objects, Redis
  key patterns.
- Jailer's `exclude-tables.csv` contributes to the same list when a `.jm` model
  is used.

### Integrity with excludes

Excluded tables are removed from the subset graph before planning.

- If an included table depends on an excluded parent, planning **fails** unless
  that edge has an explicit fallback (`null`, `stub` or `drop`).
- Child edges into excluded tables are cut.
- In the restored schema, foreign-key constraints and views that reference
  excluded tables are omitted, and the report lists each one.

## Strict mode

Strict mode needs a record of the schema as it was when the subset model was
built:

- **Jailer models** already carry one: the data model folder (`table.csv`,
  `column.csv`).
- **Native specs** get a `catalog.lock`, written by `datadouble subset lock` and
  committed next to the spec. It lists tables, columns, primary keys and foreign
  keys, with a fingerprint. The Jailer converter writes one too.

At plan time, the table list to extract is the live catalog minus excludes,
compared against the lock.

### Levels

One setting with three levels; each includes the one before it.

```yaml
strict: columns   # default: fail on new tables or new columns
# strict: tables  # fail on new tables only
# strict: off     # warn only
```

CLI: `--strict=columns` (default), `--strict=tables`, `--strict=off`, and
`--no-strict` as shorthand for `off`. JSON: `{"strict": "columns"}`.

| Difference from the lock | `off` | `tables` | `columns` (default) |
|---|---|---|---|
| Known table or column removed or changed | **Fail** | **Fail** | **Fail** |
| New table matching an exclude pattern | Ignored | Ignored | Ignored |
| New table, not excluded | Warn, include | **Fail** | **Fail** |
| New column in a known table | Warn | Warn | **Fail** |

Removed or changed objects always fail: the plan cannot run correctly against
them, whatever the strictness.

### Why the default is strictest

A table or column added to production after the subset model was built has no
anonymization rules. In a permissive mode its contents are copied unmasked. That
is the most common way real personal data ends up in test copies, so the safe
behavior is the default and relaxing it is explicit.

### Failure output

A strict-mode failure returns JSON listing each new object and the ways to
resolve it:

1. Exclude it.
2. Add it to the subset model and re-lock.
3. Add anonymization rules for it, then re-lock.
