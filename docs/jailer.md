# Jailer compatibility

[Jailer](https://github.com/Wisser/Jailer) (Apache-2.0) has a mature GUI for
designing database subsets. Datadouble supports Jailer models **natively**: a
subset can be designed in Jailer and then run by Datadouble on a schedule, over
and over, as the data changes. A converter to the native format also exists, and
fails on schema mismatch.

## What a Jailer subset consists of

**Data model folder** (`datamodel/<name>/*.csv`): tables, primary keys, columns,
and associations:

```
# Table A; Table B; First-insert; Cardinality; Join-condition; Name
EMPLOYEE; DEPARTMENT; B; n:1; A.DEPTNO=B.DEPTNO; DEPARTMENT
EMPLOYEE; SALARYGRADE; ; n:1; A.SALARY BETWEEN B.LOSAL and B.HISAL; SALARY
```

Associations can be virtual: any join condition, including non-equality joins,
with no foreign key behind it.

**Extraction model** (`*.jm`): subjects (starting table + condition, optional
limit and order), additional subjects, and per-association restrictions, either
`ignore` or an SQL condition:

```
# subject; condition; limit; limit-order
EMPLOYEE; T.NAME='SCOTT'; ;
# association; ; restriction-condition
inverse-BOSS; ; ignore
```

It can also hold column filters, `${param}` placeholders, and GUI/output settings
(layout, XML mapping, export mode).

## Mapping

What version 1 translates (decision 81):

| Jailer | Datadouble |
|---|---|
| Subject + condition (`T.` alias), additional subjects | Seed filter; several subjects on one table are joined with `OR` |
| Association with an equality join, `first-insert` side or `n:1` | A declared foreign key when the catalog has one with those columns, otherwise a virtual one |
| Child direction (`inverse-<name>` of an `n:1` association), unrestricted | A followed child edge (Jailer follows every association unless restricted) |
| Restriction `ignore` on a child direction | Not followed |
| Restriction `ignore` on a parent direction, or an excluded association | `on_missing: null` (Jailer exports the row with the reference dangling; the key sets agree) |
| `exclude-tables.csv` | Excludes, added to the spec's own |
| Data model `table.csv` | Checked against the catalog: a primary key that differs fails the plan |
| Layout, XML mapping, export mode, version and other display settings | Ignored, listed in the report |

Not yet supported, and failing the plan by name rather than running a
different subset: restriction conditions (`A.` / `B.` SQL), joins other than
column equality, subject limits and orders, `${param}` placeholders and column
filters. Each would need a native equivalent first.

## Design

```
native YAML/JSON spec ─┐
                       ├─► subset model (graph + rules) ─► planner ─► extract
Jailer .jm + datamodel ┘
```

Both formats compile to the same internal subset model.

- `datadouble run --subset jailer:model.jm …` reads the Jailer files on every
  run. Jailer stays the design tool; edits committed to the `.jm` take effect on
  the next run with no regeneration step.
- `datadouble subset import-jailer model.jm --datamodel dir/` writes a native
  spec plus `catalog.lock`, and a JSON report of anything mapped approximately or
  dropped. It fails if the data model does not match the live schema.

## Subtleties

1. **Opposite defaults.** Jailer follows every association, including child
   edges, unless restricted. The native format follows child edges only when
   asked. The Jailer front end lists every unrestricted child direction as a
   followed edge, so a model produces the same subset in both tools.
2. **Identical results.** Jailer executes by iteratively collecting keys in
   working tables, the same approach as Datadouble's collect-keys strategy. Any
   shortcut the planner takes must produce identical row sets.
3. **Schema drift.** The data model is a snapshot of the schema. The planner
   compares it to the live catalog; removed or changed objects fail, and new ones
   are handled by [strict mode](excludes-and-strict-mode.md).
4. **Raw SQL conditions** in the source's dialect are passed through, with the
   `T.` / `A.` / `B.` aliases rewritten.
5. **Relational only.** Jailer is JDBC-based, so this applies to the relational
   engines, not MongoDB, Redis or Salesforce.

## Conformance test

The Compose `jailer` service (Jailer 17.3.1, pinned) builds Jailer's data
model of the MySQL Sakila and exports `dev/jailer/sakila.jm` as JSON, unsorted
(Sakila's reference cycles have no insert order) and with its key collection
in an embedded database, so nothing is written to the source. The test runs
the same model through Datadouble's front end with the keys strategy and
compares the primary-key sets of every table; they are identical, 8,501 rows.
