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

| Jailer | Datadouble |
|---|---|
| Subject + condition (`T.` alias) | Seed filter |
| Limit and limit order | Seed `limit` / `order_by` |
| Association, first-insert B, n:1 | Parent edge (always followed) |
| `inverse-<name>` | Child edge |
| Restriction `ignore` | `follow: false` |
| Restriction SQL (`A.` / `B.` aliases) | Edge condition |
| Virtual association, any join expression | Virtual foreign key with a join expression |
| `${param}` | Plan-time parameter |
| Column filters | Anonymization transforms, or a warning |
| `exclude-tables.csv` | Excludes |
| Data model `table.csv` / `column.csv` | Catalog lock for strict mode |
| Layout, XML mapping, export mode | Ignored, listed in the report |

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
   asked. The Jailer front end sets `default_follow: all` so a model produces the
   same subset in both tools.
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

Jailer ships a Demo-Sakila data model, and Sakila is the MySQL sample dataset in
the local development setup. The conformance test runs the same `.jm` through
Jailer's CLI and through Datadouble against the Sakila container, and compares the
primary-key sets per table. They must be identical. Jailer's demo models can be
included as test fixtures with attribution under its Apache-2.0 licence.
