# Spec sizes {#spec-sizes}

Different people want different sizes of the same copy. A developer wants a
micro database that restores in seconds on a laptop. QA wants a bigger one
with enough customers for its acceptance tests. Staging wants every row,
anonymized. All three copies come from the same source and have the same
shape: the same sources, the same references to follow, the same
anonymization and the same checks. Mostly they differ in the seeds of a table
or two.

This page explains how to describe those sizes without copying the spec. It
covers, in order:

1. why a second full copy of the spec, or an image group, is the wrong tool;
2. how a spec extends another with `extends`, and how Datadouble finds and
   merges the files;
3. what Datadouble checks in the result;
4. why each size has its own catalog lock;
5. how to see the merged spec, and where a build records the files it came
   from;
6. a complete set of files for a micro, a QA and a staging size.

It assumes you know what a spec, a seed and a catalog lock are. The
[quickstart](quickstart.md) introduces all three, and
[subsetting](../subsetting.md) explains seeds in detail.

## Why not copy the spec, or use image groups {#why-not}

The obvious way to get a second size is to copy `shop.yml` to
`shop-micro.yml` and change its seeds. That works on the first day. Then
someone adds an anonymization rule for a new column to `shop.yml` and forgets
`shop-micro.yml`, or changes a reference to follow in one file and not the
other. Every change has to be made in each copy, and the copies drift apart.

[Image groups](spec-reference.md) look related, but they solve a different
problem. A spec's `images` section splits the tables of one subset into
several golden images, such as one image for the `catalog` schema and one for
everything else. Every group shares the spec's seeds, so every group holds
the same customers. Groups decide which tables go into which image. They
can't make one image with five customers and another with five thousand.

## A size is a spec that extends another {#extends}

A spec file can name other spec files in a top-level `extends` key. Datadouble
reads those files first, then lays the file's own settings over them. So a
size holds only what makes it different.

Here is a base spec for the shop from [subsetting](../subsetting.md):

```yaml
# datadouble/shop.yml
name: shop
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: SHOP_DSN
exclude:
  - audit.*
  - "*.archived_*"
subset:
  seeds:
    customers: "created_at >= :as_of - interval '30 days'"
  follow:
    down:
      - customers -> orders
      - orders -> order_items
anonymize:
  customers:
    email:
      faker: email
store:
  - filesystem:
      path: /srv/datadouble/store
```

And here is a micro size of it:

```yaml
# datadouble/shop-micro.yml
extends: shop.yml
name: shop-micro
subset:
  seeds:
    customers: "id <= 5"
```

`shop-micro.yml` gets everything from `shop.yml`: the source, the excludes,
the references to follow, the anonymization rule and the store. It changes
two things. Its name is `shop-micro`, so its images are filed under
`shop-micro` rather than `shop`. And its `customers` seed picks five
customers instead of a month's worth.

Every command that takes `--spec` reads a size the same way: `datadouble plan
--spec datadouble/shop-micro.yml`, `datadouble run ... --spec
datadouble/shop-micro.yml`, `datadouble ci init --spec
datadouble/shop-micro.yml`, and the same requests in the JSON interface.

### Where Datadouble looks for the files {#paths}

`extends` takes one path, or a list of paths:

```yaml
extends:
  - shop.yml
  - sizes/small-orders.yml
```

The rules for those paths are:

- A path is relative to the directory of the file that names it, not to the
  directory you run Datadouble from. If `sizes/small-orders.yml` itself says
  `extends: ../shop.yml`, that means `datadouble/shop.yml`.
- Every file must be inside the directory of the spec you give to `--spec`,
  or in a subdirectory of it. A path that leads out of that directory fails.
- An absolute path fails.
- Paths are taken as written. Datadouble doesn't follow symbolic links to
  name a file: a link keeps its own name, and a path inside a linked file is
  relative to the link's directory. The same goes for `--spec` itself, so a
  linked spec reads its parents and its lock from beside the link. A
  ConfigMap mounted as a directory in Kubernetes is made of such links, and
  reads like any other directory.

The directory rule exists because that directory is what travels with a
spec. A CI job gets the spec's directory, so the files a spec extends have to
be in it too. [CI/CD integration](../ci-cd.md) covers how jobs get it.

### How the files merge {#merge}

Datadouble arranges the spec and every file it extends in a single order,
then lays each file's own settings over the ones before it. Each file
comes after all the files it extends, and those come in the order its
`extends` lists them. In the list above, `shop.yml` comes first,
`sizes/small-orders.yml` goes over it, and the file that names them both goes
over that. If `sizes/small-orders.yml` extends a file of its own, that file
comes before it, unless it has already come earlier.

