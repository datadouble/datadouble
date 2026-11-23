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
- When production publishes the export store through a web server that
  non-production reaches only over HTTPS, `fetch.from` is the `http` store
  (`datadouble-http`): `{http: {url: ..., auth: ..., ca_bundle: ...}}`.
    - It is read-only. Never put it in `store` or `promote.to`; `promote`
      still writes with `filesystem` or `s3` into the directory the server
      serves.
    - It finds each group's engine versions through the `index.json` that
      `promote` writes under each `<spec>/<group>/`, not through the
      server's directory listing. If `fetch` says there are no images, the
      index is missing: the export store was not filled by `promote`, so
      promote again rather than writing an index by hand.
    - `HTTP 403` usually means the web server cannot read the files
      `promote` wrote (the filesystem store makes them readable only by
      `promote`'s user); `HTTP 401`, wrong credentials; "certificate
      verification failed", a `ca_bundle` that does not trust the server.
    - `auth` is `basic: {username: ..., password: ...}` or `bearer: ...`,
      each a secret reference like `{env: EXPORT_TOKEN}`, never a value.
      `ca_bundle` is a file path for an internal CA.
- `trust`: the same signature settings `promote` uses.

## Generate it

```
datadouble ci init --system <s> --job fetch,provision,release,prune,activate \
    --spec <spec> --runner <non-production runner>
```

It runs every 15 minutes, which is cheap: it reads the channel and a small
image summary, and downloads only when there is something newer. A pool's
`provision` follows it. On runners whose Docker is not `docker` at
`/var/run/docker.sock`, add `--docker` and `--docker-socket`, as for build.

Tell the person which variables to define as secrets on this side only; they
must never be the source's or the production stores' credentials (rule 5).

## Engine versions

`fetch` takes the newest compatibility line in the export store unless
`fetch.engine_version` names one (`8.4`, not `8.4.13`). When the source moves
to a new line, say so: consumers serving copies need a server of that line.
