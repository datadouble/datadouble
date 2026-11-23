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
PostgreSQL), the most recent lines' images only. An image a channel points to is never removed. Pruning the
export stores also rewrites each group's `index.json` to match what is left
(or removes it when nothing is), so the lines a `fetch` through
the `http` store finds stay current. `--target export`
prunes the stores in `promote.to`, the ones `promote` writes; the read-only
`http` store in `fetch.from` is never pruned.

## See before it removes

```
datadouble prune --spec <spec> --target <store|export|staging> --dry-run
```

reports what would go and the space recovered. Show the person that before
the first scheduled run. The generated job also takes a dry-run switch
(`dry-run` in GitHub, `DATADOUBLE_DRY_RUN` in GitLab, `DRY_RUN` in Jenkins,
`dryRun` in Argo CD values).

`ci init` generates it with either side's jobs; alone, it needs `--side`.
