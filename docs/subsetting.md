# Subsetting

A test copy rarely needs all of production. It needs a slice: recent
customers, say, with everything that makes their data hang together. Taking
that slice is called **subsetting**.

The hard part is scale. Sources can be hundreds of gigabytes or terabytes, so
Datadouble cannot copy everything first and trim it afterwards. Instead it
plans the slice up front and pushes it down into `extract`, the stage that
reads the source. The result must still be relationally consistent: every
reference in the copy points at a row that is also in the copy. And it should
move as little data as possible.

This page builds the idea up from one small example database, in this order:

1. how you describe a subset: a few starting filters, and rules for following
   references from there;
2. what happens to tables those rules don't reach, and how tables are named;
3. how Datadouble runs the subset against the source, and how it keeps every
   read at one point in time;
4. how the same model applies to engines that aren't relational.

## An example database

Picture a small online shop with these tables:

| Table | Holds | References |
|---|---|---|
| `customers` | One row per customer, with `created_at` | |
| `orders` | One row per order | `orders.customer_id -> customers` |
| `order_items` | The lines of each order | `order_items.order_id -> orders`, `order_items.product_id -> products` |
| `products` | The catalog | `products.category_id -> categories` |
| `categories` | A short lookup list of product categories | |
| `reviews` | Customers' reviews of products | `reviews.product_id -> products`, `reviews.customer_id -> customers` |

An arrow `child.column -> parent` means "this column of the child table holds
the key of a row in the parent table". So `orders` is a **child** of
`customers`, and `customers` is a **parent** of `orders`. The spec writes
references the same way.

Production holds millions of customers. For a test copy, we want only the
customers who signed up in the last ten days, and enough of everything else to
make their data work.

## Declare starting filters, derive the rest {#declare-starting-filters-derive-the-rest}

The obvious approach is to write a filter for every table:

```
customers:   where created_at >= NOW() - 10 days
orders:      where customer_id in (select id from customers where created_at >= NOW() - 10 days)
order_items: where order_id in (select id from orders where customer_id in (select ...))
```

This doesn't scale, and it drifts out of sync. The `customers` condition is
repeated in every filter below it, so changing "10 days" means editing each
one. Worse, each `NOW()` is evaluated at a different moment, so the tables
disagree about which customers count as recent.

Datadouble asks for less. You declare:

- **seeds**: the starting filters, usually on one or two tables;
- **a traversal policy**: which references to follow from there, and what to
  do with the ones you don't follow.

The planner then derives every other table's filter from the graph of foreign
keys. The rest of this section introduces each piece, using the shop.

### Seeds and the anchor time

A seed is a condition on one table, written in the source's own query
language. Our seed picks recent customers:

```yaml
subset:
  as_of: "2026-11-09T00:00:00Z"
  seeds:
    customers: "created_at >= :as_of - interval '10 days'"
```

`as_of` is the run's **anchor time**. Datadouble resolves it once, and every
query that says `:as_of` uses that same value. That fixes the `NOW()` problem:
all tables agree on what "the last ten days" means.

### Parents always, children by choice

Seeds choose some rows. The planner then follows references from those rows,
in two directions.

**Up, to parents: always.** If an order is in the copy, its customer must be
too, or the order's foreign key points at nothing. So every included row pulls
in its parent rows. This is not optional: the copy's integrity depends on it.

**Down, to children: only when you ask.** Our seed chose customers. Do we want
their orders? Probably. Do we want every review of every product they bought?
Maybe. Following every child edge is how ten days of customers turns into the
whole database. So you list the child edges to follow, one by one, under
`follow.down`:

```yaml
subset:
  follow:
    down:
      - customers -> orders
      - orders -> order_items
      - products -> reviews
```

Each entry is `parent -> child`, or `parent -> child.column` when the child
has several references to the same parent and you mean one of them.

Trace what this selects:

1. The seed chooses recent customers.
2. `customers -> orders` brings in their orders.
3. `orders -> order_items` brings in the lines of those orders.
4. Each order item references a product. Parents are always pulled in, so
   those products come in too.
5. `products -> reviews` brings in the reviews of those products.

### When a reference costs too much

