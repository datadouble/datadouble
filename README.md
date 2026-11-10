# Datadouble

**Realistic, safe copies of production data for development and testing.**

Datadouble extracts data from production systems, cuts it down to a relationally
consistent subset, anonymizes it, verifies the result, and publishes it as a
"golden image" that can be stamped out into as many disposable copies as you need.

It is built around pluggable adapters, so the same pipeline works across very
different systems:

| Kind | Initial targets |
|---|---|
| Relational | PostgreSQL, MySQL, Microsoft SQL Server, Oracle |
| Document / key-value | MongoDB, Redis |
| SaaS | Salesforce |

> **Status: planning.** There is no code yet. This repository currently holds the
> design documents. Everything here is open for discussion.

## Why

Developers and test systems need data that behaves like production: realistic
volumes, real edge cases, intact relationships. They must not get real people's
data. Existing tools tend to cover one engine well (usually PostgreSQL), or are
commercial platforms. Datadouble aims to be the open, pluggable toolkit that
covers the whole estate.

## Shape of the tool

```
plan → extract → load → transform → verify → package → store → replicate
```

- **Subsetting happens during extract**, pushed down to the source, so multi-terabyte
  databases never have to be copied in full.
- **Every stage writes an artifact with a manifest**, so runs are resumable and
  stages can be chained in a single command.
- **JSON in, JSON out** is the primary interface; the CLI is a thin layer over it.
- **Jailer extraction models (`.jm`) are supported natively**, so subsets can be
  designed in Jailer's GUI and run repeatedly by Datadouble.
- **Strict mode** refuses to copy tables or columns the subset model has never
  seen, the usual way unmasked personal data leaks into test copies.

## Design documents

- [Requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [Subsetting](docs/subsetting.md)
- [Excludes and strict mode](docs/excludes-and-strict-mode.md)
- [Jailer compatibility](docs/jailer.md)
- [Local development and testing](docs/local-development.md)
- [Prior art](docs/prior-art.md)
- [Decision log and open questions](docs/decisions.md)
