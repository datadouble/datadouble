---
name: datadouble-prune
description: Set up retention for Datadouble's golden images in the build store, the export store and staging - keep the last N generations and anything recent, never what a channel points to. Use when stores grow, or to see what pruning would remove.
---

# The prune job

Follow `../RULES.md` throughout. `prune` runs on both sides: the build and
export stores in production, staging outside it.

## Retention

The spec's `prune` section: keep the last generations and anything younger
than a number of days, and, by compatibility line (`8.4` for MySQL, `17` for
PostgreSQL), the most recent lines' images only. An image a channel points to is never removed.

## See before it removes

```
datadouble prune --spec <spec> --target <store|export|staging> --dry-run
```

reports what would go and the space recovered. Show the person that before
the first scheduled run. The generated job also takes a dry-run switch
(`dry-run` in GitHub, `DATADOUBLE_DRY_RUN` in GitLab, `DRY_RUN` in Jenkins,
`dryRun` in Argo CD values).

`ci init` generates it with either side's jobs; alone, it needs `--side`.