Step 5 has a problem. Every review references the customer who wrote it, and
parents are always pulled in. So every reviewer of every copied product joins
the copy. Because `customers -> orders` is followed, their orders come too,
then those orders' products, then those products' reviews, and so on. One
popular product can drag in a large part of the database.

For cases like this, each reference can have its own policy under `edges`,
keyed `child.column -> parent`. There are four choices:

| Policy | Effect |
|---|---|
| `follow: all` | Copy the whole parent table. Good for small lookup tables. |
| `on_missing: null` | Don't pull in parents. Blank the reference when its parent isn't in the copy. |
| `on_missing: stub` | Don't pull in parents. Insert a placeholder parent row for each missing one. |
| `on_missing: drop` | Don't pull in parents. Leave out the child row when its parent isn't in the copy. |

An edge takes exactly one of `follow: all` or `on_missing`. The three
`on_missing` choices are the **fallbacks**, and every edge can have one for
when following it costs too much.

For the shop, we blank the author of each review written by a customer who
isn't otherwise in the copy. And `categories` is a short list, so we take all
of it:

```yaml
subset:
  edges:
    reviews.customer_id -> customers:
      on_missing: null
    products.category_id -> categories:
      follow: all
```

### References the schema doesn't declare

Everything so far follows foreign keys the database declares. Many
application schemas declare few or none: the application knows that
`orders.customer_id` points at `customers`, but the database was never told.
So **virtual foreign keys** are a primary input, not an edge case. They
describe relationships the schema doesn't declare, including polymorphic ones
and arbitrary join expressions.

Suppose the shop also has an `events` table. Its `subject_id` column points
at an order when `subject_type` is `'Order'`, and at other tables otherwise.
That is a polymorphic reference. A virtual foreign key with a `when`
condition describes it:

```yaml
subset:
  virtual_fks:
    events.subject_id -> orders:
      when: "subject_type = 'Order'"
```

Once declared, a virtual foreign key works like a declared one. You can follow
it down (`orders -> events`), give it a policy under `edges`, and so on. A
helper can propose candidates from naming conventions (columns named `*_id`)
for a human to confirm.

### The whole spec

Put together, the shop's subset is:

```yaml
subset:
  as_of: "2026-11-09T00:00:00Z"        # resolved once; every query uses the same anchor
  seeds:
    customers: "created_at >= :as_of - interval '10 days'"
  follow:
    down:                              # pull in children: opt-in, per edge
      - customers -> orders
      - orders -> order_items
      - orders -> events
      - products -> reviews
  # up: parents of anything included are always pulled in (required for integrity)
  edges:
    reviews.customer_id -> customers:  # don't drag in every reviewer
      on_missing: null
    products.category_id -> categories: # small lookup table: take everything
      follow: all
  virtual_fks:
    events.subject_id -> orders:       # polymorphic, no real FK
      when: "subject_type = 'Order'"
```

## References in detail {#references-in-detail}

A few more rules about references matter once a subset grows past the basics.

**A `when` condition limits a virtual reference in both directions.** Only
the child rows that match it count as references. In the shop, following
`orders -> events` brings in only events whose `subject_type` is `'Order'`.
And only those events pull in an order as their parent.

**Fallbacks are applied in `load`**, the stage that writes the extracted rows
into the working database:

- `null` clears each reference whose parent is missing;
- `stub` inserts a placeholder parent with the referenced key, and neutral
  values in its required columns;
- `drop` is different: it is part of the selection itself, so the dropped
  rows are never read.

**A reference to an excluded or structure-only table must have a fallback.**
That table copies no rows, so there is no parent to pull in (see
[excludes and strict mode](excludes-and-strict-mode.md)).

**Not yet supported** (decision 52):

- `drop` on a virtual reference with `when`;
- `stub` or `drop` on a reference that leaves an image group.

## Table names {#table-names}

The examples so far name tables with one word, like `customers`. Real sources
often have the same table name in several schemas or databases, so a spec can
give a longer name too. The next section uses globs over these names, so the
naming rules come first.

A spec names a table by its parts joined with dots, and may leave out leading
parts. Names match from the right, like a path read from its end:

