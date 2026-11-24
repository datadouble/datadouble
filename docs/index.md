---
hide:
  - navigation
  - toc
---

# Datadouble

**Realistic, safe copies of production data for development and testing.**

!!! note "Status: in review"
    Version 1 is built to [the v1 plan](v1-plan.md) and under review before
    its first release. There is no release yet: until there is, build
    Datadouble's image from a checkout, as the [quickstart](using/quickstart.md)
    shows.

## The problem

Developers and test systems work best with data that behaves like
production. It should have realistic volumes, real edge cases and intact
relationships between records. But it must not hold real people's data.

Getting both at once is hard:

- **Production is big.** A database may hold terabytes. Copying all of it
  for every developer or test run is slow and expensive.
- **A small piece must still hold together.** If you take some orders, you
  need their customers and products too, or the copy breaks the first time
  an application reads it.
- **Personal data leaks easily.** One unreviewed column of email addresses
  is enough to put real people's data on a laptop.
- **Teams run many engines.** A typical estate mixes relational databases,
  document stores, search indexes, SaaS systems and plain files.

## What Datadouble does

Datadouble builds a **golden image**: a small, anonymized, verified copy of a
production system. It builds the image once, inside production, then stamps
out as many disposable copies of it as people need.

To build the image, Datadouble:

1. reads a relationally consistent subset of the source, filtering at the
   source so it never copies the whole thing;
2. anonymizes that subset;
3. verifies the result, for example that every reference still has its
   parent, and scans it for personal data;
4. packages it as a golden image.

The work happens in a series of **stages**. The first half runs in
production, where the real data lives. Only a verified golden image crosses
out, through a step called `promote`. The second half runs outside
production and turns the image into copies:

<div class="dd-pipeline" role="figure" aria-label="Pipeline stages split across the production boundary" markdown>
<div class="dd-zone dd-prod">
<div class="dd-zone-head"><span>Production</span><span>real personal data lives here</span></div>
<div class="dd-stages">
<span class="dd-stage">plan</span><span class="dd-arrow">→</span>
<span class="dd-stage">extract</span><span class="dd-arrow">→</span>
<span class="dd-stage">load</span><span class="dd-arrow">→</span>
<span class="dd-stage">transform</span><span class="dd-arrow">→</span>
<span class="dd-stage">package</span><span class="dd-arrow">→</span>
<span class="dd-stage">verify</span><span class="dd-arrow">→</span>
<span class="dd-stage">store</span><span class="dd-arrow">→</span>
<span class="dd-stage dd-gate">promote</span>
</div>
</div>
<div class="dd-crossing">↓ only a <strong>verified golden image</strong> crosses, through the export store ↓</div>
<div class="dd-zone dd-nonprod">
<div class="dd-zone-head"><span>Non-production</span><span>read-only access to the export store only</span></div>
<div class="dd-stages">
<span class="dd-stage">fetch</span><span class="dd-arrow">→</span>
<span class="dd-stage">replicate</span><span class="dd-arrow">→</span>
<span class="dd-stage">announce</span>
</div>
</div>
<div class="dd-ops">Operations on existing images and copies: <span class="dd-stage">release</span> <span class="dd-stage">prune</span> <span class="dd-stage">activate</span></div>
</div>

`promote` copies the image into an **export store**, the only storage
non-production can read. On the other side, `fetch` brings the image in,
`replicate` makes copies of it, and `announce` tells people where those
copies are. Three more operations look after images and copies already
made: `release` destroys copies, `prune` removes old images, and `activate`
rolls back to an earlier image. The [architecture](architecture.md) page
walks through every stage.

## How the pieces fit

A team describes what it wants in a **spec**, a YAML file. The spec names
the sources to read, which rows to keep, how to anonymize them and where to
put the result. Everything else works from that spec:

- **The `datadouble` command** runs the stages. Its primary interface is JSON
  in, JSON out. The CLI is a thin layer that builds the same JSON requests.
- **Adapters** do the engine-specific work: one per engine, plus adapters for
  storage, replication, anonymization and the rest. The core contains no
  engine-specific logic.
- **CI/CD integrations** for GitHub Actions, GitLab CI, Jenkins and Argo CD
  run the stages as ready-made jobs on a schedule or on demand.
- **Agent skills** let a coding agent set up any part of a pipeline, or a
  whole pipeline at once, in a team's own repositories.

A few features address the problems above directly:

- **Subsetting happens during extract,** pushed down to the source, so
  multi-terabyte databases never have to be copied in full.
- **Strict mode** refuses to copy tables or columns the subset model has never
  seen. That is the usual way unmasked personal data leaks into test copies.
- **Every stage writes an artifact with a manifest,** so runs are resumable
  and stages can be chained in a single command.
- **Jailer extraction models** are supported natively, so subsets can be
  designed in Jailer's GUI and run repeatedly by Datadouble.

## Engines

| Kind | Targets |
|---|---|
| Relational | PostgreSQL, MySQL, Microsoft SQL Server, Oracle |
| Document and key-value | MongoDB, Redis |
| Search | Elasticsearch, OpenSearch |
| SaaS | Salesforce |
| Files | Filesystem trees |

## Design principles

These principles shape every part of Datadouble. When a design choice looks
odd, one of them is usually the reason.

- **Engine breadth is a core goal.** Nothing is designed for one engine
  without saying how the others fit.
- **Assume terabyte-scale sources.** Subsetting is pushed into extract and
  reads one consistent snapshot.
- **The core contains no engine-specific logic.** Engine quirks stay inside
  adapters behind narrow interfaces.
- **JSON is the primary interface.** The CLI, the CI/CD adapters and the agent
  skills only build JSON requests.
- **Fail loudly.** No silent configuration merges, and no warn-and-continue for
  anything that could put real personal data into a copy.
- **Real personal data stays in production.** Only a verified golden image
  crosses out, through `promote`.
- **Wrap mature tools** rather than reimplementing them: native dump tools,
  SFDMU, DBLab Engine, Jailer models.

## Where to go next

- To try it, follow the [quickstart](using/quickstart.md).
- To understand the design, read the [requirements](requirements.md), then
  the [architecture](architecture.md).
- To see why things are the way they are, read the
  [decision log](decisions.md).
