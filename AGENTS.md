# Guidance for AI coding agents

Datadouble makes realistic, safe copies of production data for development and
testing: extract with subsetting, anonymize, verify, package as a golden image,
store, and replicate. See the [README](README.md).

## Status

Planning. There is no code yet; the repository holds design documents only.

## Before proposing anything

1. Read [docs/decisions.md](docs/decisions.md). Decided items are settled; don't
   reopen them unless asked. Open questions are listed there too.
2. Read the design document for the area you're touching:
   - [requirements](docs/requirements.md)
   - [architecture](docs/architecture.md): stages, adapters, manifests, interfaces, repo layout
   - [subsetting](docs/subsetting.md)
   - [excludes and strict mode](docs/excludes-and-strict-mode.md)
   - [Jailer compatibility](docs/jailer.md)
   - [local development and testing](docs/local-development.md)
   - [prior art](docs/prior-art.md)

## Design principles to preserve

- **Engine breadth is a core goal:** PostgreSQL, MySQL, SQL Server, Oracle,
  MongoDB, Redis and Salesforce. Don't design anything that only works for one
  engine without saying how the others fit.
- **Assume terabyte-scale sources.** Subsetting is pushed down into extract and
  reads one consistent snapshot. Designs that require a full copy first don't work.
- **The core contains no engine-specific logic.** Engine quirks stay inside
  adapters behind narrow interfaces.
- **JSON in / JSON out is the primary interface.** The CLI only builds JSON
  requests; never add behavior that exists in one interface but not the other.
- **Fail loudly.** No silent configuration merges, and no warn-and-continue for
  anything that could put real personal data into a copy. `strict: columns` is
  the default.
- **Wrap mature tools rather than reimplement them** (native dump tools, SFDMU,
  DBLab Engine, Jailer models).

## Working conventions

- Record new decisions in `docs/decisions.md` in the same change that makes them.
- Keep design docs in sync with any change that alters behavior they describe.
- Never put credentials, real hostnames or real personal data in the repository,
  including in examples, fixtures and recorded API cassettes.
- Commit messages: a short imperative subject line, then a body explaining why.