| Engine | Full name | Shorter names that work |
|---|---|---|
| PostgreSQL | `shop.public.users` (database, schema, table) | `public.users`, `users` |
| MySQL | `shop.users` (database, table) | `users` |
| SQL Server, Oracle | `dbo.users` (schema, table) | `users` |
| MongoDB, Redis | `cinema.movies`, `db0.user:{}` (database, collection or key pattern) | `movies`, `user:{}` |
| Elasticsearch, OpenSearch | `customers` (index) | |
| Salesforce | `Account` (object) | |
| Files | `files.documents` (the `files` namespace, a top-level directory) | `documents` |

Four rules follow from matching from the right:

- **A name must fit one table.** Suppose two PostgreSQL databases both have
  `public.users`. Then `users` fits both, and the plan fails, naming every
  table it fits. Write enough of the name to choose one.
- **Globs match part by part.** A `*` stays within one part. So `audit.*` is
  every table in namespaces called `audit` (a schema in PostgreSQL, a database
  in MySQL), and `shop.*.*` is every table in PostgreSQL's database `shop`.
  Excludes, `structure_only`, seed patterns and image groups all match this
  way.
- **A table's own name may hold dots,** as MongoDB's `fs.files` does. Such a
  name still matches whole: `fs.files` and `fs.*` both match it.
- **Excluding every table of a database does not leave it unread.** Suppose
  `exclude` holds `shop.*.*`. PostgreSQL's database `shop` is still
  snapshotted, and it is an empty database in the copies, with whatever schema
  objects don't depend on its tables. To leave a database out altogether,
  leave it out of `connection.databases`.

## Tables nothing selects {#tables-nothing-selects}

Look back at the shop's spec. Every table gets rows from somewhere:

- `customers` from its seed;
- `orders`, `order_items`, `events` and `reviews` from followed child edges;
- `products` as the parent of chosen order items;
- `categories` from `follow: all`.

In general, a kept table gets rows in one of three ways:

- from a seed;
- from `follow: all` on a reference to it;
- through a followed edge from a table that has rows. Parent edges are always
  followed, and child edges only along `follow.down`.

Now suppose the shop adds a `coupons` table that nothing references and no
seed names. None of these ways reach it, so it would be copied empty. That is
easy to miss, so by default it fails the plan.

Two settings deal with such tables. Seeds can use globs, and `unselected`
says what happens to whatever is still left:

```yaml
subset:
  seeds:
    "*": "TRUE"                  # globs, as in excludes; a table's own name wins
    orders: "created_at >= :as_of - interval '30 days'"
  unselected: fail               # default; or none (no rows, with a warning) or all
```

### Seed keys can be globs