Laying one file over another follows two rules:

- A mapping merges key by key, at every depth. A key the later file doesn't
  mention keeps its earlier value.
- Any other value replaces the earlier one completely. That includes lists,
  strings, numbers, booleans and `null`.

There is no way to remove a key that an earlier file set. You can only give
it a new value.

Take `shop.yml` above, and this QA size:

```yaml
# datadouble/shop-qa.yml
extends: shop.yml
name: shop-qa
exclude:
  - audit.*
subset:
  seeds:
    orders: "created_at >= :as_of - interval '7 days'"
```

The merged spec has:

- `exclude` with only `audit.*`. A list replaces the earlier list as a whole,
  so the archived tables that `shop.yml` excluded are now copied. To keep an
  exclude and add another, repeat both.
- Two seeds: `customers` from `shop.yml`, and `orders` from `shop-qa.yml`.
  `seeds` is a mapping, so the two files' keys merge. `shop-qa.yml` can't
  drop the `customers` seed, only replace its condition.
- `follow`, `anonymize`, `sources` and `store` exactly as `shop.yml` has
  them, since `shop-qa.yml` doesn't mention them.

The same rules give a size its own store, its own `replicate` settings, or an
extra anonymization rule. A rule for a new column merges into `anonymize`
beside the base spec's rules.

YAML anchors and aliases work within one file. They don't reach across
files.

### A file that several files extend {#shared}

Two files can extend the same file, such as two sizes of `shop.yml`. A third
file can then extend both, to combine them. Each file still merges exactly
once, at the first place it fits: after every file it extends, before every
file that extends it. Take a size with few customers, a size without a store,
and a file that combines them:

```yaml
# datadouble/few.yml
extends: shop.yml
name: few
exclude:
  - audit.*
subset:
  seeds:
    customers: "id <= 5"
```

```yaml
# datadouble/nostore.yml
extends: shop.yml
name: nostore
store: []
```

```yaml
# datadouble/combo.yml
extends:
  - few.yml
  - nostore.yml
name: combo
```

`combo.yml` merges `shop.yml`, `few.yml`, `nostore.yml` and then its own
settings. `shop.yml` merges once, at the start, so it doesn't come back after
`few.yml` and undo it. `combo` has five customers, excludes only `audit.*`,
and has no store. A file counts as the same file however it is named, such as
through a symbolic link.

## What Datadouble checks {#checks}

Datadouble validates the merged spec once, against the same schema as any
other spec. A file that another file extends doesn't have to be a valid spec
on its own. It can be an overlay that holds only `subset`, with no `name` or
`sources`. But a mistake in any file still fails, because the merged result
holds it. A misspelled `sedes:` in an overlay fails as an unknown option, the
same as in a single file. The error names the spec you gave to `--spec`, and
says the problem is in that spec or the specs it extends.

Three more checks apply only to specs that extend others:

- **Names.** The merged `name` must differ from the `name` written in every
  file it extends, at any depth. Otherwise two specs would file different
  images under one identity. So a size always sets its own `name`, even when
  nothing else about it changes.
- **Cycles.** A file that extends itself, directly or through other files,
  fails, and the error names the chain, such as
  `shop.yml -> shop-micro.yml -> shop.yml`.
- **Readable files.** Each file must exist, be UTF-8 text, and hold a
  mapping. An empty file adds nothing.

## Each size has its own lock {#locks}

Strict mode compares the live catalog with a catalog lock that someone has
reviewed. Each spec has its own lock, beside it and named after it: the
spec's path with the suffix `.lock`.

| Spec | Lock |
|---|---|
| `datadouble/shop.yml` | `datadouble/shop.lock` |
| `datadouble/shop-micro.yml` | `datadouble/shop-micro.lock` |
| `datadouble/shop-qa.yml` | `datadouble/shop-qa.lock` |

`datadouble lock --spec datadouble/shop-micro.yml` writes
`datadouble/shop-micro.lock`. `--lock FILE` names another lock for any
command. With `--output DIR`, as the CI `lock` job runs it, `lock` leaves the
current lock as it is, and writes the new lock under the current lock's file
name, with `DIR/changes.txt` beside it. That is `DIR/shop-micro.lock` here, or
the `--lock` file's name when `--lock` is given.

A size that changes only seeds gets a lock with the same content as its base
spec's. Each size still needs its own lock. A size can also change
`sources`, `exclude` or `structure_only`, and then its lock differs. One rule
for every spec is easier to follow than a shared lock that is right for some
sizes and wrong for others. The cost is that after a schema change, each
size is re-locked and reviewed, and `ci init` gives each spec its own `lock`
job for that.

