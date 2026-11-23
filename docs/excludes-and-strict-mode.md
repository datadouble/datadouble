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
  - "archive.*.*"        # an entire PostgreSQL database
structure_only:          # keep the definition, copy no rows
  - "event_log"
```

- **Shell-style globs**, matched against catalog names from the right, part
  by part: a pattern may leave out leading parts, and `*` stays within one
  part ([table names](subsetting.md#table-names)). Case-insensitive by
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

`strict` is a mapping with a level for each kind of thing in the catalog.
Kinds left out take their defaults:

```yaml
strict:
  data: columns       # tables, columns, requirements: off | tables | columns
  functions: warn     # functions and procedures: off | warn | fail
  views: warn
  triggers: warn
  types: warn
  sequences: warn
  events: warn
```

CLI: `--strict KIND=LEVEL`, repeatable, such as `--strict data=tables
--strict functions=fail`; each sets one kind over the spec's levels. JSON:
`{"strict": {"data": "tables"}}`, with the same meaning. A bare level
(`strict: tables`) is refused: say which kind it is for.

`data` has three levels, each stricter than the one before:

| Difference from the lock | `off` | `tables` | `columns` (default) |
|---|---|---|---|
| Known table or column removed | **Fail** | **Fail** | **Fail** |
| Known table's primary key or foreign keys changed | **Fail** | **Fail** | **Fail** |
| Known column's type, kind or nullability changed | Warn | Warn | **Fail** |
| New table matching an exclude pattern | Ignored | Ignored | Ignored |
| New table, not excluded | Warn, include | **Fail** | **Fail** |
| New column in a known table | Warn | Warn | **Fail** |
| Requirement removed, or at another version | **Fail** | **Fail** | **Fail** |
| New requirement | Warn | **Fail** | **Fail** |

Removed tables and columns always fail: the plan cannot run against them,
whatever the level. Changed keys fail too, since the subset model follows
them.

### Differences that break something

A difference that breaks something fails the plan at every level, even
`off`, and the failure says what it breaks. Planning reads the live catalog,
so most changes to a known column break nothing: a widened `varchar` is
copied as it now is. A change does break the column's anonymization rule
when the rule no longer fits it, such as a `faker: email` column that became
an integer, or a `null` rule on a column that can no longer be null. Such a
change fails whatever `data` says; any other change to a known column fails
at `data: columns` and warns at `tables` and `off`.

After reviewing a warned change, re-lock with `datadouble lock` so the next
plan compares against what you checked.

A source that reads several databases (MySQL's and PostgreSQL's
`connection.databases`) has every database's tables in one lock, each named
with its database. A new database that a pattern such as `["*"]` picks up is
new tables, and strict mode treats them like any other; a renamed database is
its tables removed and new ones added.
### Objects

The catalog lists each source's functions and procedures (and Oracle's
packages), views and materialized views, triggers, types, sequences and
events, each with a fingerprint of its definition and the tables and objects
it depends on (see [architecture](architecture.md#source-objects)). The lock
records them, and each kind has its own level:

| Level | New, changed or removed object |
|---|---|
| `off` | Ignored |
| `warn` (default) | Warn, naming it |
| `fail` | **Fail** |

A changed definition means a different fingerprint. The definition counts
as written: only line endings and spaces at the ends of lines are ignored,
since whitespace inside a string or an indented PL/Python body is a real
edit. Object changes warn by default because most are routine, and a person
should still look: a rewritten trigger or function can change what a copy
holds.

Whatever the level, even `off`, a difference that breaks something fails
the plan, naming what it breaks:

- a removed object that a remaining object still depends on, such as a
  function a packaged trigger calls: the failure names both;
- a removed object that a packaged table still needs (a sequence its
  default draws from, a type a column has), or that the spec or a group
  declares in `objects:`;
- a new or changed object on a path to something an image cannot have: a
  new trigger on a packaged table that calls a function reading an excluded
  table, or a function rewritten to read one. Copies leave out an object
  that needs an excluded table, so whatever needs that object breaks.

Package planning (see
[images that work on their own](architecture.md#images-that-work-on-their-own))
finds the last two, since it resolves what each image needs. An object that
depends on an excluded table and that nothing packaged needs is left out of
copies quietly, and one whose tables are all excluded is ignored, as they
are.

Some edges are **inferred**: parsed from a routine's or trigger's body
rather than read from the engine's own records. The plan marks them. Only
exact edges can fail the plan: a removal that only inferred edges point at
warns, at every level, naming what may still need it. An inferred edge knows
a name, not a signature, so a routine whose arguments changed still
satisfies it. Each adapter's README says which of its edges are exact.

After reviewing an object change, re-lock with `datadouble lock`. Locks
written before objects were listed have none, so every object of a
PostgreSQL, MySQL, SQL Server or Oracle source warns as new until you
re-lock. The catalog is read again inside the extract's snapshot and must
match the plan's, so an object changed between planning and extracting
stops the extract, as a changed table does; plan again.

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

A strict-mode failure returns JSON listing each difference, the `strict`
kind it falls under, what it breaks if anything, and the ways to resolve a
new table or column:

1. Exclude it.
2. Add it to the subset model and re-lock.
3. Add anonymization rules for it, then re-lock.

### Re-locking

`datadouble lock` reads the source, so it runs only inside the production
boundary (decision 98). Its response lists what changed from the lock it
replaces (`changes`): tables, columns, primary and foreign keys,
requirements and objects (functions, views, triggers and the like) added,
removed or changed, each with its type, key or version, or for an object
whether its definition or what it depends on changed.
Column changes cover the type, its kind and nullability; a table's changes,
its column order and the objects it needs. The CLI prints the same list. A first lock has nothing to
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