A seed key is a table name or a glob, matched as in
[table names](#table-names).

- A glob gives its condition to every table it matches that keeps rows. It
  skips excluded and structure-only tables.
- A table's own name wins over a glob. Above, `orders` gets its 30-day
  condition, and every other table that keeps rows gets `TRUE`.
- A table that two globs match fails the plan. So does a glob that matches
  nothing.
- The plan records what each key matched.

### The `unselected` setting

`unselected` decides what happens to a kept table that no seed or followed
reference selects:

| Value | Effect |
|---|---|
| `fail` (default) | The plan fails. It names each such table and the ways out: seed it, make it structure-only, exclude it, or change `unselected`. |
| `none` | The table is copied without rows, and the plan warns. |
| `all` | Every row of each such table is copied. |

Three details about `all`:

- It copies every row, even of a parent that another unselected table would
  otherwise bring in only part of.
- It is the engine-neutral way to copy whole tables. A seed's condition is in
  the source's own language; `unselected: all` works on any engine.
- `on_missing: drop` references still apply to the rows it copies.

**Jailer subsets keep Jailer's behaviour.** Tables that no subject or
association reaches export nothing, as with `unselected: none` (see
[Jailer](jailer.md)).

## Two execution strategies {#two-execution-strategies}

That covers what a subset contains. The next question is how Datadouble reads
it from the source. There are two strategies.

### A. Nested subqueries pushed to the source

The planner generates SQL like the hand-written filters at the top of this
page. For `order_items`, that is a query nested inside a query for `orders`,
nested inside one for `customers`.

This strategy needs one pass and no extra state. It is fast when the paths
through the graph are short and indexed.

It struggles when parent pull-ins loop back. Without the `null` policy, the
shop's reviews bring in their authors, the authors bring in their orders, and
those orders bring in more products and more reviews. Each hop nests the query
one level deeper.

### B. Collect keys first, then fetch rows

1. Read only primary and foreign key columns. These are narrow index scans.
2. Compute the closure of rows to include on Datadouble's side, in an
   embedded database or as compressed in-memory ID sets. Repeat until nothing
   new is added. This handles cycles.
3. Fetch the full rows by key, in chunks.

Strategy B has three advantages:

- it scales to terabyte sources;
- it never needs temporary tables on the source, which read replicas usually
  forbid;
- it lets the planner estimate size before any heavy reads.

So B is the engine, and A is a shortcut the planner may choose when the graph
is shallow. Both must produce identical row sets.

### Choosing a strategy

`subset.strategy` chooses one: `auto` (the default), `nested` (A) or `keys`
(B). The `--strategy` option does the same on the command line.

`auto` picks A when the tables' selections depend on each other without a
cycle, and B otherwise. What counts as a cycle needs care (decision 51):

- **A followed child edge does not by itself make a cycle.** Take
  `customers -> orders`. Orders reached through it already have their
  customer, so customers only depend on orders chosen some other way.
- **True recursions run with B.** These are cycles in the schema,
  self-references, and children that pull in further parents. The shop's
  reviews, without the `null` policy, are the last kind: reviews reached from
  products pull in customers, who pull in more orders.

## Plan mode

Before running a subset for real, you can ask what it would do:

```
datadouble plan --spec prod.yml --estimate
```

This prints each table's derived query, its estimated row count, and the
edges that inflate the subset most, such as "following `reviews.customer_id`
adds 4.1M customers". There are two estimates:

- the cheap one uses the engine's query planner;
- the precise one runs step 1 of strategy B.

## One consistent snapshot

Strategy B reads the source more than once: keys first, rows later. If the
data changed in between, an order could arrive without its customer. So every
read in a run sees the same point in time. How that works depends on the
engine.

**PostgreSQL.** A coordinator connection opens a repeatable-read, read-only
transaction and calls `pg_export_snapshot()`.

- Key reads and every table's rows (`COPY (query) TO STDOUT`) run in that
  transaction.
- `pg_dump --schema-only --snapshot=` writes the schema from the same point
  in time.
- Parallel workers can attach with `SET TRANSACTION SNAPSHOT`.
- The catalog is read again inside the snapshot, and the extract fails if it
  changed since planning.

**PostgreSQL with several databases** (`connection.databases`). PostgreSQL
cannot share a snapshot between databases. So each database gets its own
coordinator transaction and exported snapshot, opened one after another when
the extract starts. No query reads across databases: a virtual reference
between two of them fails the plan. So each database is consistent on its
own. The manifest records every database's snapshot (`snapshots`).

**Several sources** (several servers, under `sources`). No snapshot spans
servers, so each source is read under its own. The snapshots are opened one
after another, before anything is read. An image of several sources is
therefore not one point in time: its manifest records each source's snapshot
and when it was opened.

- A virtual reference between sources runs with strategy B. Its keys are read
  through each side's own snapshot, and the plan lists it
  (`between_sources`).
- A child read in one snapshot may name a parent the other snapshot does not
  hold yet. Verification's reference check then fails and stops the build,
  unless the reference has an `on_missing` fallback.
- Foreign tables between servers are not listed yet. Links to other servers
  ([#80](https://github.com/datadouble/datadouble/issues/80)) will handle them
  (see [several sources](architecture.md#several-sources)).

**MySQL.** `START TRANSACTION WITH CONSISTENT SNAPSHOT` runs on one
connection, which reads every key and row. The extract runs on that single
connection.

- The snapshot covers every InnoDB table on the server. So a source reading
  several databases (`connection.databases`) is still one consistent read,
  references between them included.
- MySQL's catalog is not versioned with the data. So `mysqldump` writes the
  schema separately, and the catalog fingerprint is checked again inside the
  snapshot.

**SQL Server and Oracle.** Snapshot isolation and flashback (SCN-based) reads
respectively. The details are to be designed per adapter.

**MongoDB.** A snapshot session (read concern `snapshot`, pinned to one
cluster time), which needs a replica set or sharded cluster. Collection and
index listings are not allowed inside it. So they run first, and the catalog
fingerprint is checked again inside the session.

**Elasticsearch and OpenSearch.** A point in time, opened over every index the
source's patterns match. Every search pages through it, so the extract sees
the indices as they were when it opened. Mappings are read outside it, and the
catalog fingerprint is checked again once it is open.

**Redis and Salesforce** have no transactional snapshot. Their adapters
document the consistency they can offer.

Long snapshots are not free. On a primary, they hold back cleanup. On a
replica, they conflict with replication. The manifest records how long the
snapshot was held, and the docs recommend extracting from a replica configured
for it.

## Subset planning and package planning

Everything above is **subset planning**. It decides which rows of each table
the extract reads, for the whole spec.

**Package planning** comes after it and asks a different question, once per
image group: what does this image need to work? It follows the same
references. It also follows what tables and objects need of functions, views,
triggers, types and sequences. A `standalone` group pulls what it reaches into
its own image. See
[images that work on their own](architecture.md#images-that-work-on-their-own).

## Non-relational sources

The same model of seeds, followed references and fallbacks applies beyond
relational databases. Each engine maps it onto its own data, and most can run
only strategy B.

**MongoDB.**

- References between collections are declared as virtual foreign keys:
  `ObjectId` fields, embedded IDs, and dotted paths into subdocuments.
- An array of references counts every element as a reference.
- Seeds and `when` clauses are query filters in Extended JSON, with
  `":as_of"` for the plan's anchor time:
  `'{"created": {"$lte": ":as_of"}}'`.
- MongoDB cannot evaluate another collection's selection inside a query, so
  only strategy B is available.
- Columns are field paths, found by sampling documents (`sample`, default
  1000) and from any `$jsonSchema` validator. So strict mode still notices new
  fields.

**Elasticsearch and OpenSearch.**

- Each index is a table keyed by `_id`. Its columns are the field paths its
  mapping declares, such as `address.city`, or `actors.actor_id` inside
  nested documents.
- References are virtual foreign keys to `_id` or to another field. They must
  be `keyword`, numeric or `_id` fields, which queries match exactly.
- Seeds and `when` clauses are Query DSL objects, with `":as_of"` for the
  plan's anchor time.
- Elasticsearch cannot evaluate another index's selection inside a query, so
  only strategy B is available.
- Indices whose mappings let documents hold undeclared fields are sampled, as
  for MongoDB. The extract fails on any field the catalog does not list.

**Salesforce.**

- Objects and their lookup and master-detail relationships form a natural
  graph.
- Filters are SOQL, with `":as_of"` for the plan's anchor time.
- A semi-join reaches only one level and cannot be combined with `OR`, so
  only strategy B is available. Long value lists are split across queries.
- Polymorphic references (`WhoId`, `WhatId`) are declared as virtual foreign
  keys.

**Redis.** Redis has keys, not tables, so the mapping takes more steps.

- Each key pattern is a table. ID-like segments of a key (numbers, UUIDs, long
  hex strings) become `{}`. So `user:42` belongs to `user:{}`, and
  `user:42:sessions` to `user:{}:sessions`.
- Patterns the inference gets wrong are declared in
  `sources.<name>.connection.patterns`.
- A table's key is `_key`: the values its `{}` stand for (`42`).
- Its other columns are what its keys hold:
    - `value` for strings;
    - the fields of hashes and stream entries (sampled, as for MongoDB);
    - `members` for lists, sets and sorted sets;
    - `scores` for sorted sets.
- References in key names are then ordinary references,
  `user:{}:sessions._key -> user:{}`, and so are references in values,
  `order:{}.user_id -> user:{}`.
- Seeds and `when` clauses are globs over `_key`, such as `{where: "1*"}`.
- Datadouble evaluates the planner's conditions itself, against each key it
  reads. So only strategy B is available.