When a spec's own lock is missing but a `catalog.lock` sits beside it, every
command that uses the default lock fails. The message says to rename
`catalog.lock` to the spec's lock name if it belongs to that spec. Datadouble
never reads it in place of the spec's own lock, because in a directory of
several specs it could belong to any of them.

## Seeing the merged spec {#seeing}

`datadouble spec` prints the merged spec, and the files it was read from,
without reading the source:

```
datadouble spec --spec datadouble/shop-micro.yml
```

```yaml
# read from, in the order they merged:
#   shop.yml (sha256:9d4c...)
#   shop-micro.yml (sha256:51ab...)
name: shop-micro
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: SHOP_DSN
exclude:
  - audit.*
  - '*.archived_*'
subset:
  seeds:
    customers: id <= 5
  follow:
    down:
      - customers -> orders
      - orders -> order_items
anonymize:
  customers:
    email:
      faker: email
store:
  - filesystem:
      path: /srv/datadouble/store
```

The output is the spec as written, merged, without `extends` and without the
defaults that Datadouble fills in. It is a valid spec itself. With `--json`,
or the JSON request `{"command": "spec", "spec": "datadouble/shop-micro.yml"}`,
the response holds the same spec as JSON in `spec`, and the files in `layers`.

Builds record the files too. The plan (`datadouble plan --json`, and
`plan.json` in a build's work directory) and each golden image's manifest
have `spec_layers`: every file the spec was read from, in the order they
merged, with the spec's own file last. Each entry has the file's `path`,
relative to the spec's directory, and the SHA-256 `digest` of its bytes. So
an image says exactly which version of each file built it.

## A complete example {#example}

This example has a base spec, a micro size for laptops, a QA size for
acceptance tests and a staging size that copies every row. All four live in
one directory, `datadouble/`, with their locks:

```
datadouble/
  shop.yml
  shop.lock
  shop-micro.yml
  shop-micro.lock
  shop-qa.yml
  shop-qa.lock
  shop-staging.yml
  shop-staging.lock
```

The base spec, `shop.yml`, has everything the sizes share. It is a complete
spec, and it builds a copy of its own, with a month of customers:

```yaml
# datadouble/shop.yml
name: shop
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: SHOP_DSN
exclude:
  - audit.*
subset:
  seeds:
    customers: "created_at >= :as_of - interval '30 days'"
  follow:
    down:
      - customers -> orders
      - orders -> order_items
      - products -> reviews
  edges:
    reviews.customer_id -> customers:
      on_missing: null
    products.category_id -> categories:
      follow: all
anonymize:
  customers:
    email:
      faker: email
      unique: true
    name:
      faker: name
  reviews:
    body:
      expression: "'redacted'"
store:
  - filesystem:
      path: /srv/datadouble/store
```

The micro size picks five customers. Everything those customers reference,
and the orders and order items that `follow.down` brings in, come with them:

```yaml
# datadouble/shop-micro.yml
extends: shop.yml
name: shop-micro
subset:
  seeds:
    customers: "id <= 5"
```

The QA size picks more customers and leaves the reviews' text as it is,
because QA's tests check how long reviews are displayed. The `reviews` key
under `anonymize` can't be removed, so the QA size gives it a rule that keeps
the text, while `customers` keeps both of its rules from `shop.yml`:

```yaml
# datadouble/shop-qa.yml
extends: shop.yml
name: shop-qa
subset:
  seeds:
    customers: "id % 100 = 0"
anonymize:
  reviews:
    body:
      expression: "body"
```

The staging size copies every row of every table, still anonymized. It seeds
every table with `TRUE`, using a glob (see
[seed keys can be globs](../subsetting.md#seed-keys-can-be-globs)). A table's
own name wins over a glob, and `seeds` merges key by key, so the `customers`
seed from `shop.yml` would still apply. Staging replaces it with `TRUE` as
well:

```yaml
# datadouble/shop-staging.yml
extends: shop.yml
name: shop-staging
subset:
  seeds:
    "*": "TRUE"
    customers: "TRUE"
```

Check what each size merges to, then lock and build it as you would any
spec:

```
datadouble spec --spec datadouble/shop-staging.yml
datadouble lock --spec datadouble/shop-staging.yml
datadouble plan --spec datadouble/shop-staging.yml
```

The `lock` command writes `datadouble/shop-staging.lock`. Review it and
commit it with the spec, as for any lock.

In a CI system, run `datadouble ci init` once for each spec and side. Each
size gets its own jobs, named after its spec, such as
`datadouble-shop-micro-build`, and its own `lock` job.
