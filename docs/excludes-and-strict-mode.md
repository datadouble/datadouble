# Excludes and strict mode

A subset model is written against the schema as it was on one day. Production
keeps changing after that. Someone adds a `phone` column to `customers`, or a
new `gift_messages` table. Nobody wrote anonymization rules for them, because
they didn't exist yet. If a run copied them anyway, real personal data would
end up in every test copy.

Two controls decide which tables a run may touch, and they run in this order:

1. **Excludes** name tables a run never copies. Datadouble removes them first.
2. **Strict mode** then compares what is left against a record of the schema
   the subset model was built for. It warns about, or refuses, anything that
   changed.

This page uses the online shop from [subsetting](subsetting.md#an-example-database):
`customers`, `orders`, `order_items`, `products`, `categories` and `reviews`.

## Excludes

Most production databases hold tables a test copy should never contain:
maintenance tables, audit logs, scratch tables. The spec lists them under
`exclude`, as patterns. Tables whose definition the copy needs, but not their
rows, go under `structure_only` instead:

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

How the patterns work:

- **They are shell-style globs, matched against catalog names from the
  right, part by part.** A pattern may leave out leading parts, and `*` stays
  within one part (see [table names](subsetting.md#table-names)).
- **They are case-insensitive by default.** Engines fold identifier case
  differently: PostgreSQL to lower case, Oracle to upper case, SQL Server by
  collation. Set `case_sensitive: true` to match case exactly.
- **They are resolved once, at plan time.** The plan records exactly which
  tables each pattern matched.
- **They work the same way for other engines:** MongoDB collections,
  Salesforce objects, Redis key patterns.
- **Jailer adds to them.** When a `.jm` model is used, Jailer's
  `exclude-tables.csv` contributes to the same list.

### Integrity with excludes

Excluded tables are removed from the subset graph before planning. That
raises a question: what happens to references into them?

Say the shop excludes `customers`. Every order references a customer, and the
copy will have none.

- **A reference from an included table to an excluded parent fails the plan**,
  unless that edge has an explicit fallback: `null`, `stub` or `drop` (see
  [subsetting](subsetting.md#when-a-reference-costs-too-much)). So
  `orders.customer_id -> customers` needs one.
- **An excluded table's own references are gone.** They are not part of the
  subset graph. If the shop excluded `reviews` instead, the `follow.down`
  entry `products -> reviews` would fail the plan as a reference that doesn't
  exist. Remove such entries, and `edges` policies on such references, when
  you exclude a table.
- **In the restored schema, foreign-key constraints and views that reference
  excluded tables are left out.** The plan lists each left-out reference
  under its image group's `omitted_references`.

## Strict mode

Excludes handle tables you already know about. Strict mode handles the ones
you don't: tables, columns and other objects that appeared or changed after
the subset model was built.

### Why the default is strictest

A table or column added to production after the subset model was built has no
anonymization rules. In a permissive mode, its contents are copied unmasked.
That is the most common way real personal data ends up in test copies. So the
safe behaviour is the default, and relaxing it is explicit.

### The lock: a record of the schema

To notice a change, strict mode needs to know what the schema looked like
when the subset model was built. That record is the **catalog lock**, a file
named after the spec: `shop.lock` for `shop.yml`. `datadouble lock` writes
it, and you commit it next to the spec. Each spec has its own lock, including
each [size](using/spec-sizes.md) of one spec. It lists tables, columns, primary keys and foreign keys, with a
fingerprint. `datadouble import-jailer` writes one too (see
[Jailer](jailer.md#design)). A spec that reads a Jailer model on every run
needs a lock as well: Datadouble checks Jailer's data model folder against
the catalog, but strict mode compares only with the lock.

At plan time, Datadouble takes the live catalog, removes the excludes, and
compares the result against the lock. Without a lock there is nothing to
compare, so the plan fails, unless `data` is `off` and no kind is set to
`fail`. Then it only warns.

### Requirements

Besides tables, the lock records **requirements**: the add-ons a source uses
beyond the server itself, such as PostgreSQL extensions (see
[architecture](architecture.md#source-requirements)). The lock records each
one at the source's version. So an extension upgraded on the source stops
the build until someone checks that the working image has it and re-locks.
A newly installed one does too, unless `data` is `off`.

### Levels {#levels}

Not every kind of change is equally risky. A new column might hold personal
data. A rewritten view is usually routine. So `strict` is a mapping with a
level for each kind of catalog object. Kinds left out take their
defaults:

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

You can set levels in three places:

- **In the spec**, as above.
- **On the command line**, with `--strict KIND=LEVEL`. It is repeatable, as
  in `--strict data=tables --strict functions=fail`, and each one sets one
  kind over the spec's level.
- **In JSON**, as `{"strict": {"data": "tables"}}`, with the same meaning.

A bare level, such as `strict: tables`, is refused. Say which kind it is for.

### The `data` levels

`data` covers tables, columns and requirements. It has three levels, each
stricter than the one before. The table shows what each does with each kind
of difference from the lock:

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

Back to the shop. Someone adds a `phone` column to `customers`. At the default
`columns` level, the plan fails, and you decide what to do with the column.
At `tables` or `off`, the plan only warns, and the phone numbers would be
copied as they are.

Some rows fail at every level:

- **Removed tables and columns always fail.** The plan cannot run against
  them, whatever the level.
- **Changed keys fail too,** since the subset model follows them.

### Differences that break something

Besides the level, one rule applies on top: a difference that breaks
something fails the plan at every level, even `off`, and the failure says what
it breaks.

Most changes to a known column break nothing, because planning reads the live
catalog. If `customers.email` widens from `varchar(100)` to `varchar(255)`, it
is copied as a `varchar(255)`.

A change does break the column's anonymization rule when the rule no longer
fits it. For example:

- a `faker: email` column that became an integer;
- a `null` rule on a column that can no longer be null.

Such a change fails whatever `data` says. Any other change to a known column
fails at `data: columns`, and warns at `tables` and `off`.

After reviewing a warned change, re-lock with `datadouble lock`, so the next
plan compares against what you checked.

### Several databases in one source

A source can read several databases (MySQL's and PostgreSQL's
`connection.databases`). Its lock then holds every database's tables, each
named with its database. Two consequences follow:

- A new database that a pattern such as `["*"]` picks up is new tables, and
  strict mode treats them like any other.
- A renamed database is its tables removed and new ones added.

### Objects {#objects}

Tables are not the only thing a copy can depend on. The catalog also lists
each source's other objects, each with a fingerprint of its definition and the
tables and objects it depends on (see
[architecture](architecture.md#source-objects)):

- functions and procedures (and Oracle's packages);
- views and materialized views;
- triggers;
- types;
- sequences;
- events.

The lock records them, and each kind has its own level:

| Level | New, changed or removed object |
|---|---|
| `off` | Ignored |
| `warn` (default) | Warn, naming it |
| `fail` | **Fail** |

**What counts as changed.** A changed definition means a different
fingerprint. The definition counts as written. Only line endings, spaces at
the ends of lines and blank lines at the start and end are ignored, since
whitespace inside a string or an indented PL/Python body is a real edit.

**Why the default is `warn`.** Most object changes are routine, but a person
should still look. A rewritten trigger or function can change what a copy
holds.

#### Object differences that break something

As with columns, a difference that breaks something fails the plan whatever
the level, even `off`, and names what it breaks:

- **A removed object that a remaining object still depends on.** For example,
  a function that a packaged trigger calls. The failure names both.
- **A removed object that a packaged table still needs,** such as a sequence
  its default draws from or a type a column has. The same goes for an object
  the spec or a group declares in `objects:`.
- **A new or changed object on a path to something an image cannot have.**
  For example, a new trigger on a packaged table that calls a function reading
  an excluded table, or a function rewritten to read one. Copies leave out an
  object that needs an excluded table, so whatever needs that object breaks.

Package planning finds the last two, since it works out what each image
needs (see
[images that work on their own](architecture.md#images-that-work-on-their-own)).
Two kinds of object depending on excluded tables are not failures:

- one that depends on an excluded table, and that nothing packaged needs, is
  left out of copies quietly;
- one whose tables are all excluded is ignored, as those tables are.

#### Exact and inferred edges

Datadouble learns what an object depends on in two ways. Most edges come from
the engine's own records. Some are **inferred**: parsed from a routine's or
trigger's body instead. The plan marks inferred edges.

- **Only exact edges can fail the plan.** A removal that only inferred edges
  point at warns, naming what may still need it. It warns even at `off`, and
  fails only at `fail`, where every difference fails.
- **An inferred edge knows a name, not a signature.** So a routine whose
  arguments changed still satisfies it.

Each adapter's README documents which of its edges are exact.

#### After an object change

- **Re-lock** with `datadouble lock` once you have reviewed the change.
- **A lock that lists no objects** makes every object of a PostgreSQL,
  MySQL, SQL Server or Oracle source warn as new. Re-lock to record them.
- **The extract checks again.** The catalog is read again inside the
  extract's snapshot and must match the plan's. So an object changed between
  planning and extracting stops the extract, as a changed table does. Plan
  again.

## Failure output

A strict-mode failure returns JSON listing each difference, with:

- the `strict` kind it falls under;
- what it breaks, if anything;
- for a new table or column, the ways to resolve it.

There are three ways to resolve a new table or column, like the shop's new
`customers.phone`:

1. Exclude it.
2. Add it to the subset model and re-lock.
3. Add anonymization rules for it, then re-lock.

## Re-locking {#re-locking}

Every way out of a strict-mode failure ends with a new lock. This section
covers how to make one safely.

`datadouble lock` reads the source, so it runs only inside the production
boundary (decision 98). Its response lists what changed from the lock it
replaces, under `changes`:

- tables, columns, primary and foreign keys, requirements and objects
  (functions, views, triggers and the like) that were added, removed or
  changed;
- for each, its type, key or version, or, for an object, whether its
  definition or what it depends on changed;
- for a column, changes to its type, its kind and its nullability;
- for a table, changes to its column order and to the objects it needs.

The CLI prints the same list.

Two cases have nothing to compare with, so every table and column in the new
lock needs the same review:

- **A first lock.** `changes` is `null`.
- **A lock replacing one that cannot be read.** The response says so, under
  `unreadable`, with the reason.

### Re-locking through CI

Sometimes the only machine inside the boundary is a CI runner. Then you
re-lock through the manual `lock` job (`ci init --job lock`, see
[CI/CD integration](ci-cd.md#the-lock-job)). The job:

1. runs `datadouble lock --output datadouble-lock`, which writes the new lock
   and its changes (`changes.txt`) into that directory and leaves the
   committed lock as it is (an `--output` that is the committed lock's own
   directory is refused);
2. publishes both files as the job's artifact;
3. never commits.

A person then downloads the artifact and checks every new or changed column
for personal data. Where a column holds any, they anonymize or exclude it in
the spec. Then they commit the new lock beside the spec.

That review is what strict mode is for. Committing a lock without it accepts
every new column as safe.
