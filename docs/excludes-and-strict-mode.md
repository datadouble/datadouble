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
- **Native specs** get a `catalog.lock`, written by `datadouble lock` and
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
| Requirement removed, or at another version | **Fail** | **Fail** | **Fail** |
| New requirement | Warn | **Fail** | **Fail** |

Removed or changed objects always fail: the plan cannot run correctly against
them, whatever the strictness.

Requirements are the add-ons a source uses beyond the server itself, such as
PostgreSQL extensions (see [architecture](architecture.md#source-requirements)).
The lock records each one at the source's version, so an extension installed
or upgraded on the source stops the build until someone checks that the
working image has it and re-locks.

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

### Re-locking

`datadouble lock` reads the source, so it runs only inside the production
boundary (decision 98). Its response lists what changed from the lock it
replaces (`changes`): tables, columns, primary and foreign keys and
requirements added, removed or changed, each with its type, key or version.
Column changes cover the type, its kind and nullability, and a table's
column order. The CLI prints the same list. A first lock has nothing to
compare with (`changes` is `null`), so every table and column in it needs
the same check; so does a lock replacing one that cannot be read, which the
response says (`unreadable`, with the reason).

Where the only machine inside the boundary is a CI runner, re-lock through
the manual `lock` job (`ci init --job lock`, see
[CI/CD integration](ci-cd.md#the-lock-job)). It runs `datadouble lock
--output datadouble-lock`, which writes the new lock and its changes
(`changes.txt`) into that directory and leaves the committed lock as it is
(an `--output` that is the committed lock's own directory is refused),
and publishes both as the job's artifact. The job never commits. A person
downloads the artifact, checks every new or changed column for personal
data (anonymizing or excluding it in the spec where it holds any), and
commits the new lock beside the spec. That review is what strict mode is
for: committing a lock without it accepts every new column as safe.
