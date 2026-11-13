# Subsetting

Sources can be hundreds of gigabytes or terabytes, so subsetting cannot be a
stage that runs after a full copy. It is planned up front and pushed down into
extract, while still producing a relationally consistent result with as little
data transferred as possible.

## Declare starting filters, derive the rest

Hand-writing a filter for every table does not scale and drifts out of sync:

```
users: where created_at >= NOW() - 10 days
posts: where user_id in (select id from users where created_at >= NOW() - 10 days)
```

The `users` condition is repeated, and each `NOW()` is evaluated at a different
moment. Instead, the user declares starting filters plus a traversal policy, and
the planner derives every other table's filter from the foreign-key graph:

```yaml
subset:
  as_of: "2026-11-09T00:00:00Z"        # resolved once; every query uses the same anchor
  seeds:
    users: "created_at >= :as_of - interval '10 days'"
  follow:
    down: [users -> posts, posts -> comments]   # pull in children: opt-in, per edge
  # up: parents of anything included are always pulled in (required for integrity)
  edges:
    comments.author_id -> users: { on_missing: null }   # don't drag in every commenter
    posts.category_id -> categories: { follow: all }    # small lookup table: take everything
  virtual_fks:
    events.subject_id -> posts: { when: "subject_type = 'Post'" }  # polymorphic, no real FK
```

- **Pulling in parents is required.** Every included row needs its parent rows,
  or foreign keys break.
- **Pulling in children is a choice.** Following every child edge is how ten days
  of users turns into the whole database.
- **Every edge has a fallback** for when following it costs too much: `follow`,
  `null` (blank the reference), `stub` (insert a placeholder parent) or `drop`
  (leave the child row out).
- **Virtual foreign keys** describe relationships the schema doesn't declare,
  including polymorphic ones and arbitrary join expressions. Many application
  schemas declare few or no foreign keys, so this is a primary input, not an edge
  case. A helper can propose candidates from naming conventions (`*_id`) for a
  human to confirm.

## Two execution strategies

**A. Nested subqueries pushed to the source.** The planner generates SQL like the
hand-written example above. One pass, no extra state, fast when paths through the
graph are short and indexed. Struggles when parent pull-ins loop back (comments
bring in authors, authors bring in posts, …), because each hop nests deeper.

**B. Collect keys first, then fetch rows.**

1. Pull only primary and foreign key columns: narrow index scans.
2. Compute the closure of rows to include on the tool's side (an embedded
   database or compressed in-memory ID sets), repeating until nothing new is
   added. This handles cycles.
3. Fetch full rows by key, in chunks.

B scales to terabyte sources, never needs temporary tables on the source (read
replicas usually forbid them), and lets the planner estimate size before any
heavy reads. B is the engine; A is a shortcut the planner may choose when the
graph is shallow, and both must produce identical row sets.

`subset.strategy` (`auto`, `nested` or `keys`, also `--strategy`) chooses one.
`auto` picks A when the tables' selections depend on each other without a
cycle, and B otherwise. A followed child edge `users -> posts` does not by
itself make a cycle: posts reached through it already have their user, so users
only depend on posts chosen some other way (decision 51). Cycles in the schema,
self-references and children that pull in further parents are true recursions
and run with B.

### References in detail

- A **virtual reference** with `when` applies only to the child rows matching
  it, in both directions: a followed child edge brings in only those rows, and
  only those rows pull in parents.
- **Fallbacks** are applied in `load`: `null` clears references whose parent is
  missing, `stub` inserts a placeholder parent with the referenced key and
  neutral values in its required columns, and `drop` is part of the selection
  itself. A reference to an excluded or structure-only table must have one.
- **Not yet supported** (decision 52): `drop` on a virtual reference with
  `when`, and `stub` or `drop` on a reference that leaves an image group.

## One consistent snapshot

Every read in a run sees the same point in time.

- **PostgreSQL:** a coordinator connection opens a repeatable-read, read-only
  transaction and calls `pg_export_snapshot()`. Key reads and every table's
  rows (`COPY (query) TO STDOUT`) run in that transaction, and `pg_dump
  --schema-only --snapshot=` writes the schema from the same point in time.
  Parallel workers can attach with `SET TRANSACTION SNAPSHOT`. The catalog is
  read again inside the snapshot, and the extract fails if it changed since
  planning.
- **MySQL:** `START TRANSACTION WITH CONSISTENT SNAPSHOT` on one connection,
  which reads every key and row; the extract runs single-connection. MySQL's
  catalog is not versioned with the data, so `mysqldump` writes the schema
  separately and the catalog fingerprint is checked again inside the snapshot.
- **SQL Server / Oracle:** snapshot isolation and flashback/SCN-based reads
  respectively; details to be designed per adapter.
- **MongoDB:** a snapshot session (read concern `snapshot`, pinned to one cluster
  time), which needs a replica set or sharded cluster. Collection and index
  listings are not allowed inside it, so they run first and the catalog
  fingerprint is checked again inside the session.
- **Redis and Salesforce** have no transactional snapshot. Their adapters document
  the consistency they can offer.

Long snapshots are not free: on a primary they hold back cleanup, and on a
replica they conflict with replication. The manifest records how long the
snapshot was held, and the docs recommend extracting from a replica configured
for it.

## Plan mode

```
datadouble plan --spec prod.yml --estimate
```

Prints each table's derived query, estimated row counts, and the edges that
inflate the subset most ("following `comments.author_id` adds 4.1M users"). The
cheap estimate uses the engine's query planner; the precise one runs step 1 of
strategy B.

## Non-relational sources

- **MongoDB:** the same model applies, with references between collections
  declared as virtual foreign keys (`ObjectId` fields, embedded IDs, dotted
  paths into subdocuments). An array of references counts every element as a
  reference. Seeds and `when` clauses are query filters in Extended JSON, with
  `":as_of"` for the plan's anchor time:
  `'{"created": {"$lte": ":as_of"}}'`. MongoDB cannot evaluate another
  collection's selection inside a query, so only strategy B is available.
  Columns are field paths found by sampling documents (`sample`, default 1000)
  and from any `$jsonSchema` validator, so strict mode still notices new
  fields.
- **Salesforce:** objects and lookup/master-detail relationships form a natural
  graph; filters are SOQL.
- **Redis:** each key pattern is a table. ID-like segments of a key (numbers,
  UUIDs, long hex strings) become `{}`, so `user:42` belongs to `user:{}` and
  `user:42:sessions` to `user:{}:sessions`; patterns the inference gets wrong
  are declared in `source.connection.patterns`. A table's key is `_key`, the
  values its `{}` stand for (`42`), and its other columns are what its keys
  hold: `value` for strings, the fields of hashes and stream entries (sampled,
  as for MongoDB), `members` for lists, sets and sorted sets, and `scores` for
  sorted sets. References in key names are then ordinary references,
  `user:{}:sessions._key -> user:{}`, and so are references in values,
  `order:{}.user_id -> user:{}`. Seeds and `when` clauses are globs over
  `_key` (`{where: "1*"}`). The planner's conditions are evaluated by
  Datadouble against each key it reads, so only strategy B is available.
