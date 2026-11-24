# Jailer compatibility

[Jailer](https://github.com/Wisser/Jailer) (Apache-2.0) is a mature tool for
designing database subsets, with a GUI for exploring the schema and deciding
which references to follow. What it doesn't do is run that subset on a
schedule, anonymize it and package it. Datadouble does.

So Datadouble supports Jailer models **natively**. A team can design a subset
in Jailer, then have Datadouble run it over and over as the data changes.
A command, `import-jailer`, also converts a Jailer model to Datadouble's own
format. Either way, a Jailer model that doesn't match the schema fails.

This page covers:

1. what a Jailer subset is made of;
2. the two ways Datadouble uses one;
3. how Jailer's concepts map onto Datadouble's, and what isn't supported yet;
4. the subtleties of the mapping, and the test that checks it.

It assumes you know Datadouble's own subset model: seeds, followed child
edges and fallbacks (see [subsetting](subsetting.md)).

## What a Jailer subset consists of

A Jailer subset has two parts.

### The data model folder

The data model folder (`datamodel/<name>/*.csv`) describes the schema:
tables, primary keys, columns, and **associations**. An association is
Jailer's word for a reference between two tables:

```
# Table A; Table B; First-insert; Cardinality; Join-condition; Name
EMPLOYEE; DEPARTMENT; B; n:1; A.DEPTNO=B.DEPTNO; DEPARTMENT
EMPLOYEE; SALARYGRADE; ; n:1; A.SALARY BETWEEN B.LOSAL and B.HISAL; SALARY
```

Associations can be virtual: any join condition, including non-equality
joins like the `SALARY` one above, with no foreign key behind it.

### The extraction model

The extraction model (a `*.jm` file) says what to extract. It holds:

- **subjects**: a starting table and a condition, with an optional limit and
  order;
- **additional subjects**;
- **restrictions** per association: either `ignore`, or an SQL condition.

```
# subject; condition; limit; limit-order
EMPLOYEE; T.NAME='SCOTT'; ;
# association; ; restriction-condition
inverse-BOSS; ; ignore
```

It can also hold column filters, `${param}` placeholders, and GUI and output
settings (layout, XML mapping, export mode).

## Two ways to use a Jailer model {#design}

Both formats compile to the same internal subset model, which the planner
then runs:

```
native YAML/JSON spec ─┐
                       ├─► subset model (graph + rules) ─► planner ─► extract
Jailer .jm + datamodel ┘
```

There are two ways in:

- **Read it on every run.** Name the model in the spec, under
  `subset.jailer`, with the extraction model and the data model folder it was
  designed against. Datadouble then reads the Jailer files on every run.
  Jailer stays the design tool. Edits committed to the `.jm` take effect on
  the next run, with no regeneration step. A spec with `subset.jailer` cannot
  also set `seeds`, `follow`, `edges`, `virtual_fks` or `unselected`.

    ```yaml
    subset:
      jailer:
        model: jailer/shop.jm
        datamodel: jailer/datamodel/shop
    ```

- **Convert it once.** `datadouble import-jailer --spec prod.yml --model
  model.jm --datamodel dir/ --output subset.yml` writes the native `subset`
  and `exclude` settings, as YAML, to the `--output` file, for you to copy
  into the spec. It also writes a `catalog.lock`, and its response reports
  anything it mapped approximately or dropped.

Either way, the data model is checked against the live catalog. A table or
column the Jailer model has and the catalog doesn't, or a primary key that
differs, fails.

## Mapping {#mapping}

What version 1 translates (decision 81):

| Jailer | Datadouble |
|---|---|
| Subject + condition (`T.` alias), additional subjects | Seed filter; several subjects on one table are joined with `OR` |
| Association with an equality join, `first-insert` side or `n:1` | A declared foreign key when the catalog has one with those columns, otherwise a virtual one |
| Child direction (`inverse-<name>` of an `n:1` association), unrestricted | A followed child edge (Jailer follows every association unless restricted) |
| Restriction `ignore` on a child direction | Not followed |
| Restriction `ignore` on a parent direction, or an excluded association | `on_missing: null` (Jailer exports the row with the reference dangling; the key sets agree) |
| `exclude-tables.csv` | Excludes, added to the spec's own |
| A table no subject or association reaches | No rows, with a warning, as `subset.unselected: none`; `import-jailer` writes that setting, and a spec cannot set `unselected` beside `subset.jailer` |
| Data model `table.csv` | Checked against the catalog: a primary key that differs fails the plan |
| Layout, XML mapping, export mode, version and other display settings | Ignored, listed in the report |

### Not yet supported

Some Jailer features have no native equivalent yet. A model that uses one
fails the plan, naming the feature, instead of running a different subset:

- restriction conditions (`A.` / `B.` SQL);
- joins other than column equality;
- subject limits and orders;
- `${param}` placeholders;
- column filters.

Each would need a native equivalent first.

## Subtleties

1. **Opposite defaults.** Jailer follows every association, including child
   edges, unless restricted. The native format follows child edges only when
   asked. So the Jailer front end lists every unrestricted child direction as
   a followed edge, and a model produces the same subset in both tools.
2. **Identical results.** Jailer runs by collecting keys in working tables,
   round after round. That is the same approach as Datadouble's collect-keys
   strategy (see
   [two execution strategies](subsetting.md#two-execution-strategies)). Any
   shortcut the planner takes must produce identical row sets.
3. **Schema drift.** The data model is a snapshot of the schema. The planner
   compares it to the live catalog. Removed or changed objects fail, and new
   ones are handled by [strict mode](excludes-and-strict-mode.md).
4. **Raw SQL conditions.** A subject's condition, in the source's dialect,
   is passed through with its `T.` alias removed. Restriction conditions,
   which use `A.` and `B.`, are not supported yet (see
   [above](#not-yet-supported)).
5. **Relational only.** Jailer is JDBC-based, so all of this applies to the
   relational engines, not to MongoDB, Redis or Salesforce.

## Conformance test

A test checks that Datadouble and Jailer choose the same rows from the same
model:

1. The Compose `jailer` service (Jailer 17.3.1, pinned) builds Jailer's data
   model of the MySQL Sakila sample database.
2. It exports `dev/jailer/sakila.jm` as JSON. The export is unsorted, because
   Sakila's reference cycles have no insert order. Its key collection runs in
   an embedded database, so nothing is written to the source.
3. The test runs the same model through Datadouble's front end with the keys
   strategy.
4. It compares the primary-key sets of every table.

They are identical: 8,501 rows.
