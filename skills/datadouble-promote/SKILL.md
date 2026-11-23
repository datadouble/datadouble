---
name: datadouble-promote
description: Set up Datadouble's promote job, the one gate between production and everywhere else - it copies a verified, signed golden image into the export store non-production reads. Use when adding the export store, changing what promote requires, or when promote refuses an image.
---

# The promote job

Follow `../RULES.md` throughout. `promote` runs inside the production
boundary, after `build`.

## The spec's side of it

- `promote.to`: the export stores. Non-production gets read-only credentials
  for these and nothing else. `promote` also writes an `index.json` under
  each `<spec>/<group>/` there, which `fetch` reads when the export store is
  served by a web server (the read-only `http` store, in `fetch.from` only).
  Point `promote.to` at the directory or bucket itself, never at `http`.
  `promote` lists each export store to rebuild the index, so S3 export
  stores need `s3:ListBucket` for `promote`'s credentials.
- `promote.strict`: the strict-mode level an image must have been built with
  (`columns` by default). Never lower it to let an image through (rule 4).
- `trust`: how promote checks the image's signature (a public key, or the
  identity and issuer for keyless signing).

## Generate it

With `build`, as `datadouble-build` shows (`--job build,promote,prune`), so
it follows a successful build. Alone, it is a manual job.

## When promote refuses

It names the image and why: verification failed or was skipped, the image
was built less strictly than required, its signature is missing or untrusted,
or an archive's hash does not match. Report that; the fix is a new build that
passes, never a weaker gate.
