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
| Search | Elasticsearch, OpenSearch |
| Files | Filesystem trees |
| SaaS | Salesforce |

> **Status: in development.** The design is complete and version 1 is being
> built to [the v1 plan](docs/v1-plan.md). There is no release yet.

## Why

Developers and test systems need data that behaves like production: realistic
volumes, real edge cases, intact relationships. They must not get real people's
data. Existing tools tend to cover one engine well (usually PostgreSQL), or are
commercial platforms. Datadouble aims to be the open, pluggable toolkit that
covers the whole estate.

## Shape of the tool

```
plan → extract → load → transform → package → verify → store → promote → fetch → replicate → publish
```

- **Subsetting happens during extract**, pushed down to the source, so multi-terabyte
  databases never have to be copied in full.
- **Every stage writes an artifact with a manifest**, so runs are resumable and
  stages can be chained in a single command.
- **JSON in, JSON out** is the primary interface; the CLI is a thin layer over it.
- **Jailer extraction models (`.jm`) are supported natively**, so subsets can be
  designed in Jailer's GUI and run repeatedly by Datadouble.
- **Ready-made CI/CD integrations** for GitHub Actions, GitLab CI, Jenkins and
  Argo CD. Golden images are built inside production, and only a verified,
  anonymized image ever leaves it.
- **Agent skills** let a coding agent set up any part of a pipeline, or a whole
  pipeline at once.
- **Strict mode** refuses to copy tables or columns the subset model has never
  seen, the usual way unmasked personal data leaks into test copies.

## Design documents

- [Requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [Subsetting](docs/subsetting.md)
- [Anonymization](docs/anonymization.md)
- [Excludes and strict mode](docs/excludes-and-strict-mode.md)
- [Jailer compatibility](docs/jailer.md)
- [CI/CD integration](docs/ci-cd.md)
- [Agent skills](docs/agent-skills.md)
- [Local development and testing](docs/local-development.md)
- [Prior art](docs/prior-art.md)
- [Version 1 plan](docs/v1-plan.md)
- [External services](docs/external-services.md) (maintainers)
- [Decision log and open questions](docs/decisions.md)

## Licence

[MIT](LICENSE)
