---
name: datadouble-fetch
description: Set up Datadouble's fetch job outside the production boundary - it brings newly promoted golden images from the export store into staging, checking signatures and hashes, and skips work when nothing is newer. Use when setting up non-production staging.
---

# The fetch job

Follow `../RULES.md` throughout. `fetch` runs outside the production
boundary, with read-only credentials for the export store alone.

## The spec's side of it

- `fetch.from`: the export store. `fetch.staging`: a directory on the
  non-production runner, kept between runs, holding the last `keep` images so
  `activate` can roll back.
- `trust`: the same signature settings `promote` uses.

## Generate it

```
datadouble ci init --system <s> --job fetch,provision,release,prune,activate \
    --spec <spec> --runner <non-production runner>
```

It runs every 15 minutes, which is cheap: it reads the channel and a small
image summary, and downloads only when there is something newer. A pool's
`provision` follows it.

Tell the person which variables to define as secrets on this side only; they
must never be the source's or the production stores' credentials (rule 5).
