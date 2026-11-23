# datadouble-github-actions

The GitHub Actions CI/CD adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/). It has two parts:

- **The action in this directory,** `datadouble/datadouble/ci/github-actions`,
  which runs one `datadouble` command in the Datadouble image.
- **The Python package,** which writes workflows for `datadouble ci init
  --system github-actions` and, at run time, turns a response into step
  outputs, a job summary, annotations and log masks.

## Writing workflows

```
datadouble ci init --system github-actions --job build,promote,lock \
    --spec datadouble/shop.yml --runner prod-db --work /srv/datadouble/work
datadouble ci init --system github-actions --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner staging
```

Each job gets its own workflow, `.github/workflows/datadouble-<spec>-<job>.yml`,
with its schedule (in UTC), its trigger by another job (`promote` after a
successful `build`, a pool's `provision` after `fetch`), pull request triggers
for on-demand copies, and a concurrency group. The response lists the
environment variables the jobs read secrets from; define each as a
repository or environment secret. Each job receives only the ones its part of
the spec names. Name credentials an adapter reads from the environment rather
than the spec, such as `AWS_ACCESS_KEY_ID`, with `--env NAME`.

`build`, `promote` and `lock` need `--runner`, a self-hosted runner's label
inside the production boundary, and `build` needs `--work`, a directory in
production scratch space that outlives the job, so a retried run resumes its
generation. `ci init` refuses GitHub's own runner labels for every job: the
jobs need runners that keep their files, and build, promote and lock need
runners inside the boundary.

## Re-locking the catalog

The manual `lock` job (`--job lock`, on the production side with `build`)
re-locks after a schema change. It runs `datadouble lock --output
datadouble-lock` and uploads the new `catalog.lock` and `changes.txt` as the
workflow run's artifact, `datadouble-<spec>-lock`, with
`actions/upload-artifact`. It never commits: download the artifact from the
run's page, check every new or changed column in `changes.txt` for personal
data, then commit `catalog.lock` beside the spec. A lock holds names, types
and versions, never values (decision 123).

## What a job needs on its runner

- The mark `DATADOUBLE_BOUNDARY=inside` in the runner's `.env`, on runners
  inside the production boundary (decision 98).
- Docker, with its socket at `/var/run/docker.sock`. The action runs the
  Datadouble image on the host's network, with the workspace and every
  absolute path the spec names mounted at the same path, so the engine
  containers Datadouble starts can mount them too.
- Those paths, existing and owned by the engine's user (`999:999` for
  PostgreSQL), which Datadouble runs as. The action fails on a path the
  runner lacks, rather than let Docker create it owned by root.

## Outcomes

| Outcome | The step | Annotation |
|---|---|---|
| success | passes | none |
| warning | passes | a warning per warning |
| failure | fails | an error with the reason |

Step outputs are `outcome`, `generation` (for `run`) and `error` (the error
code, for a failure), and `fingerprint` and `changes` (their number) for
`lock`. The action declares each, so a workflow step with an `id` reads them
as `steps.<id>.outputs.<name>`. Datadouble never writes data artifacts to GitHub's artifact storage;
the `lock` job's lock and changes hold no data values.
