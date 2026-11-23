# datadouble-gitlab

The GitLab CI/CD adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/). It writes jobs for
`datadouble ci init --system gitlab` and, at run time, turns a response into
a dotenv report and an exit code GitLab understands.

## Writing jobs

```
datadouble ci init --system gitlab --job build,promote,prune,lock \
    --spec datadouble/shop.yml --runner prod-db --work /srv/datadouble/work
datadouble ci init --system gitlab --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner staging
```

Each call writes one file, `.gitlab/datadouble-<spec>-<side>.yml`, and lists
what to set up in GitLab:

- **Include the file** from `.gitlab-ci.yml` (`include: local`).
- **Create the pipeline schedules** it names. GitLab keeps schedules outside
  its configuration, so a schedule sets `DATADOUBLE_JOB` to the job it runs
  (`shop-build`, `shop-fetch`). Running a pipeline by hand works the same way.
- **Run merge request pipelines,** if the spec makes on-demand copies: a merge
  request's pipeline makes copies for `mr-<iid>` in its environment, and the
  environment's stop job releases them when the request is merged or closed.
- **Define the variables** the jobs read secrets from as masked, protected
  CI/CD variables. Each job passes on only the ones its part of the spec
  names.

The file includes the job template, `datadouble.yml` in this package, from
this repository at the release's tag, pinned by its SHA-256 (`include:
remote` with `integrity`). GitLab must be able to reach
`raw.githubusercontent.com`; on a GitLab without that, mirror the file and
change the URL. The CI/CD Catalog components follow once the GitLab mirror of
this repository exists.

## Re-locking the catalog

The manual `lock` job (`--job lock`, on the production side with `build`)
re-locks after a schema change. It runs `datadouble lock --output
datadouble-lock` and keeps the new `catalog.lock` and `changes.txt` in the
job's artifacts with `datadouble.env`; start it by running a pipeline with
`DATADOUBLE_JOB` set to `<spec>-lock`. It never commits: download the job's
artifacts, check every new or changed column in `changes.txt` for personal
data, then commit `catalog.lock` beside the spec. A lock holds names, types
and versions, never values (decision 123).

## What a job needs on its runner

- Tags matching `--runner`; `ci init` refuses GitLab.com's own `saas-` tags.
- The shell executor, with Docker and its socket at `/var/run/docker.sock`.
  The template runs the Datadouble image as the engine's user on the host's
  network, with the project directory and every absolute path the spec names
  mounted at the same path.
- Those paths, existing and owned by the engine's user (`999:999` for
  PostgreSQL).
- On runners inside the production boundary, `DATADOUBLE_BOUNDARY=inside` in
  the runner's `environment` (decision 98).

## Outcomes

| Outcome | The job | Outputs |
|---|---|---|
| success | passes | `DATADOUBLE_OUTCOME`, `DATADOUBLE_GENERATION` for `run` |
| warning | fails, allowed (exit code 3), shown as a warning | as above |
| failure | fails | `DATADOUBLE_ERROR`, the error code |

The outputs are a dotenv report (`datadouble.env`), which later jobs in the
pipeline read as variables. GitLab masks only variables defined as masked, so
Datadouble cannot mask anything else at run time.
