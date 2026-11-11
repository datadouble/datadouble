---
hide:
  - navigation
  - toc
---

# Datadouble

**Realistic, safe copies of production data for development and testing.**

Datadouble extracts a relationally consistent subset of a production system,
anonymizes it, verifies the result, and packages it as a golden image that can
be stamped out into as many disposable copies as people need.

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

!!! note "Status: in development"
    The design is complete and version 1 is being built to
    [the v1 plan](v1-plan.md). There is no release yet, so this site currently
    documents the design. Guides for using Datadouble arrive with version 1.

## What it does

- **Subsetting happens during extract**, pushed down to the source, so
  multi-terabyte databases never have to be copied in full.
- **Every stage writes an artifact with a manifest**, so runs are resumable and
  stages can be chained in a single command.
- **JSON in, JSON out** is the primary interface; the CLI is a thin layer over
  it.
- **Jailer extraction models** are supported natively, so subsets can be
  designed in Jailer's GUI and run repeatedly by Datadouble.
- **Strict mode** refuses to copy tables or columns the subset model has never
  seen, the usual way unmasked personal data leaks into test copies.
- **Ready-made CI/CD integrations** for GitHub Actions, GitLab CI, Jenkins and
  Argo CD, and **agent skills** that set up any part of a pipeline or all of it.

## Engines

| Kind | Targets |
|---|---|
| Relational | PostgreSQL, MySQL, Microsoft SQL Server, Oracle |
| Document and key-value | MongoDB, Redis |
| Search | Elasticsearch, OpenSearch |
| SaaS | Salesforce |
| Files | Filesystem trees |

## Design principles

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

Start with the [requirements](requirements.md) and the
[architecture](architecture.md), or see every decision in the
[decision log](decisions.md).
